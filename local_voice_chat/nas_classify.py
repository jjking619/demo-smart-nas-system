#!/usr/bin/env python3
"""NAS 照片语义分类工具（方案 B：基于 immich CLIP 向量，不依赖文件名）。

对每张图片：
1. 调 immich ML 服务的 CLIP 视觉编码，得到图片向量
2. 用英文描述性类别词做 CLIP 文本编码，得到类别向量
3. 计算余弦相似度，取最高者为建议类别

纯标准库实现（urllib），可在 openclaw 容器内直接运行：
  python3 /nas_share/tools/nas_classify.py /nas_share/家庭相册/*.jpg
"""
import argparse
from datetime import datetime
import json
import re
import socket
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ML_URL = "http://immich-machine-learning:3003"
MODEL = "ViT-B-32__openai"
CONNECT_TIMEOUT = 3
PREDICT_TIMEOUT = 120
_ML_READY_CHECKED = False
PREDICT_RETRIES = 1
PREDICT_RETRY_DELAY_SEC = 0.8

ARCHIVE_ROOT_HINTS = ("家庭相册", "手机相册", "旅行", "备份")

# 滤镜产物目录名（与 image_batch.py 的 _STYLE_DIR_NAMES 保持一致）。
# 分类递归遍历时需跳过这些目录：滤镜产物是派生图，不应被再次归档移动，
# 否则会把 家庭相册/复古风格/ 等目录掏空，或产生 类别/复古风格/ 套娃。
STYLE_DIR_NAMES = ("复古风格", "日系风格", "胶片风格")

_SUPPORTED_EXTS = (".jpg", ".jpeg", ".png", ".webp")

# 交通工具类在“旅行街景/路景”里易与风景、美食重叠，做一个保守回退：
# 仅当交通工具置信度不高且与候选类分差很小时，才把首类切到候选类。
_VEHICLE_AMBIGUOUS_MAX = 0.26
_VEHICLE_AMBIGUOUS_MARGIN = 0.015
_VEHICLE_FALLBACK_CATS = ("风景", "美食")

# 发给 ML 前的本地校验：小于此值视为损坏/假图片，直接跳过，避免拖垮 ML 服务
_MIN_IMAGE_BYTES = 1024
# 各格式文件头魔数（只检头几字节，纯标准库）
_MAGIC = {
    ".jpg": b"\xff\xd8\xff",
    ".jpeg": b"\xff\xd8\xff",
    ".png": b"\x89PNG",
    ".webp": b"RIFF",
}


def _is_valid_image(path: Path) -> bool:
    """快速校验：大小 + 文件头魔数。纯标准库，可在容器内直接运行。"""
    try:
        if path.stat().st_size < _MIN_IMAGE_BYTES:
            return False
        magic = _MAGIC.get(path.suffix.lower())
        if magic is None:
            return True  # 未知扩展名，不强制校验
        with path.open("rb") as fh:
            return fh.read(len(magic)) == magic
    except OSError:
        return False

# 类别 -> 英文描述（CLIP 对英文描述的分类效果远好于中文短词）
CATEGORIES = {
    "人物": "a photo of a person, people, portrait, human face",
    "动物": "a photo focused on an animal subject, pet, cat, dog, bird, wildlife",
    "美食": "a photo of food, meal, dish, restaurant",
    "风景": "a photo of landscape scenery, nature view, mountain, sea, beach, river, lake, sunset sky, blue sky, cloudy sky, cloudscape, skyline, horizon, sun rays, cloud reflection, waterscape",
    "植物": "a photo of plants, flowers, trees, garden",
    "建筑": "a photo of buildings, architecture, city, urban",
    "交通工具": "a photo focused on a vehicle, car, bus, train, motorcycle, bicycle, airplane, ship, transportation",
    "日常用品": "a photo of everyday objects, household items",
}


def _assert_ml_service_ready() -> None:
    global _ML_READY_CHECKED
    if _ML_READY_CHECKED:
        return

    parsed = urllib.parse.urlparse(ML_URL)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    if not host:
        raise RuntimeError(f"Invalid ML_URL: {ML_URL}")

    try:
        with socket.create_connection((host, port), timeout=CONNECT_TIMEOUT):
            _ML_READY_CHECKED = True
    except OSError as e:
        raise RuntimeError(
            f"Immich ML服务不可达 ({host}:{port}): {e}. "
            "请先执行: docker start immich-machine-learning"
        ) from e


def _predict(entries: dict, text=None, image=None) -> dict:
    _assert_ml_service_ready()

    boundary = "----nasclassify"
    parts = []
    parts.append(
        (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"entries\"\r\n\r\n"
            f"{json.dumps(entries)}"
        ).encode()
    )
    if text is not None:
        parts.append(
            (
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"text\"\r\n\r\n"
                f"{text}"
            ).encode()
        )
    if image is not None:
        parts.append(
            (
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; "
                f"filename=\"img.jpg\"\r\nContent-Type: image/jpeg\r\n\r\n"
            ).encode()
            + image
        )
    parts.append(f"--{boundary}--\r\n".encode())
    body = b"\r\n".join(parts)
    req = urllib.request.Request(
        f"{ML_URL}/predict",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    last_err = None
    for attempt in range(PREDICT_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=PREDICT_TIMEOUT) as resp:
                return json.load(resp)
        except urllib.error.URLError as e:
            last_err = e
            if attempt < PREDICT_RETRIES:
                time.sleep(PREDICT_RETRY_DELAY_SEC)
                continue
    raise RuntimeError(
        f"Immich ML请求失败: {last_err}. "
        "请检查 immich-machine-learning 容器状态。"
    ) from last_err


def _embedding(resp: dict):
    v = resp.get("clip")
    if isinstance(v, str):
        return json.loads(v)
    if isinstance(v, list):
        return v
    raise RuntimeError(f"Unexpected ML clip response: {str(resp)[:200]}")


def _encode_text(text: str):
    entries = {"clip": {"textual": {"modelName": MODEL, "options": {}}}}
    return _embedding(_predict(entries, text=text))


def _encode_image(img_bytes: bytes):
    entries = {"clip": {"visual": {"modelName": MODEL}}}
    return _embedding(_predict(entries, image=img_bytes))


def _cosine(a, b):
    import math

    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return sum(x * y for x, y in zip(a, b)) / (na * nb)


def _apply_ambiguous_vehicle_fallback(ranked):
    if not ranked:
        return ranked
    top_cat, top_score = ranked[0]
    if top_cat != "交通工具" or top_score >= _VEHICLE_AMBIGUOUS_MAX:
        return ranked

    candidates = [(cat, score) for cat, score in ranked if cat in _VEHICLE_FALLBACK_CATS]
    if not candidates:
        return ranked
    alt_cat, alt_score = max(candidates, key=lambda x: x[1])
    if (top_score - alt_score) > _VEHICLE_AMBIGUOUS_MARGIN:
        return ranked

    reordered = [(alt_cat, alt_score), (top_cat, top_score)]
    reordered.extend((cat, score) for cat, score in ranked if cat not in {alt_cat, top_cat})
    return reordered


def classify(img_bytes: bytes, cat_vecs: dict, min_score: float = 0.20):
    img_vec = _encode_image(img_bytes)
    scores = {cat: _cosine(img_vec, v) for cat, v in cat_vecs.items()}
    ranked = sorted(scores.items(), key=lambda x: -x[1])
    ranked = _apply_ambiguous_vehicle_fallback(ranked)
    if ranked[0][1] < min_score:
        ranked = [("不确定", ranked[0][1])] + ranked
    return ranked


def _safe_stem(stem: str) -> str:
    name = stem.strip().replace(" ", "_")
    name = re.sub(r"[\\/:*?\"<>|\r\n\t]+", "_", name)
    name = re.sub(r"_+", "_", name).strip("._ ")
    return name or "IMG"


def _strip_archive_prefix(stem: str) -> str:
    """去掉文件名里已有的归档前缀（YYYYMMDD_HHMMSS_类别_），可重复剥离。

    防止重复归档时前缀不断叠加（test_1 → ..._风景_test_1 → ..._风景_..._风景_test_1）。
    只在前缀确实存在时才剥离，普通原名（如 IMG_1001）不受影响。
    """
    ts_re = re.compile(r"^\d{8}_\d{6}_")
    while True:
        m = ts_re.match(stem)
        if not m:
            break
        stem = stem[m.end():]
        if "_" in stem:
            first, rest = stem.split("_", 1)
            if first in CATEGORIES or first == "待确认":
                stem = rest
    return stem


def _detect_archive_root(path: Path) -> Path:
    parts = path.parts
    for i, p in enumerate(parts):
        if p in ARCHIVE_ROOT_HINTS:
            return Path(*parts[: i + 1])
    return path.parent


def _build_target_path(src: Path, category: str, unknown_dir: str) -> Path:
    root = _detect_archive_root(src)
    cat_dir = unknown_dir if category == "不确定" else category
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = _safe_stem(_strip_archive_prefix(src.stem))
    suffix = src.suffix.lower()

    target_dir = root / cat_dir
    target = target_dir / f"{ts}_{cat_dir}_{stem}{suffix}"
    if target == src:
        return target

    idx = 2
    while target.exists() and target != src:
        target = target_dir / f"{ts}_{cat_dir}_{stem}_{idx}{suffix}"
        idx += 1
    return target


def _move_file(src: Path, dst: Path, dry_run: bool) -> None:
    if src == dst:
        return
    if dry_run:
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))


def _iter_images(root: Path, recursive: bool):
    """遍历 root 下待分类的图片，跳过滤镜产物目录。

    仅按“相对 root 的路径”判断，因此当 root 本身就是风格目录（如用户显式对
    `家庭相册/复古风格` 分类）时，其中文件仍会被处理——显式指定即视为有意为之。
    """
    iterator = root.rglob("*") if recursive else root.glob("*")
    for f in iterator:
        if not f.is_file() or f.suffix.lower() not in _SUPPORTED_EXTS:
            continue
        try:
            rel_parent = f.relative_to(root).parent
        except ValueError:
            rel_parent = f.parent
        if any(part in STYLE_DIR_NAMES for part in rel_parent.parts):
            # 处于风格目录内部：属滤镜派生产物，跳过以免被再次归档移动。
            continue
        yield f


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("paths", nargs="*", help="图片路径")
    ap.add_argument("--dir", help="目录，遍历其中图片")
    ap.add_argument("--recursive", action="store_true", help="递归子目录")
    ap.add_argument("--archive", action="store_true", help="按规则自动重命名并归档")
    ap.add_argument("--dry-run", action="store_true", help="仅输出归档计划，不执行移动")
    ap.add_argument("--min-score", type=float, default=0.20, help="最低置信阈值")
    ap.add_argument("--unknown-dir", default="待确认", help="低置信度归档目录名")
    args = ap.parse_args()

    files = [Path(p) for p in args.paths]
    if args.dir:
        files += list(_iter_images(Path(args.dir), args.recursive))

    if not files:
        print("没有找到图片", file=sys.stderr)
        sys.exit(1)

    print(f"[INFO] 编码 {len(CATEGORIES)} 个类别文本向量...", file=sys.stderr)
    try:
        cat_vecs = {cat: _encode_text(desc) for cat, desc in CATEGORIES.items()}
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(2)

    for f in files:
        if not _is_valid_image(f):
            print(f"{f}\tSKIP: 文件过小或格式异常（<{_MIN_IMAGE_BYTES}B 或魔数不符），跳过以避免 ML 崩溃",
                  file=sys.stderr)
            continue
        try:
            img = f.read_bytes()
            top = classify(img, cat_vecs, min_score=args.min_score)
            best, score = top[0]
            top3 = " ".join(f"{k}={v:.3f}" for k, v in top[:3])
            if args.archive:
                dst = _build_target_path(f, best, args.unknown_dir)
                try:
                    _move_file(f, dst, args.dry_run)
                    action = "PLAN" if args.dry_run else "MOVED"
                    if f == dst:
                        action = "SKIP"
                    print(f"{f}\t建议: {best} (score={score:.3f})\t{top3}\t{action}: {dst}")
                except OSError as e:
                    print(f"{f}\tERROR: move failed: {e}", file=sys.stderr)
            else:
                print(f"{f}\t建议: {best} (score={score:.3f})\t{top3}")
        except Exception as e:  # noqa: BLE001
            print(f"{f}\tERROR: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
