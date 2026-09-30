#!/usr/bin/env python3
import json
import os
import time
from pathlib import Path

NAS_ROOT = Path(os.getenv("NAS_ROOT", str(Path.home() / "nas_share"))).expanduser()
APP_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_FILE = Path(os.getenv("NAS_CONTENT_FILE", str(APP_ROOT / "logs" / "nas_content.json"))).expanduser()

DOC_EXTS = {".txt", ".md", ".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".csv", ".rtf", ".odt"}
PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".heic", ".heif", ".tif", ".tiff"}
VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".m4v", ".webm", ".ts", ".m2ts", ".wmv", ".flv"}


def _walk_files(root: Path):
    if not root.exists() or not root.is_dir():
        return []
    out = []
    for dirpath, _dirnames, filenames in os.walk(root):
        if "/." in dirpath:
            continue
        for name in filenames:
            path = Path(dirpath) / name
            out.append(path)
    return out


def _collect():
    files = _walk_files(NAS_ROOT)
    docs = []
    photos = []
    videos = []
    for path in files:
        ext = path.suffix.lower()
        rel = str(path.relative_to(NAS_ROOT)) if path.is_relative_to(NAS_ROOT) else path.name
        if ext in DOC_EXTS:
            docs.append(rel)
        elif ext in PHOTO_EXTS:
            photos.append(rel)
        elif ext in VIDEO_EXTS:
            videos.append(rel)
    return docs, photos, videos


def _default_actions():
    return {
        "docs": [
            {"zh": "住房合同在哪", "en": "Where is my housing contract?", "lzh": "住房合同在哪", "len": "Find the contract"},
            {"zh": "住房合同的甲方是谁", "en": "Who signed the housing contract?", "lzh": "合同甲方是谁", "len": "Who signed it"},
            {"zh": "合同编号是多少", "en": "What is the contract number?", "lzh": "合同编号是多少", "len": "Contract number"},
            {"zh": "住房合同里的关键日期", "en": "Key dates in the housing contract", "lzh": "合同关键日期", "len": "Key dates"},
        ],
        "photos": [
            {"zh": "找海边的照片", "en": "Show me photos from the seaside", "lzh": "找海边的照片", "len": "Seaside photos"},
            {"zh": "找猫/动物的照片", "en": "Show me photos of animals", "lzh": "找猫/动物的照片", "len": "Animal photos"},
            {"zh": "帮我把家庭相册的照片分类，先预览", "en": "Organize my family album, preview first", "lzh": "相册自动分类", "len": "Organize album"},
            {"zh": "把家庭相册的照片加复古滤镜，先预览", "en": "Add a vintage filter to my album, preview first", "lzh": "加复古滤镜", "len": "Vintage filter"},
        ],
        "videos": [
            {"zh": "下载测试视频", "en": "Download the sample video", "lzh": "下载测试视频", "len": "Download a video"},
            {"zh": "播放oceans", "en": "Play oceans", "lzh": "播放oceans", "len": "Play oceans"},
        ],
        "ask": [
            {"zh": "简单介绍下你能帮我做什么", "en": "Briefly, what can you help me with?", "lzh": "你能做什么", "len": "What can you do"},
            {"zh": "这台 NAS 上都有什么内容", "en": "What is on this NAS?", "lzh": "NAS 里有什么", "len": "What's on it"},
        ],
    }


def main():
    docs, photos, videos = _collect()
    actions = _default_actions()
    payload = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M", time.localtime()),
        "counts": {
            "documents": len(docs),
            "photos": len(photos),
            "videos": len(videos),
        },
        "samples": {
            "documents": docs[:10],
            "photos": photos[:10],
            "videos": videos[:10],
        },
        "docs": actions["docs"],
        "photos": actions["photos"],
        "videos": actions["videos"],
        "ask": actions["ask"],
    }
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"manifest generated: {OUTPUT_FILE}")
    print(json.dumps(payload["counts"], ensure_ascii=False))


if __name__ == "__main__":
    main()
