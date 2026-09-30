#!/usr/bin/env python3
"""Batch image style filter tool for NAS albums.

Output rule:
- Save processed images into a style subdirectory under each source image directory.
- Keep original files untouched.

Example:
  python3 image_batch.py --dir /home/pi/nas_share/旅行 --style vintage --recursive
  python3 image_batch.py --dir /home/pi/nas_share/旅行 --style 复古 --recursive --dry-run
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import numpy as np
from PIL import Image, ImageEnhance, ImageFilter, ImageOps

_SUPPORTED_EXTS = {".jpg", ".jpeg", ".png", ".webp"}

_STYLE_ALIASES = {
    "复古": "vintage",
    "复古风": "vintage",
    "复古风格": "vintage",
    "vintage": "vintage",
    "日系": "japanese",
    "日系风": "japanese",
    "日系风格": "japanese",
    "japanese": "japanese",
    "胶片": "film",
    "胶片风": "film",
    "胶片风格": "film",
    "film": "film",
}

_STYLE_DIR_NAMES = {
    "vintage": "复古风格",
    "japanese": "日系风格",
    "film": "胶片风格",
}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Batch apply image style filters")
    parser.add_argument("--dir", required=True, help="Input directory")
    parser.add_argument("--style", required=True, help="Style: vintage/japanese/film or 中文别名")
    parser.add_argument("--recursive", action="store_true", help="Process subdirectories recursively")
    parser.add_argument("--dry-run", action="store_true", help="Preview only; do not write files")
    parser.add_argument("--quality", type=int, default=92, help="JPEG/WebP quality")
    return parser.parse_args()


def _style_key(style_input: str) -> str:
    key = (style_input or "").strip().lower()
    if key in _STYLE_ALIASES:
        return _STYLE_ALIASES[key]
    raise ValueError(f"Unsupported style: {style_input}")


def _iter_images(root: Path, recursive: bool):
    iterator = root.rglob("*") if recursive else root.glob("*")
    style_dirs = set(_STYLE_DIR_NAMES.values())
    for p in iterator:
        if not p.is_file():
            continue
        if p.suffix.lower() not in _SUPPORTED_EXTS:
            continue
        if any(part in style_dirs for part in p.parts):
            # Avoid re-processing generated outputs.
            continue
        yield p


def _apply_vintage(img: Image.Image, seed: int) -> Image.Image:
    arr = np.asarray(img).astype(np.float32)
    arr = arr * np.array([1.06, 0.97, 0.86], dtype=np.float32)
    arr = np.clip(arr + 18.0, 0, 255)
    out = Image.fromarray(arr.astype(np.uint8))
    out = ImageEnhance.Color(out).enhance(0.72)

    w, h = out.size
    yy, xx = np.ogrid[:h, :w]
    dist = np.sqrt(((xx - w / 2) / max(w / 2, 1)) ** 2 + ((yy - h / 2) / max(h / 2, 1)) ** 2)
    vignette = np.clip(1.0 - 0.55 * np.clip(dist - 0.35, 0, None), 0, 1)

    arr2 = np.asarray(out).astype(np.float32)
    arr2 *= vignette[..., None]

    rng = np.random.default_rng(seed)
    noise = rng.normal(0, 10, arr2.shape).astype(np.float32)
    arr2 = np.clip(arr2 + noise, 0, 255)
    return Image.fromarray(arr2.astype(np.uint8))


def _apply_japanese(img: Image.Image, _seed: int) -> Image.Image:
    out = ImageEnhance.Brightness(img).enhance(1.12)
    out = ImageEnhance.Contrast(out).enhance(0.86)
    out = ImageEnhance.Color(out).enhance(0.82)

    arr = np.asarray(out).astype(np.float32)
    arr = arr * np.array([0.95, 1.00, 1.06], dtype=np.float32)
    arr = (arr - 128.0) * 0.92 + 132.0
    arr = np.clip(arr, 0, 255)

    out = Image.fromarray(arr.astype(np.uint8))
    out = out.filter(ImageFilter.GaussianBlur(0.6))
    out = ImageEnhance.Sharpness(out).enhance(1.15)
    return out


def _apply_film(img: Image.Image, seed: int) -> Image.Image:
    out = ImageEnhance.Brightness(img).enhance(1.03)
    out = ImageEnhance.Contrast(out).enhance(1.08)
    out = ImageEnhance.Color(out).enhance(0.90)

    arr = np.asarray(out).astype(np.float32)
    arr = arr * np.array([1.04, 1.00, 0.94], dtype=np.float32)

    # Slight fade to mimic film stock tone.
    arr = (arr - 128.0) * 0.96 + 130.0

    w, h = out.size
    yy, xx = np.ogrid[:h, :w]
    dist = np.sqrt(((xx - w / 2) / max(w / 2, 1)) ** 2 + ((yy - h / 2) / max(h / 2, 1)) ** 2)
    vignette = np.clip(1.0 - 0.30 * np.clip(dist - 0.40, 0, None), 0, 1)
    arr *= vignette[..., None]

    rng = np.random.default_rng(seed)
    grain = rng.normal(0, 8, arr.shape).astype(np.float32)
    arr = np.clip(arr + grain, 0, 255)

    out = Image.fromarray(arr.astype(np.uint8))
    out = ImageEnhance.Sharpness(out).enhance(1.10)
    return out


def _apply_style(img: Image.Image, style: str, seed: int) -> Image.Image:
    if style == "vintage":
        return _apply_vintage(img, seed)
    if style == "japanese":
        return _apply_japanese(img, seed)
    if style == "film":
        return _apply_film(img, seed)
    raise ValueError(f"Unknown style: {style}")


def _save_image(img: Image.Image, dst: Path, quality: int) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    suffix = dst.suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        img.save(dst, quality=quality, optimize=True)
        return
    if suffix == ".webp":
        img.save(dst, quality=quality, method=6)
        return
    img.save(dst)


def main() -> int:
    args = _parse_args()
    root = Path(args.dir).expanduser().resolve()

    if not root.exists() or not root.is_dir():
        print(f"[ERROR] invalid dir: {root}")
        return 2

    try:
        style = _style_key(args.style)
    except ValueError as e:
        print(f"[ERROR] {e}")
        return 2

    style_dir = _STYLE_DIR_NAMES[style]

    total = 0
    planned = 0
    processed = 0
    skipped = 0
    failed = 0

    for src in _iter_images(root, args.recursive):
        total += 1
        dst = src.parent / style_dir / src.name
        planned += 1

        if args.dry_run:
            print(f"[PLAN]\t{src}\t->\t{dst}")
            continue

        # 跳过已生成且未过期的产物，避免同一风格重复处理
        if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
            skipped += 1
            print(f"[SKIP]\t{src}\t->\t{dst}")
            continue

        try:
            with Image.open(src) as im:
                img = ImageOps.exif_transpose(im).convert("RGB")
            seed = (abs(hash(str(src))) ^ os.path.getsize(src)) & 0xFFFFFFFF
            out = _apply_style(img, style, seed)
            _save_image(out, dst, quality=args.quality)
            processed += 1
            print(f"[OK]\t{src}\t->\t{dst}")
        except PermissionError as e:
            failed += 1
            print(f"[ERR]\t{src}\t{e}")
            print(
                "[ERR][HINT] 无写入权限：请检查目标目录归属是否为当前用户"
                "（sudo find /home/pi/nas_share -user <其他用户> -exec chown pi:pi {} +）"
            )
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"[ERR]\t{src}\t{e}")

    print(
        "[SUMMARY]"
        f" style={style_dir}"
        f" total={total}"
        f" planned={planned}"
        f" processed={processed}"
        f" failed={failed}"
        f" skipped={skipped}"
        f" dry_run={1 if args.dry_run else 0}"
    )
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
