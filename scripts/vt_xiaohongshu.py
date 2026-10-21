"""video-transcribe · 小红书。

小红书视频没有平台字幕，一律走 下载视频 → 抽音频 → ASR。
批量模式（用户主页）串行处理，且每次 opencli 调用前插入随机抖动，
避免触发风控。

注意：opencli 的 xiaohongshu download 会同时下载图文与视频，
因此拿到文件后必须先判断有没有视频/音频轨，图文笔记要给出明确报错。
"""

from __future__ import annotations

import os
import re
import shutil
import time
from datetime import datetime
from pathlib import Path

from vt_common import (
    LOG,
    browser_args,
    opencli_json,
    opencli_json_with_prenav_retry,
    run_opencli_with_prenav_retry,
)

_VIDEO_SUFFIXES = (".mp4", ".mov", ".webm", ".mkv", ".m4v", ".avi")

# 只有图片 → 图文笔记，没有可转录内容
_IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".avif")


def extract_note_id(url: str) -> str:
    """从小红书 URL 中提取笔记 ID。"""
    value = (url or "").strip()
    for pattern in (r"/(?:explore|discovery/item)/([A-Za-z0-9]+)",
                    r"/user/profile/[A-Za-z0-9]+/([A-Za-z0-9]+)"):
        match = re.search(pattern, value)
        if match:
            return match.group(1)
    path = value.rstrip("/").split("?")[0]
    return path.rsplit("/", 1)[-1]


def extract_user_id(user_input: str) -> str:
    """从用户主页链接或纯 ID 中提取 user_id。"""
    value = (user_input or "").strip()
    match = re.search(r"user/profile/([A-Za-z0-9]+)", value)
    if match:
        return match.group(1)
    if re.fullmatch(r"[A-Za-z0-9]+", value):
        return value
    raise ValueError(f"无法解析小红书用户输入: {user_input}（需要主页链接或 user_id）")


def note_timestamp(note_id: str) -> int:
    """从小红书笔记 ID 推发布时间（Unix 秒）。

    小红书笔记**没有发布日期字段**（`opencli xiaohongshu note` 只返回
    title/author/content/likes/collects/comments/tags），但 ID 前 8 位十六进制
    就是创建时间的 Unix 秒。日期按**运行机的本地时区**换算。

    解析失败返回 0（排到最后），不抛异常——排序不该因为一条脏数据整体失败。
    """
    value = (note_id or "").strip()
    if len(value) < 8:
        return 0
    try:
        return int(value[:8], 16)
    except (ValueError, TypeError):
        return 0


def note_datetime(note_id: str) -> str:
    """笔记 ID → `YYYY-MM-DD`（本地时区）。推不出来返回空串。"""
    ts = note_timestamp(note_id)
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
    except (OSError, OverflowError, ValueError):
        return ""


def fetch_meta(url: str, jitter=None, timeout: float = 240) -> dict:
    """取笔记元数据。属于尽力而为：失败时用笔记 ID 兜底，不阻断转录。"""
    if jitter:
        jitter.wait()
    info: dict = {}
    try:
        data = opencli_json(
            ["xiaohongshu", "note", "-f", "json", *browser_args(), url], timeout=timeout)
        if isinstance(data, dict):
            data = [data]
        for item in data or []:
            if isinstance(item, dict) and item.get("field") is not None:
                info[item["field"]] = item.get("value")
    except RuntimeError as e:
        LOG.warning("笔记元数据获取失败，用笔记 ID 兜底: %s", e)

    upload_date = ""
    for key in ("publish_date", "发布时间", "date", "create_time", "创建时间"):
        value = info.get(key)
        if value:
            upload_date = str(value)[:10]
            break

    note_id = extract_note_id(url)
    # 元数据里没有日期是常态（note 接口不返回日期字段），用笔记 ID 兜底
    if not upload_date:
        upload_date = note_datetime(note_id)

    return {
        "vid": note_id,
        "title": info.get("title") or info.get("笔记标题") or "",
        "author": info.get("author") or info.get("作者") or "",
        "date": upload_date,
    }


def iter_user_notes(user_input: str, cfg: dict, jitter=None) -> list[dict]:
    """取用户主页的视频笔记（仅 type=video），失败时指数退避重试。

    2026-10-20：列表请求改走 `opencli_json_with_prenav_retry`——ephemeral 会话下
    预导航偶发 `Navigation rejected.`，以前只靠外层退避硬扛，现在先内部重试一次。
    """
    user_id = extract_user_id(user_input)
    limit = int(cfg["xiaohongshu"].get("user_limit", 200))
    retries = int(cfg["xiaohongshu"].get("retries", 3))
    last_error = ""

    for attempt in range(retries + 1):
        if jitter:
            jitter.wait()
        try:
            data = opencli_json_with_prenav_retry(
                ["xiaohongshu", "user", user_id, "-f", "json",
                 "--limit", str(limit), *browser_args()],
                timeout=300,
            )
        except RuntimeError as e:
            last_error = str(e)
            data = None
            if attempt < retries:
                delay = 2 ** attempt
                LOG.warning("获取用户笔记失败（第 %d/%d 次），%ds 后重试: %s",
                            attempt + 1, retries, delay, last_error)
                time.sleep(delay)
                continue
            raise
        if data is None:
            data = []
        if not isinstance(data, list):
            raise ValueError(f"小红书 user 返回格式异常，期望列表，实际: {type(data).__name__}")

        notes = []
        for item in data:
            if not isinstance(item, dict):
                continue
            if item.get("type") and item.get("type") != "video":
                continue
            note_id = item.get("id") or extract_note_id(item.get("url", ""))
            notes.append({
                "id": note_id,
                "title": item.get("title") or "",
                "url": item.get("url") or "",
                "type": item.get("type") or "",
                # 列表接口不返回日期，用笔记 ID 前 8 位十六进制推（见 note_timestamp）
                "date": note_datetime(note_id),
            })
        return [n for n in notes if n["url"]]

    raise RuntimeError(f"获取小红书用户笔记失败: {last_error}")


def _find_files(out_dir: Path) -> tuple[list[Path], list[Path]]:
    videos: list[Path] = []
    images: list[Path] = []
    for root, _dirs, files in os.walk(out_dir):
        for name in files:
            path = Path(root) / name
            suffix = path.suffix.lower()
            if suffix in _VIDEO_SUFFIXES:
                videos.append(path)
            elif suffix in _IMAGE_SUFFIXES:
                images.append(path)
    return videos, images


def download_media(url: str, out_dir: str | Path, jitter=None, timeout: float = 3600) -> Path:
    """下载笔记媒体，返回视频文件路径。图文笔记直接报错。"""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    if jitter:
        jitter.wait()

    proc = run_opencli_with_prenav_retry(
        ["xiaohongshu", "download", "--output", str(out), "-f", "json",
         *browser_args(), url],
        timeout=timeout,
    )
    videos, images = _find_files(out)

    if not videos:
        if images:
            raise RuntimeError(
                f"这是一篇图文笔记（只下到 {len(images)} 张图片，没有视频），无法转录: {url}"
            )
        if proc.returncode != 0:
            raise RuntimeError(
                f"小红书下载失败 (exit {proc.returncode}): {url}\n"
                f"  stdout:\n{(proc.stdout or '').strip() or '(空)'}\n  stderr:\n{(proc.stderr or '').strip() or '(空)'}"
            )
        raise FileNotFoundError(f"小红书下载后未找到视频文件: {url}，目录: {out}")

    if proc.returncode != 0:
        LOG.warning("opencli 返回非零退出码 %d，但已找到视频文件，继续", proc.returncode)

    # 多个视频（图集/多段）时取第一个，并提示
    if len(videos) > 1:
        LOG.warning("该笔记有 %d 个视频文件，只处理第一个: %s", len(videos), videos[0].name)
    return sorted(videos)[0]


def item_dir(workdir: str | Path, note_id: str) -> Path:
    """每条笔记一个独立下载目录。

    opencli 的 xiaohongshu download 会往输出目录里写图/视频，
    批量串行时共用一个目录会捡到上一条笔记的文件。
    """
    path = Path(workdir) / note_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def extract_text(url: str, cfg: dict, transcriber, workdir: str | Path, jitter=None) -> dict:
    """下载并转录一条小红书笔记，返回 {text, source, media}。"""
    note_id = extract_note_id(url)
    download_dir = item_dir(workdir, note_id)
    keep = bool(cfg.get("keep_media"))
    media = download_media(url, download_dir, jitter)
    try:
        text, backend, elapsed = transcriber.transcribe(media, download_dir)
        LOG.info("  ASR 完成（%s，%.1fs，%d 字）", backend, elapsed, len(text))
        return {"text": text, "source": "asr", "media": str(media) if keep else None}
    finally:
        if not keep:
            shutil.rmtree(download_dir, ignore_errors=True)
