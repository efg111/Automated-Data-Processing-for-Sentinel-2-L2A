#!/usr/bin/env python3
"""Batch entry point for recursively processing many Sentinel-2 L2A SAFE packages.

The processing functions live in sentinel_l2a_pipeline.py.  This script only
discovers direct SAFE packages, invokes the same processing path sequentially,
and writes a batch-level manifest.  It deliberately does not process scenes in
parallel so several large 10 m products cannot exhaust disk space or memory.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import sentinel_l2a_pipeline as pipeline


def discover_safe_packages(input_root: Path) -> list[Path]:
    """Find only SAFE folders that directly contain GRANULE, without duplicates."""
    if not input_root.exists():
        raise FileNotFoundError(f"批量输入根目录不存在: {input_root}")
    candidates: set[Path] = set()
    if input_root.suffix.upper() == ".SAFE" and (input_root / "GRANULE").is_dir():
        candidates.add(input_root.resolve())
    if input_root.is_dir():
        for candidate in input_root.rglob("*.SAFE"):
            if (candidate / "GRANULE").is_dir():
                candidates.add(candidate.resolve())
    return sorted(candidates, key=lambda path: str(path).lower())


def load_batch_config(config_path: Path, input_root: Path, output_root: Path) -> dict[str, Any]:
    config = pipeline.load_config(config_path.resolve())
    # Command-line roots are intentionally authoritative for this batch run;
    # config's input_paths is retained for single-scene processing only.
    config["input_paths"] = [str(input_root.resolve())]
    config["output_root"] = str(output_root.resolve())
    return config


def main() -> int:
    parser = argparse.ArgumentParser(
        description="递归批量处理 Sentinel-2 L2A SAFE，并调用主处理流程"
    )
    parser.add_argument("--input-root", required=True, type=Path,
                        help="包含一个或多个 .SAFE 目录的上级目录")
    parser.add_argument("--output-root", required=True, type=Path,
                        help="批量成果根目录；不要放在某个 .SAFE 内")
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("config.json"),
                        help="处理参数 JSON；默认与本脚本同目录的 config.json")
    parser.add_argument("--skip-existing", action="store_true",
                        help="若该景已有 05_final/calibrated_cloudmasked.tif 则跳过")
    parser.add_argument("--verbose", action="store_true", help="在终端显示逐景处理日志")
    args = parser.parse_args()

    input_root = args.input_root.resolve()
    output_root = args.output_root.resolve()
    config = load_batch_config(args.config, input_root, output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    log_file = output_root / "logs" / "batch_sentinel_l2a.log"
    logger = pipeline.build_logger(log_file, args.verbose)
    logger.info("批量输入根目录: %s", input_root)
    logger.info("批量输出根目录: %s", output_root)

    safe_packages = discover_safe_packages(input_root)
    if not safe_packages:
        logger.error("未发现直接包含 GRANULE 的 .SAFE 目录: %s", input_root)
        return 2
    logger.info("发现 %d 个 SAFE 产品", len(safe_packages))

    selected = {str(band).upper() for band in config.get("bands", [])}
    if not selected or "SCL" not in selected:
        raise ValueError("配置 bands 必须至少包含一个光谱波段和 SCL")
    preferred = {str(name).upper(): int(resolution)
                 for name, resolution in config.get("band_source_resolution", {}).items()}
    # Passing the concrete SAFE paths avoids the double-SAFE unzip layout from
    # being counted twice and preserves multi-granule support.
    scenes = pipeline.discover_scenes([str(path) for path in safe_packages], selected, preferred, logger)
    if not scenes:
        logger.error("SAFE 内未发现可处理的选定波段")
        return 2

    completed: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    failures: list[dict[str, str]] = []
    for position, scene in enumerate(scenes, start=1):
        final_path = output_root / scene.scene_id / "05_final" / "calibrated_cloudmasked.tif"
        if args.skip_existing and final_path.is_file():
            logger.info("[%d/%d] 跳过已有成果: %s", position, len(scenes), scene.scene_id)
            skipped.append({"scene": scene.scene_id, "final": str(final_path)})
            continue
        logger.info("[%d/%d] 开始处理: %s", position, len(scenes), scene.scene_id)
        try:
            completed.append(pipeline.process_scene(scene, config, logger))
        except Exception as exc:  # one bad SAFE must not stop the batch
            logger.exception("批量处理失败: %s: %s", scene.scene_id, exc)
            failures.append({"scene": scene.scene_id, "error": str(exc)})

    manifest = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "input_root": str(input_root),
        "output_root": str(output_root),
        "processing_config": str(args.config.resolve()),
        "safe_products_found": len(safe_packages),
        "scenes_found": len(scenes),
        "success": completed,
        "skipped": skipped,
        "failed": failures,
    }
    manifest_path = output_root / "batch_processing_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("批量清单: %s", manifest_path)
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"致命错误: {error}", file=sys.stderr)
        raise SystemExit(2)
