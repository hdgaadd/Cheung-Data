"""
Cheung-Data v2 - 导出脚本

从 output/{namespace}/ 读取 segments.json，按角色分组导出到 export/{namespace}/{speaker}/

用法：
  python export.py --namespace 魔幻手机

依赖：无额外依赖（仅 Python 标准库）
"""

import sys
import json
import shutil
import argparse
from pathlib import Path
from collections import defaultdict


# ============================================================
# 配置
# ============================================================

def load_config():
    """加载 config.json"""
    config_path = Path("config.json")
    if not config_path.exists():
        return {"export_path": "export"}
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


# ============================================================
# 导出逻辑
# ============================================================

def export_namespace(namespace, config):
    """导出指定命名空间的数据"""
    output_dir = Path("output") / namespace
    export_base = Path(config.get("export_path", "export")) / namespace

    if not output_dir.exists():
        print(f"❌ 输出目录不存在: {output_dir}")
        print(f"   请先运行: python process.py --namespace {namespace}")
        sys.exit(1)

    # 扫描所有 episode 目录
    episode_dirs = sorted([d for d in output_dir.iterdir() if d.is_dir()])
    if not episode_dirs:
        print(f"❌ 未找到任何已处理的目录: {output_dir}")
        sys.exit(1)

    # 收集所有有效标注的片段，按 speaker 分组
    speaker_items = defaultdict(list)  # speaker -> [(text, source, clip_path)]

    for ep_dir in episode_dirs:
        segments_path = ep_dir / "segments.json"
        clips_dir = ep_dir / "clips"

        if not segments_path.exists():
            continue

        with open(segments_path, "r", encoding="utf-8") as f:
            segments = json.load(f)

        for seg in segments:
            speaker = seg.get("speaker")
            # 跳过无效标注
            if not speaker:
                continue
            # 跳过匿名标签（角色1、角色2...）
            if speaker.startswith("角色"):
                continue

            clip_file = seg.get("file")
            if not clip_file:
                continue

            clip_path = clips_dir / clip_file
            if not clip_path.exists():
                continue

            source = f"{ep_dir.name}/{seg.get('index', 0):03d}"
            speaker_items[speaker].append({
                "text": seg["text"],
                "source": source,
                "clip_path": clip_path,
            })

    if not speaker_items:
        print(f"❌ 未找到有效标注数据。请确认已完成角色标注:")
        print(f"   python process.py --namespace {namespace}")
        print(f"   或: python process.py --label {namespace}")
        sys.exit(1)

    # 统计
    total_items = sum(len(items) for items in speaker_items.values())
    print(f"命名空间: {namespace}")
    print(f"角色数: {len(speaker_items)}")
    print(f"总片段数: {total_items}")
    print()

    # 清空并重建导出目录
    if export_base.exists():
        shutil.rmtree(export_base)

    # 按角色导出
    for speaker, items in sorted(speaker_items.items()):
        speaker_dir = export_base / speaker
        speaker_dir.mkdir(parents=True, exist_ok=True)

        lines = []
        for idx, item in enumerate(items, 1):
            item_id = f"{idx:03d}"
            dest_filename = f"{item_id}.wav"
            dest_path = speaker_dir / dest_filename

            # 复制 WAV 文件
            shutil.copy2(item["clip_path"], dest_path)

            lines.append({
                "id": item_id,
                "text": item["text"],
                "file": dest_filename,
                "source": item["source"],
            })

        # 保存 lines.json
        lines_path = speaker_dir / "lines.json"
        with open(lines_path, "w", encoding="utf-8") as f:
            json.dump(lines, f, ensure_ascii=False, indent=2)

        print(f"  {speaker}: {len(items)} 条 → {speaker_dir}")

    print(f"\n{'='*60}")
    print(f"导出完成！")
    print(f"目录: {export_base.resolve()}")
    print(f"{'='*60}")


# ============================================================
# 入口
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="Cheung-Data v2 - 导出"
    )
    parser.add_argument("--namespace", "-n", required=True,
                        help="命名空间（如: 魔幻手机）")
    args = parser.parse_args()

    config = load_config()
    export_namespace(args.namespace, config)


if __name__ == "__main__":
    main()
