"""video-transcribe · B站。

文本获取顺序：官方字幕（人工或 AI 字幕）→ 都没有才下载音频做 ASR。
列表来源：opencli bilibili user-videos（分页 + 日期剪枝），
合集信息与合集视频列表走 B站公开 API（无需登录）。

所有文本获取函数只负责「拿到文本」，落盘与跳过判断由 vt.py 统一处理。
"""

from __future__ import annotations

import json
import re
import shutil
import time
import urllib.parse
import urllib.request
from datetime import date, datetime
from pathlib import Path

from vt_common import (
    LOG,
    browser_args,
    opencli_json,
    opencli_json_with_prenav_retry,
    run_opencli_with_prenav_retry,
)
_MEDIA_SUFFIXES = (".mp4", ".mkv", ".webm", ".m4a", ".mp3", ".flac", ".aac", ".wav")

_API_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Referer": "https://space.bilibili.com/",
}


# ---------------------------------------------------------------------------
# 基础解析
# ---------------------------------------------------------------------------

def extract_bvid(value: str) -> str | None:
    """从 URL / 裸 ID 中提取 bvid。"""
    v = (value or "").strip()
    match = re.search(r"(BV[0-9A-Za-z]{8,})", v)
    if match:
        return match.group(1)
    return None


def extract_author_name(author_field: str | None) -> str | None:
    """opencli 的 author 形如 `用户名 (mid: 123)`，取用户名部分。"""
    if not author_field:
        return None
    match = re.match(r"^(.+?)\s*\(mid:\s*\d+\)", str(author_field))
    return (match.group(1) if match else str(author_field)).strip() or None


def extract_mid(user_input: str) -> str:
    """从用户主页链接或纯数字 mid 中提取 mid。"""
    value = (user_input or "").strip()
    match = re.search(r"space\.bilibili\.com/(\d+)", value)
    if match:
        return match.group(1)
    if re.fullmatch(r"\d+", value):
        return value
    raise ValueError(f"无法解析 B站用户输入: {user_input}（需要主页链接或纯数字 mid）")


def extract_season_params(user_input: str) -> tuple[str, str]:
    """从 https://space.bilibili.com/{mid}/lists/{season_id} 提取 (mid, season_id)。"""
    match = re.search(r"space\.bilibili\.com/(\d+)/lists/(\d+)", user_input or "")
    if match:
        return match.group(1), match.group(2)
    raise ValueError(
        f"无法解析合集链接: {user_input}"
        "（需要形如 https://space.bilibili.com/{{mid}}/lists/{{season_id}}）"
    )


def parse_pubdate(value) -> date | None:
    """解析发布日期：支持秒/毫秒时间戳，以及各种 `YYYY-MM-DD[ HH:MM]` 文本。

    opencli 的字段形态不统一（video 用 publish_time，
    user-videos 用 date），所以这里按「开头是不是日期」来认，不做字段名假设。
    """
    if value in (None, ""):
        return None
    text = str(value).strip()
    if text.isdigit():
        ts = int(text)
        if ts > 1_000_000_000_000:
            ts //= 1000
        try:
            return datetime.fromtimestamp(ts).date()
        except (OSError, OverflowError, ValueError):
            return None
    match = re.match(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", text)
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return None
    return None


def _pubdate_of(item: dict) -> str:
    for key in ("pubdate", "publish_time", "publish_date", "date", "created", "ctime"):
        value = item.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def _normalize(item: dict) -> dict:
    bvid = item.get("bvid") or extract_bvid(item.get("url", "")) or ""
    return {
        "bvid": bvid,
        "title": item.get("title") or "",
        "url": item.get("url") or f"https://www.bilibili.com/video/{bvid}",
        "pubdate": _pubdate_of(item),
        "series_name": item.get("series_name") or "",
    }


# ---------------------------------------------------------------------------
# 单个视频：元数据 / 字幕
# ---------------------------------------------------------------------------

def get_video_info(bvid: str, timeout: float = 180) -> dict:
    """取视频元数据。优先走 B站公开 API（纯 HTTP，不开浏览器、不算风控目标）。

    回退到 opencli（需登录的视频、API 异常时）。两者统一成 {field: value} 结构，
    字段名对齐 opencli 的写法（title / author / publish_time / parts）。
    """
    try:
        info = _api_video_info(bvid, timeout=min(float(timeout), 30))
        if info:
            return info
    except Exception as e:  # noqa: BLE001 —— 回退 opencli
        LOG.debug("B站公开 API 取元数据失败，回退 opencli: %s", e)

    data = opencli_json_with_prenav_retry(
        ["bilibili", "video", "-f", "json", *browser_args(), bvid], timeout=timeout)
    if isinstance(data, dict):
        data = [data]
    info = {}
    for item in data or []:
        if isinstance(item, dict) and item.get("field") is not None:
            info[item["field"]] = item.get("value")
    return info


def _api_video_info(bvid: str, timeout: float = 30) -> dict:
    """B站 web-interface/view：标题、作者、发布日期、分P数。"""
    payload = _api_get_json("https://api.bilibili.com/x/web-interface/view",
                            {"bvid": bvid}, timeout=timeout)
    if payload.get("code") != 0:
        raise RuntimeError(f"view API code={payload.get('code')} {payload.get('message', '')}")
    data = payload.get("data") or {}
    owner = data.get("owner") or {}
    pubdate = data.get("pubdate")
    return {
        "bvid": data.get("bvid") or bvid,
        "title": data.get("title") or "",
        "author": owner.get("name") or "",
        "mid": owner.get("mid"),
        # 与 opencli 的 publish_time 对齐成 `YYYY-MM-DD HH:MM`
        "publish_time": (datetime.fromtimestamp(pubdate).strftime("%Y-%m-%d %H:%M")
                         if pubdate else ""),
        "parts": str(data.get("videos") or 1),
        "duration": str(data.get("duration") or ""),
    }


def info_date(info: dict) -> str:
    """从 video 元数据里取发布日期原始串（opencli 用 publish_time）。"""
    for key in ("publish_time", "pubdate", "publish_date", "date", "create_time"):
        value = info.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def is_chinese_text(text: str, threshold: float = 0.5) -> bool:
    chars = [c for c in text if not c.isspace()]
    if not chars:
        return False
    cjk = sum(1 for c in chars if "\u4e00" <= c <= "\u9fff")
    return (cjk / len(chars)) >= threshold


def get_subtitle(bvid: str, lang: str | None = "zh-CN", timeout: float = 240) -> str | None:
    """取 B站字幕文本。无字幕（EMPTY_RESULT）或失败时返回 None。"""
    args = ["bilibili", "subtitle", "-f", "json"]
    if lang:
        args += ["--lang", lang]
    args += browser_args()
    args.append(bvid)
    try:
        data = opencli_json_with_prenav_retry(args, timeout=timeout)
    except RuntimeError as e:
        LOG.warning("字幕获取失败，转为下载音频走 ASR: %s", e)
        return None

    if not data:
        return None
    if not isinstance(data, list):
        return None

    text = "\n".join(
        str(item.get("content", "")) for item in data
        if isinstance(item, dict) and item.get("content")
    ).strip()
    if not text:
        return None

    # 指定中文时校验实际语言，避免拿到英文/日文字幕
    if lang and lang.startswith(("zh", "ai-zh")) and not is_chinese_text(text):
        LOG.warning("字幕语言与 %s 不匹配，转为下载音频走 ASR", lang)
        return None
    return text


# ---------------------------------------------------------------------------
# 下载音频
# ---------------------------------------------------------------------------

def _find_media(out_dir: Path, bvid: str) -> Path | None:
    if not out_dir.is_dir():
        return None
    for entry in sorted(out_dir.iterdir()):
        if entry.is_file() and entry.name.startswith(bvid) and entry.suffix.lower() in _MEDIA_SUFFIXES:
            return entry
    return None


def _report_failure(stdout: str, bvid: str) -> str | None:
    """从 opencli 的 JSON 状态列表里取失败原因。"""
    from vt_common import _extract_json

    data = _extract_json(stdout or "")
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and item.get("bvid") == bvid and item.get("status") == "failed":
                return str(item.get("size") or "未知错误")
    return None


def _download_once(bvid: str, out_dir: Path, quality: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    proc = run_opencli_with_prenav_retry(
        ["bilibili", "download", "--output", str(out_dir),
         "--format", "json", "--quality", quality, *browser_args(), bvid],
        timeout=3600,
    )
    path = _find_media(out_dir, bvid)

    if path is None:
        if proc.returncode != 0:
            raise RuntimeError(
                f"opencli 下载失败 (exit {proc.returncode}): {bvid}\n"
                f"  stdout:\n{(proc.stdout or '').strip() or '(空)'}\n  stderr:\n{(proc.stderr or '').strip() or '(空)'}"
            )
        reason = _report_failure(proc.stdout or "", bvid)
        if reason:
            raise RuntimeError(f"opencli 下载失败: {bvid}（{reason}）")
        raise FileNotFoundError(f"opencli 下载后未找到媒体文件: {bvid}，目录: {out_dir}")

    if proc.returncode != 0:
        LOG.warning("opencli 返回非零退出码 %d，但文件已存在，继续: %s",
                    proc.returncode, path.name)
    return path


def download_media(bvid: str, out_dir: str | Path, cfg: dict) -> Path:
    """下载 B站媒体，返回本地路径。

    opencli 的 --quality 并不保证音频轨存在（偶发挑到纯视频流），
    因此每轮都校验音频轨，失败则清理重试，最后退到 fallback_quality。
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    from vt_asr import has_audio_stream

    quality = str(cfg["bilibili"].get("quality", "audio"))
    fallback = str(cfg["bilibili"].get("fallback_quality", "480p"))
    plan = [quality, quality, quality] + ([fallback, fallback] if fallback != quality else [])

    # 清掉该 bvid 的历史残留，避免误判「已下载」
    for stale in out.glob(f"{bvid}*"):
        if stale.is_file() and stale.suffix.lower() in _MEDIA_SUFFIXES:
            stale.unlink(missing_ok=True)

    last_error: Exception | None = None
    for index, q in enumerate(plan):
        try:
            path = _download_once(bvid, out, q)
        except Exception as e:  # noqa: BLE001
            last_error = e
            LOG.warning("下载尝试 %d/%d (quality=%s) 失败: %s", index + 1, len(plan), q, e)
            time.sleep(1.0)
            continue

        if has_audio_stream(path):
            return path

        LOG.warning("下载文件不含音频轨（尝试 %d/%d, quality=%s），删除重试: %s",
                    index + 1, len(plan), q, path.name)
        path.unlink(missing_ok=True)
        last_error = RuntimeError(
            f"下载到的文件不含音频轨（opencli 选中了纯视频流）: {bvid}"
        )
        time.sleep(1.0)

    raise RuntimeError(f"B站媒体下载失败: {bvid}\n  最后一次错误: {last_error}")


# ---------------------------------------------------------------------------
# 列表：用户投稿 / 合集
# ---------------------------------------------------------------------------

def iter_user_videos(mid: str, cfg: dict, *, since: date | None = None,
                     until: date | None = None, on_page=None) -> list[dict]:
    """分页拉取用户投稿（接口按发布时间从新到旧）。

    逐条按 since/until 过滤，同时利用有序性做整页剪枝减少请求：
    最旧一条早于 since → 后面的页只会更旧，停止翻页。

    **日期解析失败的条目不会被静默丢弃**（用户 2026-10-20 要求）：会记 WARNING，
    并在函数结尾汇总，避免「视频凭空消失、看起来像不符合时间条件」。

    **列表请求会重试**（2026-10-20 第二轮）：此前这步用的是最裸的 opencli_json，
    一次 `Extension not connected` 或 `Navigation rejected.` 抖动就能让整批在开头退出，
    而字幕/下载那两步反而有重试。现在：先走预导航重试封装，再套指数退避。
    """
    page_size = int(cfg["bilibili"]["page_size"])
    max_pages = cfg["bilibili"].get("max_pages")
    retries = int(cfg.get("opencli_retries", 3) or 0)
    base_delay = float(cfg.get("opencli_retry_base_delay", 2.0) or 1.0)
    videos: list[dict] = []
    unparsed: list[dict] = []
    page = 1

    while True:
        if max_pages is not None and page > int(max_pages):
            break
        data = _fetch_user_videos_page(mid, page, page_size, retries, base_delay)
        if not data:
            break
        # 兼容 {data: [...]} 结构
        if isinstance(data, dict):
            data = data.get("data") or data.get("list") or []
        if not isinstance(data, list) or not data:
            break

        normalized = [_normalize(item) for item in data if isinstance(item, dict)]
        head = parse_pubdate(normalized[0]["pubdate"])
        tail = parse_pubdate(normalized[-1]["pubdate"])

        for video in normalized:
            video_date = parse_pubdate(video["pubdate"])
            if video_date is None:
                # 只有真的在按时间筛时才需要日期；不筛就照常处理
                if since or until:
                    unparsed.append(video)
                    continue
                videos.append(video)
                continue
            if since and video_date < since:
                continue
            if until and video_date > until:
                continue
            videos.append(video)

        if on_page:
            on_page(page, len(normalized), len(videos), head, tail)

        # 整页都早于起始日期 → 后面的页只会更旧
        if since and tail and tail < since:
            break
        if len(normalized) < page_size:
            break
        page += 1

    if unparsed:
        LOG.warning("有 %d 条投稿的发布日期无法解析，已跳过（不是不符合时间条件）:",
                    len(unparsed))
        for video in unparsed:
            LOG.warning("  %s | bvid=%s | 原始日期=%r",
                        video.get("title") or "(无标题)", video.get("bvid") or "?",
                        video.get("pubdate", ""))
    return videos


def _fetch_user_videos_page(mid: str, page: int, page_size: int,
                            retries: int, base_delay: float):
    """拉一页用户投稿，带预导航重试 + 指数退避。

    返回 opencli 解析后的数据；无数据返回 None。
    """
    args = ["bilibili", "user-videos", "-f", "json",
            "--limit", str(page_size), "--page", str(page), *browser_args(), mid]
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return opencli_json_with_prenav_retry(args, timeout=300)
        except RuntimeError as e:
            last_error = e
            if attempt >= retries:
                break
            delay = base_delay * (2 ** attempt)
            LOG.warning("拉取投稿列表失败（第 %d/%d 页，第 %d/%d 次），%.1fs 后重试: %s",
                        page, page, attempt + 1, retries + 1, delay,
                        str(e).splitlines()[0])
            time.sleep(delay)
    raise RuntimeError(
        f"拉取用户投稿列表失败 (mid={mid}, page={page})，已重试 {retries} 次: {last_error}"
    )


def _api_get(url: str, params: dict, timeout: float = 30) -> dict:
    payload = _api_get_json(url, params, timeout=timeout)
    if payload.get("code") != 0:
        raise RuntimeError(
            f"B站 API 返回错误: code={payload.get('code')} "
            f"message={payload.get('message', '')} ({url})"
        )
    return payload


def _api_get_json(url: str, params: dict, timeout: float = 30) -> dict:
    """GET 一个 B站公开 API 并解析 JSON（不做 code 校验，由调用方判断）。"""
    query = urllib.parse.urlencode(params)
    req = urllib.request.Request(f"{url}?{query}", headers=_API_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"B站 API 请求失败: {url}\n  {type(e).__name__}: {e}") from e


_SEASONS_LIST_API = "https://api.bilibili.com/x/polymer/web-space/seasons_series_list"
_SEASON_VIDEOS_API = "https://api.bilibili.com/x/polymer/web-space/seasons_archives_list"


def fetch_all_seasons(mid: str) -> list[dict]:
    """取用户全部合集 [{season_id, name}]。"""
    seasons: list[dict] = []
    page_num = 1
    while True:
        data = _api_get(_SEASONS_LIST_API, {"mid": mid, "page_size": 20, "page_num": page_num})
        items = (data.get("data") or {}).get("items_lists") or {}
        rows = items.get("seasons_list") or []
        for row in rows:
            meta = row.get("meta") or {}
            seasons.append({"season_id": str(meta.get("season_id", "")), "name": meta.get("name", "")})
        page = items.get("page") or {}
        if len(rows) < int(page.get("page_size", 20) or 20):
            break
        page_num += 1
    return seasons


def fetch_season_name(mid: str, season_id: str) -> str:
    target = int(season_id)
    for season in fetch_all_seasons(mid):
        if season["season_id"] and int(season["season_id"]) == target:
            return season["name"] or season_id
    raise ValueError(f"未找到合集 season_id={season_id} (mid={mid})")


def fetch_season_videos(mid: str, season_id: str) -> list[dict]:
    """取合集内全部视频（已归一化）。"""
    videos: list[dict] = []
    page_num = 1
    while True:
        data = _api_get(_SEASON_VIDEOS_API, {
            "mid": mid, "season_id": season_id,
            "sort_reverse": "false", "page_size": 30, "page_num": page_num,
        })
        body = data.get("data") or {}
        archives = body.get("archives") or []
        if not archives:
            break
        for item in archives:
            bvid = item.get("bvid", "")
            videos.append({
                "bvid": bvid,
                "title": item.get("title", ""),
                "url": f"https://www.bilibili.com/video/{bvid}",
                "pubdate": str(item.get("pubdate", "")),
                "series_name": "",
            })
        total = int(((body.get("page") or {}).get("total")) or 0)
        if len(archives) < 30 or (total and len(videos) >= total):
            break
        page_num += 1
    return videos


# ---------------------------------------------------------------------------
# 文本获取总入口
# ---------------------------------------------------------------------------

def item_dir(workdir: str | Path, vid: str) -> Path:
    """每条视频一个独立下载目录，避免并发/串行时互相捡到对方的文件。"""
    path = Path(workdir) / vid
    path.mkdir(parents=True, exist_ok=True)
    return path


def extract_text(bvid: str, cfg: dict, transcriber, workdir: str | Path) -> dict:
    """拿一个 B站视频的文本。

    返回 {text, source, media}。source 为 subtitle 或 asr；
    media 是保留的媒体路径（未被保留时为 None）。
    """
    lang = cfg["bilibili"].get("subtitle_lang")

    LOG.info("  尝试获取官方字幕...")
    subtitle = get_subtitle(bvid, lang)
    if subtitle:
        LOG.info("  命中字幕（%d 字），跳过 ASR", len(subtitle))
        return {"text": subtitle, "source": "subtitle", "media": None}

    LOG.info("  无字幕，下载音频走 ASR...")
    download_dir = item_dir(workdir, bvid)
    keep = bool(cfg.get("keep_media"))
    media = download_media(bvid, download_dir, cfg)
    try:
        text, backend, elapsed = transcriber.transcribe(media, download_dir)
        LOG.info("  ASR 完成（%s，%.1fs，%d 字）", backend, elapsed, len(text))
        return {"text": text, "source": "asr", "media": str(media) if keep else None}
    finally:
        # 连同下载的视频与派生的 .asr.m4a 一起清掉，不在临时目录留垃圾
        if not keep:
            shutil.rmtree(download_dir, ignore_errors=True)
