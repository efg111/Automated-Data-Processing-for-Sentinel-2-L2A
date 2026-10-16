#!/usr/bin/env python3
"""Configurable, batch Sentinel-2 L2A preprocessing without ENVI or ArcMap.

Requires: rasterio >= 1.3, numpy.  Rasterio must have JPEG2000 support for
Sentinel-2 JP2 files (the official PyPI Windows wheels do).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import os
import re
import shutil
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np


BAND_RE = re.compile(r"_(B(?:0[1-9]|1[0-2]|8A)|SCL)(?:_(\d+)m)?$", re.I)
IMAGE_SUFFIXES = {".jp2", ".tif", ".tiff", ".img", ".dat"}


@dataclass(frozen=True)
class BandFile:
    name: str
    path: Path
    nominal_resolution: int | None


@dataclass(frozen=True)
class Scene:
    scene_id: str
    root: Path
    bands: dict[str, BandFile]


def load_config(config_path: Path) -> dict[str, Any]:
    with config_path.open(encoding="utf-8") as handle:
        config = json.load(handle)
    required = {"input_paths", "output_root"}
    missing = required - config.keys()
    if missing:
        raise ValueError(f"配置缺少字段: {', '.join(sorted(missing))}")
    base = config_path.parent
    config["input_paths"] = [str((base / Path(p)).resolve()) if not Path(p).is_absolute() else str(Path(p))
                             for p in config["input_paths"]]
    out = Path(config["output_root"])
    config["output_root"] = str((base / out).resolve()) if not out.is_absolute() else str(out)
    return config


def logical_name(path: Path) -> tuple[str, int | None] | None:
    if path.suffix.lower() not in IMAGE_SUFFIXES:
        return None
    match = BAND_RE.search(path.stem)
    if not match:
        return None
    return match.group(1).upper(), int(match.group(2)) if match.group(2) else None


def make_scene_id(granule: Path, safe: Path | None) -> str:
    """Create a traceable but ENVI-safe short scene directory name.

    Repeating the complete SAFE and granule names in both directory and file
    names can exceed classic Windows/ENVI path limits.  Mission + tile +
    acquisition timestamp uniquely identifies a normal Sentinel product.
    """
    safe_name = safe.stem if safe else ""
    match = re.search(r"(S2[AB])_MSIL2A_(\d{8}T\d{6}).*_T(\d{2}[A-Z]{3})", safe_name, re.I)
    if match:
        return f"{match.group(1).upper()}_T{match.group(3).upper()}_{match.group(2)}"
    prefix = safe_name or "input"
    value = f"{prefix}_{granule.name}" if granule.name != prefix else prefix
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value)[:80]


def find_granule_dirs(entry: Path) -> list[tuple[Path, Path | None]]:
    """Return (granule_root, SAFE_root) tuples from a SAFE, root, or band."""
    if entry.is_file():
        # .../GRANULE/<name>/IMG_DATA/R10m/file.jp2 -> granule is parents[2].
        candidate = entry.parent
        while candidate != candidate.parent and candidate.name != "GRANULE":
            if (candidate / "IMG_DATA").is_dir():
                return [(candidate, next((p for p in candidate.parents if p.suffix.upper() == ".SAFE"), None))]
            candidate = candidate.parent
        return [(entry.parent, next((p for p in entry.parents if p.suffix.upper() == ".SAFE"), None))]
    if (entry / "GRANULE").is_dir():
        safe = entry if entry.suffix.upper() == ".SAFE" else next((p for p in [entry, *entry.parents] if p.suffix.upper() == ".SAFE"), None)
        return [(p, safe) for p in (entry / "GRANULE").glob("*") if (p / "IMG_DATA").is_dir()]
    # Some download/unzip tools create SAFE/SAFE/GRANULE.  The recursive
    # branch below deliberately supports this layout as well.
    granules = [p for p in entry.rglob("*") if p.is_dir() and (p / "IMG_DATA").is_dir()]
    if granules:
        return [(p, next((q for q in p.parents if q.suffix.upper() == ".SAFE"), None)) for p in granules]
    return [(entry, None)]


def discover_scenes(input_paths: Iterable[str], selected: set[str],
                    preferred_resolutions: dict[str, int], logger: logging.Logger) -> list[Scene]:
    found: list[Scene] = []
    seen_roots: set[Path] = set()
    for input_name in input_paths:
        entry = Path(input_name)
        if not entry.exists():
            logger.error("输入路径不存在: %s", entry)
            continue
        for root, safe in find_granule_dirs(entry):
            root = root.resolve()
            if root in seen_roots:
                continue
            seen_roots.add(root)
            candidates = root.rglob("*") if root.is_dir() else []
            bands: dict[str, BandFile] = {}
            for item in candidates:
                if not item.is_file():
                    continue
                parsed = logical_name(item)
                if not parsed:
                    continue
                band, resolution = parsed
                if band not in selected:
                    continue
                # Sentinel commonly supplies B02/B03/B04 at both 10 m and
                # 20 m.  Use the configured source resolution deterministically.
                existing = bands.get(band)
                preferred = preferred_resolutions.get(band)
                def rank(value: int | None) -> tuple[int, int]:
                    return (0 if preferred is not None and value == preferred else 1,
                            value if value is not None else 999999)
                if existing is None or rank(resolution) < rank(existing.nominal_resolution):
                    bands[band] = BandFile(band, item, resolution)
            if bands:
                found.append(Scene(make_scene_id(root, safe), root, bands))
            else:
                logger.warning("未在 %s 找到选定波段", root)
    return found


def require_rasterio() -> Any:
    try:
        # PostgreSQL/PostGIS often exports PROJ_LIB globally.  Its proj.db can
        # be older than Rasterio's PROJ library and makes even EPSG:32650 fail.
        # Prefer the data directory shipped with the active Rasterio wheel.
        package_spec = importlib.util.find_spec("rasterio")
        if package_spec and package_spec.origin:
            bundled_proj = Path(package_spec.origin).parent / "proj_data"
            if (bundled_proj / "proj.db").is_file():
                old_proj = os.environ.get("PROJ_DATA") or os.environ.get("PROJ_LIB")
                os.environ["PROJ_DATA"] = str(bundled_proj)
                os.environ["PROJ_LIB"] = str(bundled_proj)
                if old_proj and Path(old_proj) != bundled_proj:
                    logging.getLogger("sentinel_l2a").warning(
                        "忽略外部 PROJ 数据库，改用 Rasterio 自带版本: %s", bundled_proj)
        import rasterio  # noqa: PLC0415
        from rasterio.warp import reproject, Resampling  # noqa: PLC0415
    except ImportError as exc:
        raise RuntimeError("缺少 rasterio。请先运行: python -m pip install -r requirements.txt") from exc
    return rasterio, reproject, Resampling


def derive_fallback_crs(source: Path, configured: str | None, logger: logging.Logger) -> Any | None:
    """Recover Sentinel CRS if a JP2 driver exposes the transform but not CRS."""
    rasterio, _, _ = require_rasterio()
    if configured and configured.upper() != "AUTO":
        crs = rasterio.crs.CRS.from_user_input(configured)
        logger.warning("使用配置的 source_crs_fallback: %s", crs)
        return crs

    # The tile metadata is authoritative and is searched before parsing MGRS.
    for parent in [source.parent, *source.parents]:
        for name in ("MTD_TL.xml", "MTD_MSIL2A.xml"):
            metadata = parent / name
            if not metadata.is_file():
                continue
            try:
                root = ET.parse(metadata).getroot()
                for node in root.iter():
                    if node.tag.rsplit("}", 1)[-1] == "HORIZONTAL_CS_CODE" and node.text:
                        crs = rasterio.crs.CRS.from_user_input(node.text.strip())
                        logger.warning("JP2 未报告 CRS；从 %s 恢复: %s", metadata.name, crs)
                        return crs
            except (ET.ParseError, ValueError) as exc:
                logger.warning("无法从 %s 读取坐标系: %s", metadata, exc)

    # Standard Sentinel tile IDs use MGRS: T50TMK -> UTM zone 50, north.
    tile_match = re.search(r"T(\d{2})([C-HJ-NP-X])[A-Z]{2}", str(source), re.I)
    if tile_match:
        zone = int(tile_match.group(1))
        latitude_band = tile_match.group(2).upper()
        epsg = (32600 if latitude_band >= "N" else 32700) + zone
        crs = rasterio.crs.CRS.from_epsg(epsg)
        logger.warning("JP2 未报告 CRS；从 MGRS 瓦片 %s 恢复: EPSG:%s", tile_match.group(0), epsg)
        return crs
    return None


def profile_for_envi(profile: dict[str, Any], dtype: str, count: int, nodata: float | int | None = None) -> dict[str, Any]:
    result = profile.copy()
    # Avoid carrying format-specific creation options (for example JPEG2000
    # compression) from the source profile into an ENVI output.
    result.pop("compress", None)
    result.pop("tiled", None)
    result.pop("blockxsize", None)
    result.pop("blockysize", None)
    result.update(driver="ENVI", dtype=dtype, count=count)
    if nodata is not None:
        result["nodata"] = nodata
    else:
        result.pop("nodata", None)
    return result


def resample_to_grid(source: Path, destination: Path, grid: dict[str, Any], logger: logging.Logger) -> None:
    rasterio, reproject, Resampling = require_rasterio()
    with rasterio.open(source) as src:
        profile = profile_for_envi(src.profile, src.dtypes[0], 1, src.nodata)
        profile.update(crs=grid["crs"], transform=grid["transform"], width=grid["width"], height=grid["height"])
        with rasterio.open(destination, "w", **profile) as dst:
            reproject(
                source=rasterio.band(src, 1), destination=rasterio.band(dst, 1),
                src_transform=src.transform, src_crs=src.crs or grid["crs"], src_nodata=src.nodata,
                dst_transform=grid["transform"], dst_crs=grid["crs"], dst_nodata=src.nodata,
                resampling=Resampling.nearest,
            )
    logger.info("重采样 (Nearest Neighbor): %s -> %s", source.name, destination.name)


def target_grid(reference: Path, target_resolution: float, fallback_crs: Any | None = None) -> dict[str, Any]:
    rasterio, _, _ = require_rasterio()
    from rasterio.transform import from_bounds  # noqa: PLC0415
    with rasterio.open(reference) as src:
        crs = src.crs or fallback_crs
        if crs is None:
            raise ValueError(f"缺少坐标系: {reference}")
        left, bottom, right, top = src.bounds
        width = max(1, round((right - left) / target_resolution))
        height = max(1, round((top - bottom) / target_resolution))
        # Keep the reference extent; a 10 m reference produces exactly its grid.
        return {"crs": crs, "transform": from_bounds(left, bottom, right, top, width, height),
                "width": width, "height": height}


def write_stack(band_paths: list[tuple[str, Path]], destination: Path, logger: logging.Logger) -> None:
    rasterio, _, _ = require_rasterio()
    with rasterio.open(band_paths[0][1]) as first:
        profile = profile_for_envi(first.profile, first.dtypes[0], len(band_paths), first.nodata)
        with rasterio.open(destination, "w", **profile) as dst:
            for index, (name, path) in enumerate(band_paths, start=1):
                with rasterio.open(path) as src:
                    if (src.width, src.height, src.transform, src.crs) != (dst.width, dst.height, dst.transform, dst.crs):
                        raise ValueError(f"合成前网格不一致: {path}")
                    dst.write(src.read(1), index)
                    dst.set_band_description(index, name)
    logger.info("波段合成 (坐标系+范围+像元大小, Nearest Neighbor): %s", destination.name)


def calibrate_band(source: Path, destination: Path, logger: logging.Logger) -> None:
    """Implement ENVI: ((b1 ne 0)*(b1*0.0001-0.1))>0<1+(b1 eq 0)*(-1)."""
    rasterio, _, _ = require_rasterio()
    with rasterio.open(source) as src:
        raw = src.read(1)
        output = np.where(raw == 0, -1.0, np.clip(raw.astype("float32") * 0.0001 - 0.1, 0.0, 1.0)).astype("float32")
        profile = profile_for_envi(src.profile, "float32", 1, None)
        with rasterio.open(destination, "w", **profile) as dst:
            dst.write(output, 1)
            dst.set_band_description(1, src.descriptions[0] or source.stem)
    logger.info("辐射定标: %s", destination.name)


def write_mask(source: Path, cloud_classes: set[int], binary_path: Path, valid_path: Path, logger: logging.Logger) -> np.ndarray:
    rasterio, _, _ = require_rasterio()
    with rasterio.open(source) as src:
        scl = src.read(1)
        invalid = np.isin(scl, list(cloud_classes))
        if src.nodata is not None:
            invalid |= scl == src.nodata
        cloud_binary = invalid.astype("uint8")  # 1 is the same "cloud" result as the supplied Con expression.
        profile_binary = profile_for_envi(src.profile, "uint8", 1, 255)
        with rasterio.open(binary_path, "w", **profile_binary) as dst:
            dst.write(cloud_binary, 1)
            dst.set_band_description(1, "cloud_binary_1_cloud_0_noncloud")
        valid_mask = np.where(invalid, 255, 1).astype("uint8")
        with rasterio.open(valid_path, "w", **profile_binary) as dst:
            dst.write(valid_mask, 1)
            dst.set_band_description(1, "valid_1_invalid_nodata")
    logger.info("SCL 去云掩膜: %s", valid_path.name)
    return ~invalid


def apply_mask(calibrated_paths: list[tuple[str, Path]], valid: np.ndarray, destination: Path,
               nodata: float, logger: logging.Logger) -> None:
    rasterio, _, _ = require_rasterio()
    with rasterio.open(calibrated_paths[0][1]) as first:
        # Deliver the final cloud-masked image as GeoTIFF; ENVI DAT/HDR is
        # retained for all intermediate steps to mirror the manual workflow.
        profile = first.profile.copy()
        profile.pop("compress", None)
        profile.pop("tiled", None)
        profile.pop("blockxsize", None)
        profile.pop("blockysize", None)
        profile.update(driver="GTiff", dtype="float32", count=len(calibrated_paths),
                       nodata=nodata, compress="lzw", interleave="band")
        with rasterio.open(destination, "w", **profile) as dst:
            for index, (name, path) in enumerate(calibrated_paths, start=1):
                with rasterio.open(path) as src:
                    array = src.read(1).astype("float32", copy=False)
                    dst.write(np.where(valid, array, nodata), index)
                    dst.set_band_description(index, name)
    logger.info("按掩膜提取完成: %s", destination.name)


def choose_reference(scene: Scene, band_order: list[str], configured_name: str | None = None) -> BandFile:
    if configured_name:
        requested = configured_name.upper()
        if requested not in scene.bands:
            raise ValueError(f"配置的 reference_band={requested} 在该景中不存在")
        return scene.bands[requested]
    for name in band_order:
        if name in scene.bands:
            return scene.bands[name]
    return next(iter(scene.bands.values()))


def process_scene(scene: Scene, config: dict[str, Any], logger: logging.Logger) -> dict[str, str]:
    band_order = [b.upper() for b in config.get("bands", [])]
    requested = [(name, scene.bands[name]) for name in band_order if name in scene.bands]
    absent = [name for name in band_order if name not in scene.bands]
    if absent:
        logger.warning("%s 缺少波段: %s", scene.scene_id, ", ".join(absent))
    if not requested:
        raise ValueError("没有可处理的选定波段")
    if "SCL" not in scene.bands:
        raise ValueError("去云需要 SCL 波段，但未找到 SCL")

    base = Path(config["output_root"]) / scene.scene_id
    resampled_dir, calibrated_dir, mask_dir, final_dir = [base / x for x in ("01_resampled", "03_calibrated", "04_mask", "05_final")]
    for directory in (resampled_dir, calibrated_dir, mask_dir, final_dir):
        directory.mkdir(parents=True, exist_ok=True)

    target_m = float(config.get("target_resolution_m", 10))
    reference = choose_reference(scene, band_order, config.get("reference_band"))
    fallback_crs = derive_fallback_crs(reference.path, config.get("source_crs_fallback"), logger)
    grid = target_grid(reference.path, target_m, fallback_crs)
    logger.info("处理景: %s; 参考网格: %s; 目标分辨率: %sm", scene.scene_id, reference.path.name, target_m)

    resampled: list[tuple[str, Path]] = []
    for name, item in requested:
        nominal = item.nominal_resolution or "native"
        output = resampled_dir / f"{name}_{nominal}m_rs.dat"
        resample_to_grid(item.path, output, grid, logger)
        resampled.append((name, output))

    stack_dir = base / "02_stack"
    stack_dir.mkdir(exist_ok=True)
    raw_stack = stack_dir / "stack.dat"
    write_stack(resampled, raw_stack, logger)

    calibrated: list[tuple[str, Path]] = []
    for name, path in resampled:
        output = calibrated_dir / f"{name}_bm.dat"
        calibrate_band(path, output, logger)
        calibrated.append((name, output))
    calibrated_stack = calibrated_dir / "calibrated_stack.dat"
    write_stack(calibrated, calibrated_stack, logger)

    scl_path = dict(resampled)["SCL"]
    cloud_classes = {int(x) for x in config.get("invalid_scl_classes", [0, 1, 3, 8, 9, 10])}
    binary = mask_dir / "cloud_binary.dat"
    valid_mask = mask_dir / "valid_mask.dat"
    valid = write_mask(scl_path, cloud_classes, binary, valid_mask, logger)
    final = final_dir / "calibrated_cloudmasked.tif"
    apply_mask(calibrated, valid, final, float(config.get("final_nodata", -9999.0)), logger)

    if not config.get("keep_intermediate", True):
        # Only directories just created below the configured output root are removed.
        for directory in (resampled_dir, stack_dir, calibrated_dir):
            shutil.rmtree(directory)
        logger.info("已按配置删除中间结果: %s", base)
    return {"scene": scene.scene_id, "final": str(final), "raw_stack": str(raw_stack),
            "calibrated_stack": str(calibrated_stack), "cloud_binary": str(binary), "valid_mask": str(valid_mask)}


def build_logger(log_path: Path, verbose: bool) -> logging.Logger:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("sentinel_l2a")
    logger.handlers.clear()
    logger.setLevel(logging.DEBUG)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    stream = logging.StreamHandler()
    stream.setLevel(logging.INFO if verbose else logging.WARNING)
    stream.setFormatter(formatter)
    logger.addHandler(stream)
    return logger


def main() -> int:
    parser = argparse.ArgumentParser(description="Sentinel-2 L2A ENVI/ArcMap-equivalent batch preprocessing")
    parser.add_argument("--config", required=True, type=Path, help="JSON 配置文件")
    parser.add_argument("--verbose", action="store_true", help="同时将 INFO 日志输出到终端")
    args = parser.parse_args()
    config = load_config(args.config.resolve())
    output_root = Path(config["output_root"])
    log_name = config.get("log_file", f"sentinel_l2a_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.log")
    logger = build_logger(output_root / "logs" / log_name, args.verbose)
    selected = {str(b).upper() for b in config.get("bands", [])}
    if not selected or "SCL" not in selected:
        raise ValueError("bands 必须至少包含一个光谱波段和 SCL")
    preferred = {str(name).upper(): int(resolution)
                 for name, resolution in config.get("band_source_resolution", {}).items()}
    scenes = discover_scenes(config["input_paths"], selected, preferred, logger)
    if not scenes:
        logger.error("未发现可处理的 Sentinel-2 L2A 影像")
        return 2
    results, failures = [], []
    for scene in scenes:
        try:
            results.append(process_scene(scene, config, logger))
        except Exception as exc:  # batch should continue with the remaining scenes
            logger.exception("处理失败: %s: %s", scene.scene_id, exc)
            failures.append({"scene": scene.scene_id, "error": str(exc)})
    manifest = {"created_utc": datetime.now(timezone.utc).isoformat(), "config": str(args.config.resolve()),
                "success": results, "failed": failures}
    manifest_path = output_root / "processing_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("处理清单: %s", manifest_path)
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"致命错误: {error}", file=sys.stderr)
        raise SystemExit(2)
