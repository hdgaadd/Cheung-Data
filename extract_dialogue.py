"""
《魔幻手机》剧集对话提取与音频切分工具

功能：
1. 扫描 videos/ 目录下的 .mp4 文件
2. 优先使用 UVR GUI 手动生成的人声 WAV；若无则 FFmpeg 提取原始音频
3. FunASR Paraformer-large 识别中文对话，输出带强制对齐时间戳的 segments
4. 输出纯文本对话记录 + JSON
5. 按句切分为独立 WAV 文件

依赖：
- funasr (pip install funasr)
- FFmpeg (需在系统 PATH 中)
"""

import os
import re
import subprocess
import argparse
import sys
import json
import shutil
from pathlib import Path


# 高频专有名词纠错表（ASR 容易听错的固定模式）
CORRECTIONS = {
    "闪妞": "傻妞",
    "山妞": "傻妞",
    "小剑哥哥": "小千哥哥",
    "小剑": "小千",
    "画面条": "化梅那条",
    "画眉": "化梅",
    "华梅": "化梅",
    "向冷光明": "向往光明",
    "教育出来": "叫你出来",
}

# FunASR 热词：角色名和常见专有名词/易错短语
HOTWORDS = (
    "傻妞 陆小千 小千哥哥 黄眉大王 游所为 化梅 猪八戒 孙悟空 楚楚 魔幻手机 "
    "没有把握 我喜欢人家 人家不喜欢我 叫你出来 捋不出 理智 向往光明 "
    "救过你 手机的主人 仅此而已 楚楚姐姐"
)


def post_correct(text):
    """对识别文本做后处理纠错，修正高频专有名词错误"""
    for wrong, right in CORRECTIONS.items():
        text = text.replace(wrong, right)
    return text


def refresh_path():
    """刷新 PATH 环境变量（Windows 新安装的程序可能不在当前 shell PATH 中）"""
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


def extract_episode_number(filename):
    """从文件名中提取集数编号"""
    patterns = [
        r'[Ee][Pp]?(\d+)',
        r'第(\d+)[集话]',
        r'[_\-\s](\d+)[_\-\s.]',
        r'^(\d+)',
    ]
    stem = Path(filename).stem
    for pattern in patterns:
        match = re.search(pattern, stem)
        if match:
            return int(match.group(1))
    return None


def extract_audio(video_path, audio_path):
    """用 FFmpeg 从视频中提取音频为 16kHz 单声道 WAV"""
    print(f"  [1/3] 提取音频: {video_path.name}")
    cmd = [
        "ffmpeg", "-y",
        "-i", str(video_path),
        "-vn",
        "-acodec", "pcm_s16le",
        "-ar", "16000",
        "-ac", "1",
        str(audio_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  ❌ 音频提取失败: {result.stderr[-200:]}")
        return False
    return True


def find_vocals_wav(video_path):
    """
    查找与视频对应的人声 WAV 文件（由 UVR GUI 手动生成）。
    UVR 输出格式示例：1_bandicam 2026-07-12 20-24-44-610_(Vocals).wav
    查找规则：在 videos/ 目录下找包含视频文件名且包含 Vocals 的 WAV。
    """
    video_dir = video_path.parent
    video_stem = video_path.stem

    for wav_file in video_dir.glob("*.wav"):
        if video_stem in wav_file.stem and "Vocal" in wav_file.stem:
            return wav_file

    return None


def merge_vad_fragments(segments, max_gap=1.0):
    """
    合并被 VAD 错误切断的片段。
    
    规则：如果前一段不以句末标点（。？！）结尾，且与下一段间隔 < max_gap 秒，
    说明前一段是同一句话的中间部分（被 VAD 在停顿处切断了），应该合并。
    
    合并后总时长不超过 30 秒（避免无限合并）。
    """
    sentence_ends = set("。？！；…")

    if not segments:
        return segments

    merged = [segments[0]]
    for seg in segments[1:]:
        prev = merged[-1]
        gap = seg["start"] - prev["end"]
        combined_duration = seg["end"] - prev["start"]

        # 前一段最后一个字符（去掉空白后）
        prev_last_char = prev["text"].rstrip()[-1] if prev["text"].rstrip() else ""

        # 合并条件：前一段没有以句末标点结尾 + 间隔小 + 合并后不太长
        if prev_last_char not in sentence_ends and gap < max_gap and combined_duration <= 30.0:
            merged[-1] = {
                "start": prev["start"],
                "end": seg["end"],
                "text": prev["text"] + seg["text"],
            }
        else:
            merged.append(seg)

    return merged


def split_by_silence(text, timestamps, silence_threshold_ms=300, max_duration_s=15.0):
    """
    按句末标点（。？！）切分文本为句子。
    
    核心逻辑：
    - 遇到句末标点（。？！；）就切分 → 一句一段
    - 逗号不切分 → 同一人的完整语句保持在一起
    - 单个片段超过 max_duration_s → 按逗号二次切分（兜底）
    
    text: 完整识别文本（含标点）
    timestamps: 字级时间戳 [[start_ms, end_ms], ...]，标点不占位置
    
    返回: [{start, end, text}, ...]
    """
    # 标点符号集合（不占 timestamp 位置）
    punctuation = set("。？！；…，,、：:""''「」（）()《》")
    # 句末标点：遇到这些就切分
    sentence_ends = set("。？！；…")

    # 建立字符到 timestamp 的映射
    char_info = []  # [(char, ts_index or None)]
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

    for i, (char, ts_i) in enumerate(char_info):
        current_chars.append(char)
        if ts_i is not None:
            if current_start is None:
                current_start = timestamps[ts_i][0]
            current_end = timestamps[ts_i][1]

        # 在句末标点处切分
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

    # 处理末尾（没有句末标点的残余文本）
    segment_text = "".join(current_chars).strip()
    if segment_text and current_start is not None:
        segments.append({
            "start": current_start / 1000.0,
            "end": current_end / 1000.0,
            "text": segment_text,
        })

    # 对超长片段按逗号二次切分（兜底）
    final_segments = []
    for seg in segments:
        duration = seg["end"] - seg["start"]
        if duration <= max_duration_s:
            final_segments.append(seg)
        else:
            sub = split_long_by_comma(seg)
            final_segments.extend(sub)

    return final_segments


def split_long_by_comma(seg):
    """对超长片段按逗号二次切分，时间按字数比例分配。"""
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


def transcribe_audio(audio_path, model):
    """使用 FunASR 识别音频，返回 segments 列表"""
    print(f"  [2/3] 语音识别中（FunASR Paraformer-large）...")

    res = model.generate(
        input=str(audio_path),
        batch_size_s=300,
        batch_size_threshold_s=60,
        hotword=HOTWORDS,
    )

    # 解析 FunASR 输出
    segment_list = []

    for item in res:
        if "sentence_info" in item:
            # 有 sentence_info 时直接用（带句级时间戳）
            for sent in item["sentence_info"]:
                text = sent["text"].strip()
                if text:
                    start = sent["start"] / 1000.0
                    end = sent["end"] / 1000.0
                    segment_list.append({
                        "start": start,
                        "end": end,
                        "text": text,
                    })
        elif "text" in item and "timestamp" in item and item["timestamp"]:
            # 只有字级 timestamp 时，按句末标点切分
            text = item["text"]
            timestamps = item["timestamp"]
            sentences = split_by_silence(text, timestamps)
            segment_list.extend(sentences)
        elif "text" in item:
            text = item["text"].strip()
            if text:
                segment_list.append({
                    "start": 0.0,
                    "end": 0.0,
                    "text": text,
                })

    # 合并被 VAD 错误切断的片段：
    # 如果前一段不以句末标点结尾，且与下一段间隔较小，则合并
    segment_list = merge_vad_fragments(segment_list)

    # 后处理纠错
    for seg in segment_list:
        seg["text"] = post_correct(seg["text"])

    # 过滤极短片段（< 1 秒）
    segment_list = [seg for seg in segment_list if (seg["end"] - seg["start"]) >= 1.0]

    # 保存原始结果供参考（合并前）
    raw_segments = list(segment_list)
    raw_count = len(segment_list)

    # 合并短句
    segment_list = merge_short_segments(segment_list)
    print(f"  ✓ 识别完成，原始 {raw_count} 句 → 合并后 {len(segment_list)} 句")

    return segment_list, raw_segments


def merge_short_segments(segments, max_gap=0.3, min_chars=4):
    """
    合并过短的相邻片段。
    规则：
    - 前一句少于 min_chars 个字
    - 与下一句间隔小于 max_gap 秒
    - 合并后总时长不超过 15 秒
    """
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


def format_time(seconds):
    """将秒数格式化为 XXmXXs 格式"""
    m = int(seconds // 60)
    s = int(seconds % 60)
    return f"{m:02d}m{s:02d}s"


def split_audio_segments(source_audio, segments, output_dir, episode_label):
    """按 segments 时间戳从音频切分为独立 WAV 文件"""
    print(f"  [3/3] 切分音频片段...")
    count = 0
    for i, seg in enumerate(segments, 1):
        start_str = format_time(seg["start"])
        end_str = format_time(seg["end"])
        filename = f"{episode_label}_{i:03d}_{start_str}-{end_str}.wav"
        output_path = output_dir / filename

        duration = seg["end"] - seg["start"] + 0.3  # 末尾加 0.3 秒缓冲
        cmd = [
            "ffmpeg", "-y",
            "-i", str(source_audio),
            "-ss", str(seg["start"]),
            "-t", str(duration),
            "-vn",
            "-acodec", "pcm_s16le",
            "-ar", "16000",
            "-ac", "1",
            str(output_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            count += 1
        else:
            print(f"    ⚠ 切分第 {i} 句失败")

    print(f"  ✓ 切分完成，共生成 {count} 个音频文件")
    return count


def save_dialogue_text(segments, raw_segments, output_dir, episode_label):
    """保存对话文本为纯文本文件，同时保存 JSON 和原始识别结果"""
    # 纯文本（合并后）
    text_path = output_dir / f"{episode_label}_dialogue.txt"
    with open(text_path, "w", encoding="utf-8") as f:
        for seg in segments:
            f.write(seg["text"] + "\n")
    print(f"  ✓ 对话文本已保存: {text_path.name}")

    # 原始识别结果（未合并，带时间戳）
    raw_path = output_dir / f"{episode_label}_raw.txt"
    with open(raw_path, "w", encoding="utf-8") as f:
        for seg in raw_segments:
            start_str = format_time(seg["start"])
            end_str = format_time(seg["end"])
            f.write(f"[{start_str}-{end_str}] {seg['text']}\n")

    # JSON（含时间戳，供角色版读取）
    json_path = output_dir / f"{episode_label}_segments.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(segments, f, ensure_ascii=False, indent=2)

    return text_path


def process_video(video_path, output_base, model, episode_label):
    """处理单个视频文件的完整流程"""
    print(f"\n{'='*60}")
    print(f"处理: {video_path.name} → {episode_label}")
    print(f"{'='*60}")

    # 创建输出目录
    output_dir = output_base / episode_label

    # 清空旧文件
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # 临时音频文件路径
    temp_audio = output_dir / f"{episode_label}_temp.wav"

    try:
        # 检查是否有 UVR GUI 生成的人声 WAV 文件
        vocals_wav = find_vocals_wav(video_path)
        if vocals_wav:
            print(f"  ✓ 检测到人声文件: {vocals_wav.name}")
            # 转换为 16kHz mono（FunASR 输入格式）
            temp_audio_converted = output_dir / f"{episode_label}_vocals_16k.wav"
            cmd = [
                "ffmpeg", "-y",
                "-i", str(vocals_wav),
                "-ar", "16000", "-ac", "1",
                "-acodec", "pcm_s16le",
                str(temp_audio_converted),
            ]
            subprocess.run(cmd, capture_output=True, text=True)
            audio_source = temp_audio_converted
        else:
            # 没有人声文件，从视频提取原始音频
            if not extract_audio(video_path, temp_audio):
                return False
            audio_source = temp_audio
            print(f"  ⚠ 未找到人声 WAV，使用原始音频（建议用 UVR GUI 先分离人声放到 videos/ 目录）")

        # 步骤2：语音识别
        segments, raw_segments = transcribe_audio(audio_source, model)
        if not segments:
            print("  ⚠ 未识别到任何对话")
            return False

        # 步骤3a：保存文本
        save_dialogue_text(segments, raw_segments, output_dir, episode_label)

        # 步骤3b：切分音频
        split_audio_segments(audio_source, segments, output_dir, episode_label)

        return True

    finally:
        # 清理临时文件
        if temp_audio.exists():
            temp_audio.unlink()
        temp_converted = output_dir / f"{episode_label}_vocals_16k.wav"
        if temp_converted.exists():
            temp_converted.unlink()
        print(f"  ✓ 临时文件已清理")


def main():
    parser = argparse.ArgumentParser(
        description="《魔幻手机》剧集对话提取与音频切分工具（FunASR 版）"
    )
    parser.add_argument(
        "--input", "-i",
        default="videos",
        help="MP4 视频文件所在目录（默认: videos）",
    )
    parser.add_argument(
        "--output", "-o",
        default="output",
        help="输出目录（默认: output）",
    )
    args = parser.parse_args()

    # 检查 FFmpeg
    if not check_ffmpeg():
        print("❌ 错误: FFmpeg 未安装或未在 PATH 中")
        print("   请安装 FFmpeg: https://ffmpeg.org/download.html")
        sys.exit(1)

    # 检查输入目录
    input_dir = Path(args.input)
    if not input_dir.exists():
        print(f"❌ 错误: 输入目录不存在: {input_dir}")
        sys.exit(1)

    # 扫描 MP4 文件
    video_files = sorted(input_dir.glob("*.mp4"))
    if not video_files:
        print(f"❌ 错误: 在 {input_dir} 中未找到 .mp4 文件")
        sys.exit(1)

    print(f"找到 {len(video_files)} 个视频文件:")
    for f in video_files:
        print(f"  - {f.name}")

    # 创建输出目录
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    # 加载 FunASR 模型
    print(f"\n加载 FunASR 模型（首次运行需从 ModelScope 下载）...")
    from funasr import AutoModel

    model = AutoModel(
        model="iic/speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
        model_revision="v2.0.4",
        vad_model="fsmn-vad",
        vad_kwargs={"max_single_segment_time": 60000},
        punc_model="ct-punc",
        device="cuda:0",
    )
    print("✓ 模型加载完成")

    # 逐个处理视频
    success_count = 0
    for idx, video_path in enumerate(video_files):
        ep_num = extract_episode_number(video_path.name)
        if ep_num is not None:
            episode_label = f"EP{ep_num:02d}"
        else:
            episode_label = f"EP{idx + 1:02d}"

        if process_video(video_path, output_dir, model, episode_label):
            success_count += 1

    # 汇总
    print(f"\n{'='*60}")
    print(f"全部完成！成功处理 {success_count}/{len(video_files)} 集")
    print(f"输出目录: {output_dir.resolve()}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
