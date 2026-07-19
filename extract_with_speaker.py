"""
《魔幻手机》剧集对话提取 - 角色识别版（纯 Embedding 比对）

流程：
1. 先运行 extract_dialogue.py 得到切分好的 WAV 片段和 segments.json
2. 本脚本对每个 WAV 片段提取声纹 embedding（3D-Speaker ERes2NetV2）
3. 与 speaker_profiles/ 中已注册的角色声纹做余弦相似度比对
4. 输出带角色标签的对话文本

模式：
  默认模式：自动识别所有片段的说话人
  --enroll：交互式标注模式，注册角色声纹
  --enroll-unknown：对未识别片段进行补充标注

使用：先运行 extract_dialogue.py，再运行本脚本。

依赖：funasr, numpy, soundfile
"""

import os
import sys
import json
import argparse
from pathlib import Path
from datetime import date

import numpy as np

# ============================================================
# 常量
# ============================================================

PROFILES_DIR = Path("speaker_profiles")
PROFILES_JSON = PROFILES_DIR / "profiles.json"
MIN_DURATION = 1.0  # 低于此时长的片段跳过（embedding 不稳定）
SIMILARITY_THRESHOLD = 0.70  # 低于此值判定为未知角色（提高严格度，避免混合音频误判）
MARGIN_THRESHOLD = 0.10  # top1 和 top2 差距小于此值判定为不确定（两人重叠时差距通常很小）
LONG_SEGMENT_DURATION = 3.5  # 超过此时长的片段视为"长片段"，适用更严格阈值
LONG_SEGMENT_THRESHOLD_BOOST = 0.06  # 长片段的相似度阈值额外提高量
LONG_SEGMENT_MARGIN_BOOST = 0.05  # 长片段的 margin 额外提高量


# ============================================================
# 工具函数
# ============================================================

def cosine_similarity(vec_a, vec_b):
    """计算两个向量的余弦相似度"""
    return float(np.dot(vec_a, vec_b) / (np.linalg.norm(vec_a) * np.linalg.norm(vec_b)))


def load_embedding_model():
    """加载 3D-Speaker ERes2NetV2 embedding 模型（通过 FunASR 接口）"""
    print("加载声纹 embedding 模型（3D-Speaker ERes2NetV2）...")
    from funasr import AutoModel

    model = AutoModel(
        model="iic/speech_eres2netv2_sv_zh-cn_16k-common",
        device="cuda:0",
    )
    print(f"✓ 模型加载完成")
    return model


def extract_embedding(model, wav_path):
    """对单个 WAV 文件提取 embedding 向量"""
    res = model.generate(input=str(wav_path))
    # FunASR 返回的 spk_embedding 是 numpy array，shape [1, 192] 或 [192]
    embedding = res[0]["spk_embedding"]
    if isinstance(embedding, np.ndarray):
        vec = embedding.flatten()
    else:
        vec = np.array(embedding).flatten()
    # 归一化为单位向量
    norm = np.linalg.norm(vec)
    if norm > 0:
        vec = vec / norm
    return vec


def load_profiles():
    """加载已注册的角色声纹"""
    if not PROFILES_DIR.exists():
        return {}, {}

    profiles = {}  # name -> embedding (numpy array)
    meta = {}  # name -> metadata dict

    if PROFILES_JSON.exists():
        with open(PROFILES_JSON, "r", encoding="utf-8") as f:
            meta = json.load(f)

    for npy_file in PROFILES_DIR.glob("*.npy"):
        name = npy_file.stem
        profiles[name] = np.load(str(npy_file))

    return profiles, meta


def save_profile(name, embedding, source_id):
    """保存或更新角色声纹"""
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)

    # 加载已有元信息
    meta = {}
    if PROFILES_JSON.exists():
        with open(PROFILES_JSON, "r", encoding="utf-8") as f:
            meta = json.load(f)

    npy_path = PROFILES_DIR / f"{name}.npy"
    today = date.today().isoformat()

    if npy_path.exists() and name in meta:
        # 增量平均：合并新样本
        existing = np.load(str(npy_path))
        count = meta[name]["sample_count"]
        updated = (existing * count + embedding) / (count + 1)
        norm = np.linalg.norm(updated)
        if norm > 0:
            updated = updated / norm
        np.save(str(npy_path), updated)
        meta[name]["sample_count"] = count + 1
        meta[name]["sources"].append(source_id)
        meta[name]["last_updated"] = today
    else:
        # 新角色
        np.save(str(npy_path), embedding)
        meta[name] = {
            "sample_count": 1,
            "sources": [source_id],
            "created_at": today,
            "last_updated": today,
        }

    with open(PROFILES_JSON, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    return meta[name]["sample_count"]


def identify_speaker(embedding, profiles, duration=0.0):
    """
    根据 embedding 判断说话人

    对长片段（> LONG_SEGMENT_DURATION）施加更严格的阈值，
    因为长片段更可能包含多人对话，embedding 是混合向量。

    返回: (角色名或标签, 最高相似度分数)
    """
    if not profiles:
        return "未知角色", 0.0

    # 动态阈值：长片段更严格
    threshold = SIMILARITY_THRESHOLD
    margin = MARGIN_THRESHOLD
    if duration > LONG_SEGMENT_DURATION:
        threshold += LONG_SEGMENT_THRESHOLD_BOOST
        margin += LONG_SEGMENT_MARGIN_BOOST

    scores = {}
    for name, profile_emb in profiles.items():
        scores[name] = cosine_similarity(embedding, profile_emb)

    sorted_scores = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    top1_name, top1_score = sorted_scores[0]
    top2_score = sorted_scores[1][1] if len(sorted_scores) > 1 else 0.0

    if top1_score < threshold:
        return "未知角色", top1_score
    if top1_score - top2_score < margin:
        return "不确定", top1_score
    return top1_name, top1_score


def format_time(seconds):
    """将秒数格式化为 XXmXXs"""
    m = int(seconds // 60)
    s = int(seconds % 60)
    return f"{m:02d}m{s:02d}s"


def get_wav_path(output_dir, episode_label, seg_index, seg):
    """根据 segment 信息查找 WAV 文件路径（支持原始命名和重命名后的格式）"""
    start_str = format_time(seg["start"])
    end_str = format_time(seg["end"])
    
    # 原始命名格式
    original_name = f"{episode_label}_{seg_index:03d}_{start_str}-{end_str}.wav"
    original_path = output_dir / episode_label / original_name
    if original_path.exists():
        return original_path

    # 重命名后的格式：[角色名]_{文本}_{集数}_{序号}_{时间}.wav
    # 或旧格式：{角色名}_{文本}_{集数}_{序号}_{时间}.wav
    # 按序号和时间戳后缀匹配
    pattern = f"_{episode_label}_{seg_index:03d}_{start_str}-{end_str}.wav"
    ep_dir = output_dir / episode_label
    if ep_dir.exists():
        for f in ep_dir.iterdir():
            if f.name.endswith(pattern):
                return f

    # 都没找到，返回原始路径（调用方会检查 exists）
    return original_path


# ============================================================
# 模式一：声纹注册
# ============================================================

def enroll_mode(args, model):
    """交互式声纹注册"""
    output_dir = Path(args.output)

    # 找到可用的 episode 目录
    ep_dirs = sorted([d for d in output_dir.iterdir() if d.is_dir()])
    if not ep_dirs:
        print("❌ 未找到任何已处理的集数目录")
        print("   请先运行: python extract_dialogue.py")
        sys.exit(1)

    # 让用户选择要标注的集
    print(f"\n可用集数目录:")
    for i, d in enumerate(ep_dirs, 1):
        print(f"  {i}. {d.name}")

    if len(ep_dirs) == 1:
        selected_dir = ep_dirs[0]
        print(f"\n自动选择: {selected_dir.name}")
    else:
        choice = input(f"\n选择集数 (1-{len(ep_dirs)}，直接回车选第1个): ").strip()
        if not choice:
            selected_dir = ep_dirs[0]
        else:
            selected_dir = ep_dirs[int(choice) - 1]

    episode_label = selected_dir.name

    # 加载 segments
    json_path = selected_dir / f"{episode_label}_segments.json"
    if not json_path.exists():
        print(f"❌ 未找到 {json_path.name}，请先运行 extract_dialogue.py")
        sys.exit(1)

    with open(json_path, "r", encoding="utf-8") as f:
        segments = json.load(f)

    # 加载已有声纹
    profiles, meta = load_profiles()
    if profiles:
        print(f"\n已注册角色: {', '.join(profiles.keys())}")
    else:
        print("\n当前无已注册角色，开始首次标注。")

    # 展示片段列表
    print(f"\n{'='*60}")
    print(f"片段列表（{episode_label}，共 {len(segments)} 句）")
    print(f"{'='*60}")

    valid_segments = []
    for i, seg in enumerate(segments, 1):
        duration = seg["end"] - seg["start"]
        if duration < MIN_DURATION:
            continue
        valid_segments.append((i, seg, duration))

    for i, seg, duration in valid_segments:
        start_str = format_time(seg["start"])
        end_str = format_time(seg["end"])
        print(f"  {i:3d}. [{start_str}-{end_str}] ({duration:.1f}s) {seg['text']}")

    # 交互式标注
    print(f"\n{'='*60}")
    print("输入格式: 先输入角色名，再输入该角色的序号（空格分隔）")
    print("示例:")
    print("  角色名> 傻妞")
    print("  序号> 1 3 40 42 43")
    print("  （然后继续下一个角色）")
    print("输入 q 退出，输入 list 查看已注册角色")
    print(f"{'='*60}")

    enrolled_count = 0
    while True:
        speaker_name = input("\n角色名> ").strip()
        if not speaker_name or speaker_name.lower() == "q":
            break
        if speaker_name.lower() == "list":
            profiles, meta = load_profiles()
            if profiles:
                print("已注册角色:")
                for name, info in meta.items():
                    print(f"  {name}: {info['sample_count']} 个样本")
            else:
                print("暂无已注册角色")
            continue

        indices_input = input("序号> ").strip()
        if not indices_input:
            print("  未输入序号，跳过")
            continue

        # 解析序号列表
        indices = []
        for part in indices_input.replace(",", " ").split():
            try:
                indices.append(int(part))
            except ValueError:
                print(f"  忽略无效序号: {part}")

        if not indices:
            print("  无有效序号")
            continue

        # 逐个处理
        for seg_idx in indices:
            if seg_idx < 1 or seg_idx > len(segments):
                print(f"  ⚠ 序号 {seg_idx} 超出范围 (1-{len(segments)})，跳过")
                continue

            seg = segments[seg_idx - 1]
            duration = seg["end"] - seg["start"]
            if duration < MIN_DURATION:
                print(f"  ⚠ 序号 {seg_idx} 时长 {duration:.1f}s 太短，跳过")
                continue

            wav_path = get_wav_path(output_dir, episode_label, seg_idx, seg)
            if not wav_path.exists():
                print(f"  ⚠ 序号 {seg_idx} WAV 文件不存在，跳过")
                continue

            # 提取 embedding
            embedding = extract_embedding(model, wav_path)
            source_id = f"{episode_label}_{seg_idx:03d}"
            count = save_profile(speaker_name, embedding, source_id)
            enrolled_count += 1
            print(f"  ✓ [{seg_idx}] → 「{speaker_name}」(累计 {count} 个样本)")

        print(f"  「{speaker_name}」本轮标注完成")

    print(f"\n本次标注完成，共注册 {enrolled_count} 个样本。")
    profiles, meta = load_profiles()
    if profiles:
        print("当前角色库:")
        for name, info in meta.items():
            print(f"  {name}: {info['sample_count']} 个样本")


# ============================================================
# 模式二：自动识别
# ============================================================

def auto_identify_mode(args, model):
    """自动识别所有片段的说话人"""
    output_dir = Path(args.output)

    # 加载声纹库
    profiles, meta = load_profiles()
    if not profiles:
        print("❌ 声纹库为空，请先运行注册模式:")
        print("   python extract_with_speaker.py --enroll")
        sys.exit(1)

    print(f"已加载 {len(profiles)} 个角色声纹: {', '.join(profiles.keys())}")

    # 扫描所有 episode 目录
    ep_dirs = sorted([d for d in output_dir.iterdir() if d.is_dir()])
    if not ep_dirs:
        print("❌ 未找到任何已处理的集数目录")
        sys.exit(1)

    total_segments = 0
    total_identified = 0
    total_unknown = 0
    total_uncertain = 0

    for ep_dir in ep_dirs:
        episode_label = ep_dir.name
        json_path = ep_dir / f"{episode_label}_segments.json"

        if not json_path.exists():
            continue

        print(f"\n{'='*60}")
        print(f"处理: {episode_label}")
        print(f"{'='*60}")

        with open(json_path, "r", encoding="utf-8") as f:
            segments = json.load(f)

        results = []
        ep_identified = 0
        ep_unknown = 0
        ep_uncertain = 0

        for i, seg in enumerate(segments, 1):
            duration = seg["end"] - seg["start"]

            if duration < MIN_DURATION:
                results.append({
                    "speaker": "未知角色",
                    "score": 0.0,
                    "start": seg["start"],
                    "end": seg["end"],
                    "text": seg["text"],
                })
                ep_unknown += 1
                continue

            wav_path = get_wav_path(output_dir, episode_label, i, seg)
            if not wav_path.exists():
                results.append({
                    "speaker": "未知角色",
                    "score": 0.0,
                    "start": seg["start"],
                    "end": seg["end"],
                    "text": seg["text"],
                })
                ep_unknown += 1
                continue

            # 提取 embedding 并比对
            embedding = extract_embedding(model, wav_path)
            speaker, score = identify_speaker(embedding, profiles, duration=duration)

            results.append({
                "speaker": speaker,
                "score": score,
                "start": seg["start"],
                "end": seg["end"],
                "text": seg["text"],
            })

            if speaker == "未知角色":
                ep_unknown += 1
            elif speaker == "不确定":
                ep_uncertain += 1
            else:
                ep_identified += 1

        # 保存带角色标签的文本
        speaker_txt_path = ep_dir / f"{episode_label}_dialogue_speaker.txt"
        with open(speaker_txt_path, "w", encoding="utf-8") as f:
            for r in results:
                f.write(f"[{r['speaker']}] {r['text']}\n")

        # 保存带分数的详细 JSON
        speaker_json_path = ep_dir / f"{episode_label}_speaker_results.json"
        with open(speaker_json_path, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)

        # 重命名切片文件：[角色名]_文本_集数_序号_时间.wav
        rename_count = 0
        for i, (seg, r) in enumerate(zip(segments, results), 1):
            old_wav = get_wav_path(output_dir, episode_label, i, seg)
            if not old_wav.exists():
                continue
            # 清理文本用于文件名：只去掉 Windows 禁止字符（\ / : * ? " < > |）
            clean_text = r["text"]
            for ch in '\\/:*?"<>|':
                clean_text = clean_text.replace(ch, "")
            clean_text = clean_text.replace(" ", "")
            start_str = format_time(seg["start"])
            end_str = format_time(seg["end"])
            new_name = f"[{r['speaker']}]_{clean_text}_{episode_label}_{i:03d}_{start_str}-{end_str}.wav"
            new_path = old_wav.parent / new_name
            if old_wav != new_path:
                old_wav.rename(new_path)
                rename_count += 1

        print(f"  ✓ 完成: {len(segments)} 句")
        print(f"    已识别: {ep_identified}  不确定: {ep_uncertain}  未知: {ep_unknown}")
        print(f"    已重命名: {rename_count} 个文件")
        print(f"    输出: {speaker_txt_path.name}")

        total_segments += len(segments)
        total_identified += ep_identified
        total_unknown += ep_unknown
        total_uncertain += ep_uncertain

    print(f"\n{'='*60}")
    print(f"全部完成！")
    print(f"  总片段数: {total_segments}")
    print(f"  已识别: {total_identified} ({total_identified/max(total_segments,1)*100:.0f}%)")
    print(f"  不确定: {total_uncertain} ({total_uncertain/max(total_segments,1)*100:.0f}%)")
    print(f"  未知: {total_unknown} ({total_unknown/max(total_segments,1)*100:.0f}%)")
    print(f"{'='*60}")


# ============================================================
# 模式三：补充注册未知角色
# ============================================================

def enroll_unknown_mode(args, model):
    """对识别结果中的未知片段进行补充标注"""
    output_dir = Path(args.output)

    # 收集所有未知/不确定的片段
    unknown_items = []

    ep_dirs = sorted([d for d in output_dir.iterdir() if d.is_dir()])
    for ep_dir in ep_dirs:
        episode_label = ep_dir.name
        results_path = ep_dir / f"{episode_label}_speaker_results.json"

        if not results_path.exists():
            continue

        with open(results_path, "r", encoding="utf-8") as f:
            results = json.load(f)

        segments_path = ep_dir / f"{episode_label}_segments.json"
        with open(segments_path, "r", encoding="utf-8") as f:
            segments = json.load(f)

        for i, (r, seg) in enumerate(zip(results, segments), 1):
            if r["speaker"] in ("未知角色", "不确定"):
                duration = seg["end"] - seg["start"]
                if duration >= MIN_DURATION:
                    wav_path = get_wav_path(output_dir, episode_label, i, seg)
                    if wav_path.exists():
                        unknown_items.append({
                            "episode": episode_label,
                            "index": i,
                            "seg": seg,
                            "wav_path": wav_path,
                            "score": r["score"],
                            "label": r["speaker"],
                        })

    if not unknown_items:
        print("✓ 没有未识别的片段，全部已标注。")
        return

    print(f"\n找到 {len(unknown_items)} 个未识别/不确定的片段:")
    print(f"{'='*60}")

    for idx, item in enumerate(unknown_items, 1):
        seg = item["seg"]
        start_str = format_time(seg["start"])
        end_str = format_time(seg["end"])
        duration = seg["end"] - seg["start"]
        print(f"  {idx:3d}. [{item['episode']}] [{start_str}-{end_str}] ({duration:.1f}s) [{item['label']}] {seg['text']}")

    print(f"\n{'='*60}")
    print("输入格式: 序号 角色名（如: 3 傻妞）")
    print("输入 q 退出")
    print(f"{'='*60}")

    enrolled_count = 0
    while True:
        user_input = input("\n> ").strip()
        if not user_input or user_input.lower() == "q":
            break

        parts = user_input.split(maxsplit=1)
        if len(parts) != 2:
            print("  格式错误，请输入: 序号 角色名")
            continue

        try:
            idx = int(parts[0])
            speaker_name = parts[1]
        except ValueError:
            print("  序号必须是数字")
            continue

        if idx < 1 or idx > len(unknown_items):
            print(f"  序号超出范围 (1-{len(unknown_items)})")
            continue

        item = unknown_items[idx - 1]

        # 提取 embedding
        print(f"  提取声纹中...")
        embedding = extract_embedding(model, item["wav_path"])

        # 保存
        source_id = f"{item['episode']}_{item['index']:03d}"
        count = save_profile(speaker_name, embedding, source_id)
        enrolled_count += 1
        print(f"  ✓ 已注册「{speaker_name}」(累计 {count} 个样本)")

    if enrolled_count > 0:
        print(f"\n补充标注完成，共注册 {enrolled_count} 个样本。")
        print("建议重新运行自动识别模式以更新结果:")
        print("  python extract_with_speaker.py")


# ============================================================
# 主入口
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="《魔幻手机》角色识别（3D-Speaker ERes2NetV2 声纹比对）"
    )
    parser.add_argument("--output", "-o", default="output",
                        help="基础版的输出目录（默认: output）")
    parser.add_argument("--enroll", action="store_true",
                        help="进入声纹注册模式（交互式标注）")
    parser.add_argument("--enroll-unknown", action="store_true",
                        help="对未识别片段进行补充标注")
    args = parser.parse_args()

    # 检查输出目录
    output_dir = Path(args.output)
    if not output_dir.exists():
        print(f"❌ 错误: 输出目录不存在: {output_dir}")
        print("   请先运行: python extract_dialogue.py")
        sys.exit(1)

    # 加载 embedding 模型
    model = load_embedding_model()

    # 根据模式执行
    if args.enroll:
        enroll_mode(args, model)
    elif args.enroll_unknown:
        enroll_unknown_mode(args, model)
    else:
        auto_identify_mode(args, model)


if __name__ == "__main__":
    main()
