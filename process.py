"""
Cheung-Data v2 - 素材处理管线

主流程：
  --gen-edit   : ASR 识别 → 生成 edit.txt + reference.wav → 打开编辑器
  --apply-edit : 按 edit.txt 切分 → 聚类 → 标注
  --label      : 仅重新标注（声纹库更新后使用）

用法：
  python process.py --namespace 魔幻手机 --gen-edit EP01.wav
  python process.py --namespace 魔幻手机 --apply-edit EP01
  python process.py --namespace 魔幻手机 --apply-edit EP01 --no-label
  python process.py --label 魔幻手机/EP01

依赖：funasr, numpy, soundfile, scikit-learn, FFmpeg
"""

import os
import sys
import json
import subprocess
import argparse
import shutil
import re
from pathlib import Path

import numpy as np
import soundfile as sf


# ============================================================
# 修复 Windows 控制台编码
# ============================================================

if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")


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
    """将秒数格式化为 XXmXX.XXs（精确到 0.01 秒）"""
    m = int(seconds // 60)
    s = seconds % 60
    return f"{m:02d}m{s:05.2f}s"


def parse_time(time_str):
    """将 XXmXX.Xs 格式解析为秒数（兼容整数格式）"""
    match = re.match(r"(\d+)m(\d+(?:\.\d+)?)s", time_str)
    if not match:
        raise ValueError(f"无法解析时间格式: {time_str}")
    return int(match.group(1)) * 60 + float(match.group(2))


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
    result = subprocess.run(cmd, capture_output=True)
    return result.returncode == 0


def split_to_phrases(text, timestamps):
    """
    按逗号级粒度切分文本为短句。

    所有停顿标点（逗号、句号、问号等）都作为切分点，
    产出最细粒度的短句列表，供 edit.txt 使用。

    text: 完整识别文本（含标点）
    timestamps: 字级时间戳 [[start_ms, end_ms], ...]

    返回: [{start, end, text}, ...]
    """
    # 标点符号集合（不占 timestamp 位置）
    punctuation = set("。？！；…，,、：:""''「」（）()《》")
    # 切分标点：遇到这些就切
    split_puncts = set("。？！；…，,")

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

    # 按切分标点切分
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

        if char in split_puncts and current_start is not None:
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

    return segments


def transcribe_to_phrases(audio_path, model, hotwords=""):
    """
    使用 FunASR 识别音频，返回逗号级短句列表。

    返回: [{start, end, text}, ...]
    """
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
            # sentence_info 粒度通常是句子级，直接用
            for sent in item["sentence_info"]:
                text = sent["text"].strip()
                if text:
                    segment_list.append({
                        "start": sent["start"] / 1000.0,
                        "end": sent["end"] / 1000.0,
                        "text": text,
                    })
        elif "text" in item and "timestamp" in item and item["timestamp"]:
            # 字级 timestamp → 按逗号级切分
            phrases = split_to_phrases(item["text"], item["timestamp"])
            segment_list.extend(phrases)
        elif "text" in item:
            text = item["text"].strip()
            if text:
                segment_list.append({"start": 0.0, "end": 0.0, "text": text})

    return segment_list


# ============================================================
# --gen-edit：生成 edit.txt + reference.wav
# ============================================================

def is_sentence_end(text):
    """判断文本是否以句末标点结尾"""
    sentence_ends = set("。？！；…")
    stripped = text.rstrip()
    return stripped and stripped[-1] in sentence_ends


def gen_edit(namespace, wav_filename, config):
    """生成 edit.txt 和 reference.wav"""
    wavs_dir = Path("wavs") / namespace
    wav_path = wavs_dir / wav_filename

    if not wav_path.exists():
        print(f"❌ WAV 文件不存在: {wav_path}")
        sys.exit(1)

    wav_stem = wav_path.stem
    episode_dir = Path("output") / namespace / wav_stem

    # 检查 edit.txt 是否已存在
    edit_path = episode_dir / "edit.txt"
    if edit_path.exists():
        print(f"❌ edit.txt 已存在: {edit_path}")
        print(f"   请删除后重新执行: del \"{edit_path}\"")
        sys.exit(1)

    print(f"\n{'='*60}")
    print(f"生成编辑文件: {namespace}/{wav_filename}")
    print(f"{'='*60}")

    # 创建输出目录
    episode_dir.mkdir(parents=True, exist_ok=True)

    # 步骤 1：音频转换
    temp_16k = episode_dir / "_temp_16k.wav"
    print(f"  [转换] 转为 16kHz mono...")
    if not convert_to_16k_mono(wav_path, temp_16k):
        print(f"  ❌ 音频转换失败")
        sys.exit(1)

    try:
        # 步骤 2：ASR 识别（逗号级粒度）
        print(f"  加载 ASR 模型...")
        from funasr import AutoModel

        asr_model = AutoModel(
            model="iic/speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
            model_revision="v2.0.4",
            vad_model="fsmn-vad",
            vad_kwargs={"max_single_segment_time": 60000},
            punc_model="ct-punc",
            device="cuda:0",
        )
        print("  ✓ ASR 模型加载完成")

        ns_config = get_namespace_config(config, namespace)
        hotwords = ns_config.get("hotwords", "")
        corrections = ns_config.get("corrections", {})

        phrases = transcribe_to_phrases(temp_16k, asr_model, hotwords)

        if not phrases:
            print("  ⚠ 未识别到任何对话")
            sys.exit(1)

        # 后处理纠错
        for phrase in phrases:
            phrase["text"] = post_correct(phrase["text"], corrections)

        # 过滤极短片段（< 0.3秒，纯噪声）
        phrases = [p for p in phrases if (p["end"] - p["start"]) >= 0.3]

        print(f"  ✓ 识别完成: {len(phrases)} 个短句")

        # 步骤 3：生成 edit.txt（按句末标点插空行）
        print(f"  [生成] 写入 edit.txt...")
        with open(edit_path, "w", encoding="utf-8") as f:
            for i, phrase in enumerate(phrases):
                start_str = format_time(phrase["start"])
                end_str = format_time(phrase["end"])
                f.write(f"[{start_str}-{end_str}] {phrase['text']}\n")

                # 如果以句末标点结尾，插入空行（预切分）
                if is_sentence_end(phrase["text"]):
                    f.write("\n")

        print(f"  ✓ edit.txt 已生成 ({len(phrases)} 行)")

        # 步骤 4：生成 reference.wav
        print(f"  [生成] 拼接 reference.wav...")
        reference_path = episode_dir / "reference.wav"
        generate_reference_wav(temp_16k, phrases, reference_path)
        print(f"  ✓ reference.wav 已生成")

        # 步骤 5：自动打开
        open_editor(edit_path)
        open_player(reference_path)

        print(f"\n{'='*60}")
        print(f"完成！请编辑后运行:")
        print(f"  python process.py --namespace {namespace} --apply-edit {wav_stem}")
        print(f"{'='*60}")

    finally:
        if temp_16k.exists():
            temp_16k.unlink()


def generate_reference_wav(source_wav, phrases, output_path, silence_duration=2.0):
    """
    将 ASR 短句音频按顺序拼接，片段间插入静音，生成 reference.wav。

    每个片段与 edit.txt 每行一一对应。
    """
    # 读取源音频
    data, sr = sf.read(str(source_wav))

    # 静音片段
    silence = np.zeros(int(silence_duration * sr))

    # 拼接
    pieces = []
    for i, phrase in enumerate(phrases):
        start_sample = int(phrase["start"] * sr)
        end_sample = int(phrase["end"] * sr)

        # 边界保护
        start_sample = max(0, start_sample)
        end_sample = min(len(data), end_sample)

        if start_sample < end_sample:
            pieces.append(data[start_sample:end_sample])

        # 片段间插入静音（最后一个片段后不加）
        if i < len(phrases) - 1:
            pieces.append(silence)

    if pieces:
        combined = np.concatenate(pieces)
        sf.write(str(output_path), combined, sr)


def open_editor(file_path):
    """用 Notepad++ 打开文件，fallback 到系统默认"""
    notepad_pp = Path(r"C:\Program Files (x86)\Notepad++\notepad++.exe")
    try:
        if notepad_pp.exists():
            subprocess.Popen([str(notepad_pp), str(file_path)])
            print(f"  ✓ 已用 Notepad++ 打开 edit.txt")
        else:
            os.startfile(str(file_path))
            print(f"  ✓ 已用默认编辑器打开 edit.txt")
    except Exception as e:
        print(f"  ⚠ 无法自动打开编辑器: {e}")


def open_player(file_path):
    """用系统默认播放器打开音频"""
    try:
        os.startfile(str(file_path))
        print(f"  ✓ 已用默认播放器打开 reference.wav")
    except Exception as e:
        print(f"  ⚠ 无法自动打开播放器: {e}")


# ============================================================
# --apply-edit：按 edit.txt 切分 + 聚类 + 标注
# ============================================================

def parse_edit_txt(edit_path):
    """
    解析 edit.txt，按空行分组。

    每组 = 一个切片：
    - 起始时间 = 组内第一行的 start
    - 结束时间 = 组内最后一行的 end
    - 文本 = 组内各行文本拼接

    返回: [{start, end, text}, ...]
    """
    with open(edit_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    # 按空行分组
    groups = []
    current_group = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            if current_group:
                groups.append(current_group)
                current_group = []
        else:
            current_group.append(stripped)

    if current_group:
        groups.append(current_group)

    # 解析每组
    line_pattern = re.compile(r"^\[(\d+m\d+(?:\.\d+)?s)-(\d+m\d+(?:\.\d+)?s)\]\s*(.*)$")
    segments = []

    for group in groups:
        group_start = None
        group_end = None
        texts = []

        for line in group:
            match = line_pattern.match(line)
            if not match:
                print(f"  ⚠ 无法解析行: {line}")
                continue

            start_str, end_str, text = match.groups()
            start = parse_time(start_str)
            end = parse_time(end_str)

            if group_start is None:
                group_start = start
            group_end = end
            if text.strip():
                texts.append(text.strip())

        if group_start is not None and group_end is not None and texts:
            segments.append({
                "start": float(group_start),
                "end": float(group_end),
                "text": "".join(texts),
            })

    return segments


def compare_with_previous_data(old_segments, new_segments):
    """对比新旧 segments，打印变化（时间调整 + 新切片）"""
    if not old_segments:
        return

    # 建立旧切片的时间范围索引（用 start 做粗匹配）
    old_by_start = {}
    for seg in old_segments:
        key = round(seg["start"], 2)
        old_by_start[key] = seg

    changes = []  # [(type, index, text, old_range, new_range)]

    for i, seg in enumerate(new_segments, 1):
        new_start = round(seg["start"], 2)
        new_end = round(seg["end"], 2)
        text = seg["text"]

        # 尝试匹配旧切片（start 相同或非常接近）
        matched_old = None
        for old_start, old_seg in old_by_start.items():
            if abs(old_start - new_start) < 0.5:
                matched_old = old_seg
                break

        if matched_old:
            old_start_r = round(matched_old["start"], 2)
            old_end_r = round(matched_old["end"], 2)
            # 时间有变化
            if abs(old_end_r - new_end) >= 0.05 or abs(old_start_r - new_start) >= 0.05:
                old_range = f"{format_time(matched_old['start'])}-{format_time(matched_old['end'])}"
                new_range = f"{format_time(seg['start'])}-{format_time(seg['end'])}"
                changes.append(("时间调整", i, text, old_range, new_range))
        else:
            # 旧的里没找到 → 新切片
            new_range = f"{format_time(seg['start'])}-{format_time(seg['end'])}"
            changes.append(("新切片", i, text, "", new_range))

    if not changes:
        print(f"\n  [对比上次] edit.txt 无变化")
        return

    # 按序号排序
    changes.sort(key=lambda x: x[1])

    print(f"\n  [对比上次] edit.txt 有变化 (切片数: {len(old_segments)} → {len(new_segments)}):\n")
    for change_type, index, text, old_range, new_range in changes:
        print(f"    [{change_type}] {index:03d} \"{text}\"")
        if old_range:
            print(f"             {old_range} → {new_range}")
        else:
            print(f"             {new_range}")
        print()


def apply_edit(namespace, episode_name, config, no_label=False):
    """按 edit.txt 切分音频 + 聚类 + 标注"""
    episode_dir = Path("output") / namespace / episode_name
    edit_path = episode_dir / "edit.txt"
    clips_dir = episode_dir / "clips"

    # 检查 edit.txt 存在
    if not edit_path.exists():
        print(f"❌ edit.txt 不存在: {edit_path}")
        print(f"   请先运行: python process.py --namespace {namespace} --gen-edit {episode_name}.wav")
        sys.exit(1)

    # 找到源 WAV
    wav_path = Path("wavs") / namespace / f"{episode_name}.wav"
    if not wav_path.exists():
        print(f"❌ 源 WAV 不存在: {wav_path}")
        sys.exit(1)

    print(f"\n{'='*60}")
    print(f"应用编辑: {namespace}/{episode_name}")
    print(f"{'='*60}")

    # 步骤 1：解析 edit.txt
    print(f"  [解析] 读取 edit.txt...")
    segments = parse_edit_txt(edit_path)
    if not segments:
        print(f"  ❌ edit.txt 中无有效内容")
        sys.exit(1)
    print(f"  ✓ 解析完成: {len(segments)} 个切片")

    # 记住旧 segments.json 路径，最后对比用
    segments_path = episode_dir / "segments.json"
    old_segments = None
    if segments_path.exists():
        with open(segments_path, "r", encoding="utf-8") as f:
            old_segments = json.load(f)

    # 步骤 2：切分音频（清空重建 clips/）
    if clips_dir.exists():
        shutil.rmtree(clips_dir)
    clips_dir.mkdir(parents=True, exist_ok=True)

    print(f"  [切分] 从源 WAV 切分音频片段...")
    for i, seg in enumerate(segments, 1):
        seg["index"] = i
        seg["cluster"] = None
        seg["speaker"] = None
        seg["score"] = None

        start_str = format_time(seg["start"])
        end_str = format_time(seg["end"])
        filename = f"{i:03d}_{start_str}-{end_str}.wav"
        output_path = clips_dir / filename

        duration = seg["end"] - seg["start"] + 0.3  # 末尾加缓冲
        cmd = [
            "ffmpeg", "-y",
            "-i", str(wav_path),
            "-ss", str(seg["start"]),
            "-t", str(duration),
            "-vn",
            "-acodec", "pcm_s16le",
            "-ar", "16000", "-ac", "1",
            str(output_path),
        ]
        result = subprocess.run(cmd, capture_output=True)
        if result.returncode == 0:
            seg["file"] = filename
        else:
            seg["file"] = None
            print(f"    ⚠ 切分第 {i} 个片段失败")

    valid_count = sum(1 for s in segments if s["file"])
    print(f"  ✓ 切分完成: {valid_count}/{len(segments)} 个片段")

    # 步骤 3：匿名聚类
    embedding_model = load_embedding_model()
    run_clustering(segments, clips_dir, embedding_model, config)

    # 步骤 4：角色标注（可选）
    if not no_label:
        label_by_cluster(segments, clips_dir, embedding_model, namespace, config)

    # 步骤 5：保存 segments.json
    with open(segments_path, "w", encoding="utf-8") as f:
        json.dump(segments, f, ensure_ascii=False, indent=2)
    print(f"  ✓ 已保存 segments.json")

    # 对比上次结果（最后输出）
    if old_segments:
        compare_with_previous_data(old_segments, segments)

    print(f"\n{'='*60}")
    print(f"完成！输出目录: {episode_dir}")
    print(f"{'='*60}")


# ============================================================
# 匿名聚类
# ============================================================

def load_embedding_model():
    """加载 3D-Speaker ERes2NetV2 embedding 模型"""
    print("  加载声纹 embedding 模型...")
    from funasr import AutoModel

    model = AutoModel(
        model="iic/speech_eres2netv2_sv_zh-cn_16k-common",
        device="cuda:0",
    )
    print("  ✓ embedding 模型加载完成")
    return model


def extract_embedding(model, wav_path):
    """对单个 WAV 文件提取 embedding 向量"""
    res = model.generate(input=str(wav_path))
    embedding = res[0]["spk_embedding"]
    if hasattr(embedding, "cpu"):
        embedding = embedding.cpu().numpy()
    if isinstance(embedding, np.ndarray):
        vec = embedding.flatten()
    else:
        vec = np.array(embedding).flatten()
    norm = np.linalg.norm(vec)
    if norm > 0:
        vec = vec / norm
    return vec


def cluster_speakers(embeddings, threshold=0.30):
    """对所有切片的 embedding 做层次聚类"""
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
    """按片段数量降序分配匿名标签：角色1、角色2..."""
    from collections import Counter
    counts = Counter(labels)
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

    valid_indices = []
    embeddings = []

    for i, seg in enumerate(segments):
        duration = seg["end"] - seg["start"]
        clip_path = clips_dir / seg.get("file", "")

        if duration < min_duration or not seg.get("file") or not clip_path.exists():
            seg["cluster"] = "过短" if duration < min_duration else None
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

    label_map = assign_cluster_labels(labels)

    for idx, seg_i in enumerate(valid_indices):
        segments[seg_i]["cluster"] = label_map[labels[idx]]

    # 统计
    from collections import Counter
    cluster_counts = Counter(seg.get("cluster") for seg in segments if seg.get("cluster"))
    print(f"  ✓ 聚类完成，共 {len(cluster_counts)} 个簇:")
    for name, count in sorted(cluster_counts.items(), key=lambda x: x[1], reverse=True):
        print(f"    {name}: {count} 个片段")

    # 重命名切片文件
    rename_clips(segments, clips_dir)


def rename_clips(segments, clips_dir):
    """重命名切片文件：{序号}_{时间}_{标签}.wav"""
    rename_count = 0
    for seg in segments:
        old_file = seg.get("file")
        if not old_file:
            continue

        old_path = clips_dir / old_file
        if not old_path.exists():
            continue

        label = seg.get("speaker") or seg.get("cluster")
        if not label:
            continue

        start_str = format_time(seg["start"])
        end_str = format_time(seg["end"])
        new_file = f"{seg['index']:03d}_{start_str}-{end_str}_{label}.wav"
        new_path = clips_dir / new_file

        if old_path != new_path:
            # 避免目标已存在的冲突
            if new_path.exists() and old_path != new_path:
                new_path.unlink()
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

    for cluster_name in sorted(cluster_to_speaker.keys()):
        speaker = cluster_to_speaker[cluster_name]
        score = cluster_scores[cluster_name]
        status = f"→ {speaker} ({score:.3f})" if speaker else f"→ 未匹配 ({score:.3f})"
        print(f"    {cluster_name} {status}")

    # 重命名切片文件（用角色名替换 cluster 标签）
    rename_clips(segments, clips_dir)


# ============================================================
# --label：仅重新标注
# ============================================================

def label_only(target, config):
    """仅对已有的 segments.json 重新标注角色"""
    parts = target.split("/")
    if len(parts) == 2:
        namespace, episode = parts
        episodes = [episode]
    elif len(parts) == 1:
        namespace = parts[0]
        episodes = None
    else:
        print(f"❌ 格式错误，应为: namespace/episode 或 namespace")
        sys.exit(1)

    output_dir = Path("output") / namespace
    if not output_dir.exists():
        print(f"❌ 目录不存在: {output_dir}")
        sys.exit(1)

    if episodes:
        dirs_to_process = [output_dir / ep for ep in episodes]
    else:
        dirs_to_process = sorted([d for d in output_dir.iterdir() if d.is_dir()])

    if not dirs_to_process:
        print(f"❌ 未找到任何已处理的目录")
        sys.exit(1)

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

        label_by_cluster(segments, clips_dir, embedding_model, namespace, config)

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
    parser.add_argument("--gen-edit",
                        help="生成 edit.txt + reference.wav（指定 WAV 文件名，如: EP01.wav）")
    parser.add_argument("--apply-edit",
                        help="按 edit.txt 切分（指定 episode 名，如: EP01）")
    parser.add_argument("--no-label", action="store_true",
                        help="不做角色标注，只切分+聚类")
    parser.add_argument("--label", "-l",
                        help="仅重新标注（格式: namespace/episode 或 namespace）")
    args = parser.parse_args()

    config = load_config()

    # --label 模式
    if args.label:
        if not check_ffmpeg():
            print("❌ FFmpeg 未安装或未在 PATH 中")
            sys.exit(1)
        label_only(args.label, config)
        return

    # 需要 namespace
    if not args.namespace:
        print("❌ 请指定命名空间: --namespace 魔幻手机")
        parser.print_help()
        sys.exit(1)

    if not check_ffmpeg():
        print("❌ FFmpeg 未安装或未在 PATH 中")
        sys.exit(1)

    # --gen-edit 模式
    if args.gen_edit:
        gen_edit(args.namespace, args.gen_edit, config)
        return

    # --apply-edit 模式
    if args.apply_edit:
        apply_edit(args.namespace, args.apply_edit, config, no_label=args.no_label)
        return

    # 未指定操作
    print("❌ 请指定操作: --gen-edit 或 --apply-edit")
    print()
    print("用法:")
    print("  python process.py --namespace 魔幻手机 --gen-edit EP01.wav")
    print("  python process.py --namespace 魔幻手机 --apply-edit EP01")
    print("  python process.py --label 魔幻手机/EP01")
    sys.exit(1)


if __name__ == "__main__":
    main()
