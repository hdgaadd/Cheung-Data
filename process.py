"""
Cheung-Data v2 - 素材处理管线

主流程：ASR 识别 + 音频切分 + 匿名聚类 + 角色标注

用法：
  python process.py --namespace 魔幻手机                # 完整流程（有 .npy 时自动标注）
  python process.py --namespace 魔幻手机 --no-label     # 不标注，只切分+聚类
  python process.py --label 魔幻手机/EP01               # 仅重新标注（声纹库更新后使用）

依赖：funasr, numpy, soundfile, scikit-learn, FFmpeg
"""

import os
import sys
import json
import subprocess
import argparse
import shutil
from pathlib import Path

import numpy as np


# ============================================================
# 配置加载
# ============================================================

def load_config():
    """加载 config.json"""
    config_path = Path("config.json")
    if not config_path.exists():
        return {
            "export_path": "export",
            "namespaces": {},
            "clustering": {"distance_threshold": 0.30, "min_duration": 1.0},
            "identification": {"similarity_threshold": 0.70, "margin_threshold": 0.10},
        }
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


def get_namespace_config(config, namespace):
    """获取指定命名空间的热词和纠错表"""
    ns_config = config.get("namespaces", {}).get(namespace, {})
    return {
        "hotwords": ns_config.get("hotwords", ""),
        "corrections": ns_config.get("corrections", {}),
    }


# ============================================================
# 工具函数
# ============================================================

def refresh_path():
    """刷新 PATH 环境变量（Windows）"""
    if sys.platform == "win32":
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment") as key:
            system_path = winreg.QueryValueEx(key, "Path")[0]
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment") as key:
            try:
                user_path = winreg.QueryValueEx(key, "Path")[0]
            except FileNotFoundError:
                user_path = ""
        os.environ["PATH"] = system_path + ";" + user_path


def check_ffmpeg():
    """检查 FFmpeg 是否可用"""
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        refresh_path()
        try:
            subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
            return True
        except (subprocess.CalledProcessError, FileNotFoundError):
            return False


def format_time(seconds):
    """将秒数格式化为 XXmXXs"""
    m = int(seconds // 60)
    s = int(seconds % 60)
    return f"{m:02d}m{s:02d}s"


def post_correct(text, corrections):
    """对识别文本做后处理纠错"""
    for wrong, right in corrections.items():
        text = text.replace(wrong, right)
    return text


def cosine_similarity(vec_a, vec_b):
    """计算两个向量的余弦相似度"""
    return float(np.dot(vec_a, vec_b) / (np.linalg.norm(vec_a) * np.linalg.norm(vec_b)))


# ============================================================
# ASR 相关
# ============================================================

def convert_to_16k_mono(input_path, output_path):
    """将音频转换为 16kHz 单声道 WAV"""
    cmd = [
        "ffmpeg", "-y",
        "-i", str(input_path),
        "-ar", "16000", "-ac", "1",
        "-acodec", "pcm_s16le",
        str(output_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode == 0


def split_by_silence(text, timestamps):
    """
    按句末标点切分文本为句子。

    text: 完整识别文本（含标点）
    timestamps: 字级时间戳 [[start_ms, end_ms], ...]

    返回: [{start, end, text}, ...]
    """
    punctuation = set("。？！；…，,、：:""''「」（）()《》")
    sentence_ends = set("。？！；…")

    # 建立字符到 timestamp 的映射
    char_info = []
    ts_idx = 0
    for char in text:
        if char in punctuation:
            char_info.append((char, None))
        else:
            if ts_idx < len(timestamps):
                char_info.append((char, ts_idx))
                ts_idx += 1
            else:
                char_info.append((char, None))

    # 按句末标点切分
    segments = []
    current_chars = []
    current_start = None
    current_end = None

    for char, ts_i in char_info:
        current_chars.append(char)
        if ts_i is not None:
            if current_start is None:
                current_start = timestamps[ts_i][0]
            current_end = timestamps[ts_i][1]

        if char in sentence_ends and current_start is not None:
            segment_text = "".join(current_chars).strip()
            if segment_text:
                segments.append({
                    "start": current_start / 1000.0,
                    "end": current_end / 1000.0,
                    "text": segment_text,
                })
            current_chars = []
            current_start = None
            current_end = None

    # 残余文本
    segment_text = "".join(current_chars).strip()
    if segment_text and current_start is not None:
        segments.append({
            "start": current_start / 1000.0,
            "end": current_end / 1000.0,
            "text": segment_text,
        })

    # 超长片段按逗号二次切分
    final_segments = []
    for seg in segments:
        duration = seg["end"] - seg["start"]
        if duration <= 15.0:
            final_segments.append(seg)
        else:
            final_segments.extend(split_long_by_comma(seg))

    return final_segments


def split_long_by_comma(seg):
    """对超长片段按逗号二次切分，时间按字数比例分配"""
    text = seg["text"]
    parts = []
    current = ""
    for char in text:
        current += char
        if char in "，," and len(current) >= 6:
            parts.append(current)
            current = ""
    if current:
        if parts:
            parts.append(current)
        else:
            return [seg]

    if len(parts) <= 1:
        return [seg]

    total_duration = seg["end"] - seg["start"]
    total_chars = sum(len(p) for p in parts)

    results = []
    time_cursor = seg["start"]
    for part in parts:
        part_duration = total_duration * (len(part) / total_chars)
        part_end = time_cursor + part_duration
        part_text = part.strip()
        if part_text:
            results.append({
                "start": round(time_cursor, 3),
                "end": round(part_end, 3),
                "text": part_text,
            })
        time_cursor = part_end

    return results


def merge_vad_fragments(segments, max_gap=1.0):
    """合并被 VAD 错误切断的片段"""
    sentence_ends = set("。？！；…")

    if not segments:
        return segments

    merged = [segments[0]]
    for seg in segments[1:]:
        prev = merged[-1]
        gap = seg["start"] - prev["end"]
        combined_duration = seg["end"] - prev["start"]

        prev_last_char = prev["text"].rstrip()[-1] if prev["text"].rstrip() else ""

        if prev_last_char not in sentence_ends and gap < max_gap and combined_duration <= 30.0:
            merged[-1] = {
                "start": prev["start"],
                "end": seg["end"],
                "text": prev["text"] + seg["text"],
            }
        else:
            merged.append(seg)

    return merged


def merge_short_segments(segments, max_gap=0.3, min_chars=4):
    """合并过短的相邻片段"""
    if not segments:
        return segments

    merged = [segments[0]]
    for seg in segments[1:]:
        prev = merged[-1]
        gap = seg["start"] - prev["end"]
        combined_duration = seg["end"] - prev["start"]
        if gap < max_gap and len(prev["text"]) < min_chars and combined_duration <= 15.0:
            merged[-1] = {
                "start": prev["start"],
                "end": seg["end"],
                "text": prev["text"] + seg["text"],
            }
        else:
            merged.append(seg)
    return merged


def transcribe_audio(audio_path, model, hotwords=""):
    """使用 FunASR 识别音频，返回 segments 列表"""
    print(f"  [ASR] 语音识别中...")

    kwargs = {
        "input": str(audio_path),
        "batch_size_s": 300,
        "batch_size_threshold_s": 60,
    }
    if hotwords:
        kwargs["hotword"] = hotwords

    res = model.generate(**kwargs)

    segment_list = []
    for item in res:
        if "sentence_info" in item:
            for sent in item["sentence_info"]:
                text = sent["text"].strip()
                if text:
                    segment_list.append({
                        "start": sent["start"] / 1000.0,
                        "end": sent["end"] / 1000.0,
                        "text": text,
                    })
        elif "text" in item and "timestamp" in item and item["timestamp"]:
            sentences = split_by_silence(item["text"], item["timestamp"])
            segment_list.extend(sentences)
        elif "text" in item:
            text = item["text"].strip()
            if text:
                segment_list.append({"start": 0.0, "end": 0.0, "text": text})

    # 合并 VAD 碎片
    segment_list = merge_vad_fragments(segment_list)

    return segment_list


def postprocess_segments(segments, corrections, min_duration=1.0):
    """后处理：纠错 + 过滤极短 + 合并短句"""
    # 纠错
    for seg in segments:
        seg["text"] = post_correct(seg["text"], corrections)

    # 过滤极短片段
    segments = [seg for seg in segments if (seg["end"] - seg["start"]) >= min_duration]

    # 合并短句
    segments = merge_short_segments(segments)

    return segments


# ============================================================
# 音频切分
# ============================================================

def split_audio_clips(source_audio, segments, clips_dir):
    """按 segments 时间戳切分音频为独立 WAV 文件"""
    print(f"  [切分] 生成音频片段...")
    clips_dir.mkdir(parents=True, exist_ok=True)

    count = 0
    for i, seg in enumerate(segments, 1):
        start_str = format_time(seg["start"])
        end_str = format_time(seg["end"])
        filename = f"{i:03d}_{start_str}-{end_str}.wav"
        output_path = clips_dir / filename

        duration = seg["end"] - seg["start"] + 0.3  # 末尾加缓冲
        cmd = [
            "ffmpeg", "-y",
            "-i", str(source_audio),
            "-ss", str(seg["start"]),
            "-t", str(duration),
            "-vn",
            "-acodec", "pcm_s16le",
            "-ar", "16000", "-ac", "1",
            str(output_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            count += 1
            seg["file"] = filename
        else:
            seg["file"] = None

    print(f"  ✓ 切分完成，共 {count}/{len(segments)} 个片段")
    return count


# ============================================================
# 匿名聚类
# ============================================================

def load_embedding_model():
    """加载 3D-Speaker ERes2NetV2 embedding 模型"""
    print("加载声纹 embedding 模型...")
    from funasr import AutoModel

    model = AutoModel(
        model="iic/speech_eres2netv2_sv_zh-cn_16k-common",
        device="cuda:0",
    )
    print("✓ embedding 模型加载完成")
    return model


def extract_embedding(model, wav_path):
    """对单个 WAV 文件提取 embedding 向量"""
    res = model.generate(input=str(wav_path))
    embedding = res[0]["spk_embedding"]
    if isinstance(embedding, np.ndarray):
        vec = embedding.flatten()
    else:
        vec = np.array(embedding).flatten()
    norm = np.linalg.norm(vec)
    if norm > 0:
        vec = vec / norm
    return vec


def cluster_speakers(embeddings, threshold=0.30):
    """
    对所有切片的 embedding 做层次聚类。

    threshold: 距离阈值（1 - cosine_similarity）
    返回: labels 数组
    """
    from sklearn.cluster import AgglomerativeClustering

    if len(embeddings) <= 1:
        return [0] * len(embeddings)

    clustering = AgglomerativeClustering(
        n_clusters=None,
        distance_threshold=threshold,
        metric="cosine",
        linkage="average",
    )
    labels = clustering.fit_predict(embeddings)
    return labels.tolist()


def assign_cluster_labels(labels):
    """
    按片段数量降序分配匿名标签：角色1、角色2...

    返回: dict mapping original_label -> "角色X"
    """
    from collections import Counter
    counts = Counter(labels)
    # 按数量降序排列
    sorted_clusters = sorted(counts.keys(), key=lambda k: counts[k], reverse=True)
    label_map = {}
    for rank, cluster_id in enumerate(sorted_clusters, 1):
        label_map[cluster_id] = f"角色{rank}"
    return label_map


def run_clustering(segments, clips_dir, embedding_model, config):
    """对所有切片做聚类，更新 segments 的 cluster 字段"""
    print(f"  [聚类] 提取声纹 embedding...")
    min_duration = config.get("clustering", {}).get("min_duration", 1.0)
    distance_threshold = config.get("clustering", {}).get("distance_threshold", 0.30)

    # 提取有效片段的 embedding
    valid_indices = []
    embeddings = []

    for i, seg in enumerate(segments):
        duration = seg["end"] - seg["start"]
        clip_path = clips_dir / seg.get("file", "")

        if duration < min_duration or not seg.get("file") or not clip_path.exists():
            seg["cluster"] = None
            continue

        valid_indices.append(i)
        emb = extract_embedding(embedding_model, clip_path)
        embeddings.append(emb)

    if not embeddings:
        print("  ⚠ 无有效片段可聚类")
        return

    print(f"  [聚类] 对 {len(embeddings)} 个片段做层次聚类...")
    embeddings_array = np.array(embeddings)
    labels = cluster_speakers(embeddings_array, threshold=distance_threshold)

    # 分配匿名标签
    label_map = assign_cluster_labels(labels)

    for idx, seg_i in enumerate(valid_indices):
        cluster_label = label_map[labels[idx]]
        segments[seg_i]["cluster"] = cluster_label

    # 统计
    from collections import Counter
    cluster_counts = Counter(seg.get("cluster") for seg in segments if seg.get("cluster"))
    print(f"  ✓ 聚类完成，共 {len(cluster_counts)} 个簇:")
    for name, count in sorted(cluster_counts.items(), key=lambda x: x[1], reverse=True):
        print(f"    {name}: {count} 个片段")

    # 重命名切片文件（加上 cluster 标签）
    rename_clips_by_cluster(segments, clips_dir)


def rename_clips_by_cluster(segments, clips_dir):
    """重命名切片文件：{序号}_{时间}_{角色X}.wav"""
    rename_count = 0
    for i, seg in enumerate(segments, 1):
        old_file = seg.get("file")
        if not old_file or not seg.get("cluster"):
            continue

        old_path = clips_dir / old_file
        if not old_path.exists():
            continue

        start_str = format_time(seg["start"])
        end_str = format_time(seg["end"])
        new_file = f"{i:03d}_{start_str}-{end_str}_{seg['cluster']}.wav"
        new_path = clips_dir / new_file

        if old_path != new_path:
            old_path.rename(new_path)
            seg["file"] = new_file
            rename_count += 1

    if rename_count > 0:
        print(f"  ✓ 重命名 {rename_count} 个文件")


# ============================================================
# 角色标注（按簇比对 .npy）
# ============================================================

def load_speaker_profiles(namespace):
    """加载指定命名空间的角色声纹"""
    profiles_dir = Path("speaker_profiles") / namespace
    profiles = {}

    if not profiles_dir.exists():
        return profiles

    for npy_file in profiles_dir.glob("*.npy"):
        name = npy_file.stem
        profiles[name] = np.load(str(npy_file))

    return profiles


def label_by_cluster(segments, clips_dir, embedding_model, namespace, config):
    """按簇比对角色声纹，标注 speaker 字段"""
    profiles = load_speaker_profiles(namespace)
    if not profiles:
        print(f"  [标注] 未找到声纹库 speaker_profiles/{namespace}/，跳过标注")
        return

    print(f"  [标注] 加载 {len(profiles)} 个角色声纹: {', '.join(profiles.keys())}")

    sim_threshold = config.get("identification", {}).get("similarity_threshold", 0.70)
    margin_threshold = config.get("identification", {}).get("margin_threshold", 0.10)

    # 收集每个 cluster 的 embedding
    from collections import defaultdict
    cluster_embeddings = defaultdict(list)

    for seg in segments:
        cluster = seg.get("cluster")
        if not cluster or not seg.get("file"):
            continue
        clip_path = clips_dir / seg["file"]
        if clip_path.exists():
            emb = extract_embedding(embedding_model, clip_path)
            cluster_embeddings[cluster].append(emb)

    # 对每个簇取平均 embedding，与角色声纹比对
    cluster_to_speaker = {}
    cluster_scores = {}

    for cluster_name, embs in cluster_embeddings.items():
        avg_emb = np.mean(embs, axis=0)
        avg_emb = avg_emb / np.linalg.norm(avg_emb)

        scores = {}
        for speaker_name, profile_emb in profiles.items():
            scores[speaker_name] = cosine_similarity(avg_emb, profile_emb)

        sorted_scores = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        top1_name, top1_score = sorted_scores[0]
        top2_score = sorted_scores[1][1] if len(sorted_scores) > 1 else 0.0

        if top1_score >= sim_threshold and (top1_score - top2_score) >= margin_threshold:
            cluster_to_speaker[cluster_name] = top1_name
            cluster_scores[cluster_name] = top1_score
        else:
            cluster_to_speaker[cluster_name] = None
            cluster_scores[cluster_name] = top1_score

    # 更新 segments
    labeled_count = 0
    for seg in segments:
        cluster = seg.get("cluster")
        if cluster and cluster in cluster_to_speaker:
            speaker = cluster_to_speaker[cluster]
            if speaker:
                seg["speaker"] = speaker
                seg["score"] = round(cluster_scores[cluster], 4)
                labeled_count += 1
            else:
                seg["speaker"] = None
                seg["score"] = round(cluster_scores[cluster], 4)
        else:
            seg["speaker"] = None
            seg["score"] = None

    print(f"  ✓ 标注完成: {labeled_count}/{len(segments)} 个片段已识别角色")

    # 打印匹配结果
    for cluster_name in sorted(cluster_to_speaker.keys()):
        speaker = cluster_to_speaker[cluster_name]
        score = cluster_scores[cluster_name]
        status = f"→ {speaker} ({score:.3f})" if speaker else f"→ 未匹配 ({score:.3f})"
        print(f"    {cluster_name} {status}")

    # 重命名切片文件（加上角色名）
    rename_clips_by_speaker(segments, clips_dir)


def rename_clips_by_speaker(segments, clips_dir):
    """重命名切片文件：{序号}_{时间}_{角色名}.wav"""
    rename_count = 0
    for i, seg in enumerate(segments, 1):
        old_file = seg.get("file")
        if not old_file:
            continue

        old_path = clips_dir / old_file
        if not old_path.exists():
            continue

        # 用 speaker 或 cluster 作为标签
        label = seg.get("speaker") or seg.get("cluster") or "unknown"
        start_str = format_time(seg["start"])
        end_str = format_time(seg["end"])
        new_file = f"{i:03d}_{start_str}-{end_str}_{label}.wav"
        new_path = clips_dir / new_file

        if old_path != new_path:
            old_path.rename(new_path)
            seg["file"] = new_file
            rename_count += 1

    if rename_count > 0:
        print(f"  ✓ 重命名 {rename_count} 个文件（角色标签）")


# ============================================================
# 主流程
# ============================================================

def process_single_wav(wav_path, output_dir, asr_model, embedding_model, ns_config, config, no_label=False):
    """处理单个 WAV 文件的完整流程"""
    wav_stem = wav_path.stem
    episode_dir = output_dir / wav_stem
    clips_dir = episode_dir / "clips"

    print(f"\n{'='*60}")
    print(f"处理: {wav_path.name}")
    print(f"{'='*60}")

    # 步骤 1：音频转换
    temp_16k = episode_dir / "_temp_16k.wav"
    episode_dir.mkdir(parents=True, exist_ok=True)

    print(f"  [转换] 转为 16kHz mono...")
    if not convert_to_16k_mono(wav_path, temp_16k):
        print(f"  ❌ 音频转换失败")
        return False

    try:
        # 步骤 2：ASR 识别
        hotwords = ns_config.get("hotwords", "")
        segments = transcribe_audio(temp_16k, asr_model, hotwords)

        if not segments:
            print("  ⚠ 未识别到任何对话")
            return False

        # 步骤 3：后处理
        corrections = ns_config.get("corrections", {})
        min_duration = config.get("clustering", {}).get("min_duration", 1.0)
        raw_count = len(segments)
        segments = postprocess_segments(segments, corrections, min_duration)
        print(f"  ✓ 识别完成: 原始 {raw_count} 句 → 处理后 {len(segments)} 句")

        # 初始化 segments.json 字段
        for i, seg in enumerate(segments, 1):
            seg["index"] = i
            seg["cluster"] = None
            seg["speaker"] = None
            seg["score"] = None
            seg["file"] = None

        # 步骤 4：切分音频
        split_audio_clips(temp_16k, segments, clips_dir)

        # 步骤 5：匿名聚类
        run_clustering(segments, clips_dir, embedding_model, config)

        # 步骤 6：角色标注（可选）
        namespace = output_dir.name
        if not no_label:
            label_by_cluster(segments, clips_dir, embedding_model, namespace, config)

        # 保存 segments.json
        segments_path = episode_dir / "segments.json"
        with open(segments_path, "w", encoding="utf-8") as f:
            json.dump(segments, f, ensure_ascii=False, indent=2)
        print(f"  ✓ 已保存 segments.json")

        return True

    finally:
        # 清理临时文件
        if temp_16k.exists():
            temp_16k.unlink()


def process_namespace(namespace, config, no_label=False):
    """处理整个命名空间"""
    wavs_dir = Path("wavs") / namespace
    output_dir = Path("output") / namespace

    if not wavs_dir.exists():
        print(f"❌ 输入目录不存在: {wavs_dir}")
        sys.exit(1)

    # 扫描 WAV 文件
    wav_files = sorted(wavs_dir.glob("*.wav"))
    if not wav_files:
        print(f"❌ 在 {wavs_dir} 中未找到 .wav 文件")
        sys.exit(1)

    # 增量检查
    to_process = []
    for wav_path in wav_files:
        episode_dir = output_dir / wav_path.stem
        if episode_dir.exists():
            print(f"  [跳过] {wav_path.name}（已存在 output/{namespace}/{wav_path.stem}/）")
        else:
            to_process.append(wav_path)

    if not to_process:
        print(f"\n所有 WAV 文件均已处理完毕，无新增文件。")
        print(f"如需重新处理某集，删除 output/{namespace}/{{文件夹名}}/ 后重跑。")
        return

    print(f"\n找到 {len(wav_files)} 个 WAV 文件，其中 {len(to_process)} 个待处理:")
    for f in to_process:
        print(f"  - {f.name}")

    # 加载模型
    print(f"\n加载 ASR 模型...")
    from funasr import AutoModel

    asr_model = AutoModel(
        model="iic/speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
        model_revision="v2.0.4",
        vad_model="fsmn-vad",
        vad_kwargs={"max_single_segment_time": 60000},
        punc_model="ct-punc",
        device="cuda:0",
    )
    print("✓ ASR 模型加载完成")

    embedding_model = load_embedding_model()

    # 获取命名空间配置
    ns_config = get_namespace_config(config, namespace)

    # 创建输出目录
    output_dir.mkdir(parents=True, exist_ok=True)

    # 逐个处理
    success_count = 0
    for wav_path in to_process:
        if process_single_wav(wav_path, output_dir, asr_model, embedding_model, ns_config, config, no_label):
            success_count += 1

    print(f"\n{'='*60}")
    print(f"完成！成功处理 {success_count}/{len(to_process)} 个文件")
    print(f"输出目录: {output_dir.resolve()}")
    print(f"{'='*60}")


# ============================================================
# --label 子命令：仅重新标注
# ============================================================

def label_only(target, config):
    """仅对已有的 segments.json 重新标注角色"""
    # 解析 target: "namespace/episode" 或 "namespace"
    parts = target.split("/")
    if len(parts) == 2:
        namespace, episode = parts
        episodes = [episode]
    elif len(parts) == 1:
        namespace = parts[0]
        episodes = None  # 处理全部
    else:
        print(f"❌ 格式错误，应为: namespace/episode 或 namespace")
        sys.exit(1)

    output_dir = Path("output") / namespace
    if not output_dir.exists():
        print(f"❌ 目录不存在: {output_dir}")
        sys.exit(1)

    # 确定要处理的目录
    if episodes:
        dirs_to_process = [output_dir / ep for ep in episodes]
    else:
        dirs_to_process = sorted([d for d in output_dir.iterdir() if d.is_dir()])

    if not dirs_to_process:
        print(f"❌ 未找到任何已处理的目录")
        sys.exit(1)

    # 加载模型
    embedding_model = load_embedding_model()

    for ep_dir in dirs_to_process:
        segments_path = ep_dir / "segments.json"
        clips_dir = ep_dir / "clips"

        if not segments_path.exists():
            print(f"  [跳过] {ep_dir.name}（无 segments.json）")
            continue

        print(f"\n{'='*60}")
        print(f"重新标注: {namespace}/{ep_dir.name}")
        print(f"{'='*60}")

        with open(segments_path, "r", encoding="utf-8") as f:
            segments = json.load(f)

        # 重新标注
        label_by_cluster(segments, clips_dir, embedding_model, namespace, config)

        # 保存
        with open(segments_path, "w", encoding="utf-8") as f:
            json.dump(segments, f, ensure_ascii=False, indent=2)
        print(f"  ✓ 已更新 segments.json")

    print(f"\n标注完成。")


# ============================================================
# 入口
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="Cheung-Data v2 - 素材处理管线"
    )
    parser.add_argument("--namespace", "-n",
                        help="命名空间（如: 魔幻手机）")
    parser.add_argument("--no-label", action="store_true",
                        help="不做角色标注，只切分+聚类")
    parser.add_argument("--label", "-l",
                        help="仅重新标注（格式: namespace/episode 或 namespace）")
    args = parser.parse_args()

    # 加载配置
    config = load_config()

    # --label 模式
    if args.label:
        label_only(args.label, config)
        return

    # 正常处理模式
    if not args.namespace:
        print("❌ 请指定命名空间: --namespace 魔幻手机")
        sys.exit(1)

    # 检查 FFmpeg
    if not check_ffmpeg():
        print("❌ FFmpeg 未安装或未在 PATH 中")
        sys.exit(1)

    process_namespace(args.namespace, config, no_label=args.no_label)


if __name__ == "__main__":
    main()
