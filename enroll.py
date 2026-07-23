"""
Cheung-Data v2 - 声纹注册

从 speaker_sources/{namespace}/{speaker_name}/*.wav 提取 embedding，
全量平均后保存为 speaker_profiles/{namespace}/{speaker_name}.npy

用法：
  python enroll.py --namespace 魔幻手机                 # 该空间下所有角色
  python enroll.py --namespace 魔幻手机 --speaker 傻妞   # 单个角色

依赖：funasr, numpy, soundfile
"""

import sys
import json
import argparse
from pathlib import Path
from datetime import date

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
# 常量
# ============================================================

MIN_DURATION = 1.0  # 低于此时长的素材跳过
MIN_RMS_ENERGY = 0.005  # 低于此 RMS 能量的素材跳过（静音检测）


# ============================================================
# 工具函数
# ============================================================

def load_embedding_model():
    """加载 3D-Speaker ERes2NetV2 embedding 模型"""
    print("加载声纹 embedding 模型...")
    from funasr import AutoModel

    model = AutoModel(
        model="iic/speech_eres2netv2_sv_zh-cn_16k-common",
        device="cuda:0",
    )
    print("✓ 模型加载完成")
    return model


def extract_embedding(model, wav_path):
    """对单个 WAV 文件提取 embedding 向量"""
    res = model.generate(input=str(wav_path))
    embedding = res[0]["spk_embedding"]
    # 处理 torch tensor（可能在 GPU 上）
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


def check_audio_quality(wav_path):
    """
    检查音频质量：时长和能量。
    返回 (is_valid, reason)
    """
    try:
        data, sr = sf.read(str(wav_path))
    except Exception as e:
        return False, f"读取失败: {e}"

    # 时长检查
    duration = len(data) / sr
    if duration < MIN_DURATION:
        return False, f"时长过短 ({duration:.1f}s < {MIN_DURATION}s)"

    # RMS 能量检查（静音检测）
    rms = np.sqrt(np.mean(data ** 2))
    if rms < MIN_RMS_ENERGY:
        return False, f"能量过低 (RMS={rms:.5f})，可能是静音"

    return True, f"OK (时长={duration:.1f}s, RMS={rms:.4f})"


# ============================================================
# 注册逻辑
# ============================================================

def enroll_speaker(model, namespace, speaker_name):
    """注册单个角色的声纹"""
    sources_dir = Path("speaker_sources") / namespace / speaker_name
    profiles_dir = Path("speaker_profiles") / namespace

    if not sources_dir.exists():
        print(f"  ❌ 素材目录不存在: {sources_dir}")
        return False

    wav_files = sorted(sources_dir.glob("*.wav"))
    if not wav_files:
        print(f"  ❌ 未找到 WAV 文件: {sources_dir}")
        return False

    print(f"\n  注册「{speaker_name}」({len(wav_files)} 个素材文件)")

    # 逐个提取 embedding（有质量检查）
    embeddings = []
    skipped = 0
    for wav_path in wav_files:
        is_valid, reason = check_audio_quality(wav_path)
        if not is_valid:
            print(f"    ⚠ 跳过 {wav_path.name}: {reason}")
            skipped += 1
            continue

        emb = extract_embedding(model, wav_path)
        embeddings.append(emb)

    if not embeddings:
        print(f"  ❌ 无有效素材（全部被跳过）")
        return False

    # 全量平均 + 归一化
    avg_embedding = np.mean(embeddings, axis=0)
    avg_embedding = avg_embedding / np.linalg.norm(avg_embedding)

    # 保存 .npy
    profiles_dir.mkdir(parents=True, exist_ok=True)
    npy_path = profiles_dir / f"{speaker_name}.npy"
    np.save(str(npy_path), avg_embedding)

    # 更新 profiles.json
    profiles_json = profiles_dir / "profiles.json"
    meta = {}
    if profiles_json.exists():
        with open(profiles_json, "r", encoding="utf-8") as f:
            meta = json.load(f)

    today = date.today().isoformat()
    meta[speaker_name] = {
        "sample_count": len(embeddings),
        "total_files": len(wav_files),
        "skipped_files": skipped,
        "sources_dir": str(sources_dir),
        "created_at": meta.get(speaker_name, {}).get("created_at", today),
        "last_updated": today,
    }

    with open(profiles_json, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    print(f"  ✓ 「{speaker_name}」注册完成: {len(embeddings)} 个有效样本"
          f"{'（跳过 ' + str(skipped) + ' 个）' if skipped else ''}")
    return True


def enroll_namespace(model, namespace, speaker=None):
    """注册命名空间下的角色"""
    sources_dir = Path("speaker_sources") / namespace

    if not sources_dir.exists():
        raise RuntimeError(f"素材目录不存在: {sources_dir}\n请创建目录并放入角色 WAV 片段:\nspeaker_sources/{namespace}/{{角色名}}/*.wav")

    if speaker:
        # 单个角色
        speakers = [speaker]
    else:
        # 扫描所有子目录
        speakers = sorted([d.name for d in sources_dir.iterdir() if d.is_dir()])

    if not speakers:
        raise RuntimeError(f"未找到任何角色目录: {sources_dir}")

    print(f"命名空间: {namespace}")
    print(f"待注册角色: {', '.join(speakers)}")

    success_count = 0
    for name in speakers:
        if enroll_speaker(model, namespace, name):
            success_count += 1

    print(f"\n{'='*60}")
    print(f"注册完成: {success_count}/{len(speakers)} 个角色")
    print(f"声纹库: speaker_profiles/{namespace}/")
    print(f"{'='*60}")


# ============================================================
# 入口
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="Cheung-Data v2 - 声纹注册"
    )
    parser.add_argument("--namespace", "-n", required=True,
                        help="命名空间（如: 魔幻手机）")
    parser.add_argument("--speaker", "-s",
                        help="指定单个角色（如: 傻妞）")
    args = parser.parse_args()

    model = load_embedding_model()
    enroll_namespace(model, args.namespace, args.speaker)


if __name__ == "__main__":
    main()
