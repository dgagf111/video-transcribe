#!/usr/bin/env python3
"""video-transcribe · 命令行入口。

把小红书 / B站 的视频变成纯文本转录（.txt 或 stdout）：
    B站：官方字幕优先 → 无字幕则下载音频走 ASR
    小红书：下载视频 → 抽音频 → ASR

不含任何 LLM 后处理（不做格式化、不做内容过滤），输出就是 ASR/字幕的原文。

用法示例：
    python vt.py "https://www.bilibili.com/video/BV1xx411c7mD"
    python vt.py --user <mid> --limit 5 --since 2026-01-01
    python vt.py --season "https://space.bilibili.com/<mid>/lists/<season_id>"
    python vt.py "https://www.xiaohongshu.com/explore/xxxx?xsec_token=yyy" --stdout
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _setup_stdio() -> None:
    """被管道/agent 捕获时强制 UTF-8；真终端保留本地编码（Windows 中文控制台是 cp936）。

    必须在 argparse 之前执行，否则 --help 的中文会按本地编码写出，
    而捕获方通常按 UTF-8 解码，直接变乱码。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            if not stream.isatty():
                stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


_setup_stdio()

import vt_bilibili  # noqa: E402
import vt_common as C  # noqa: E402
import vt_xiaohongshu  # noqa: E402
from vt_asr import AsrError, Transcriber  # noqa: E402

LOG = logging.getLogger("video-transcribe")

_manifest_lock = threading.Lock()
_stdout_lock = threading.Lock()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vt.py",
        description="小红书 / B站 视频 → 纯文本转录（下载 + ASR，无 LLM 后处理）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    target = parser.add_argument_group("目标（url / --user / --season 三选一）")
    target.add_argument("url", nargs="?", help="单个视频链接或裸 ID（自动识别平台）")
    target.add_argument("--url", dest="url_opt", help="同上（等价写法）")
    target.add_argument("--user", help="用户主页链接或 ID：B站 mid / 小红书 user_id")
    target.add_argument("--season", help="B站合集链接 https://space.bilibili.com/{mid}/lists/{id}")
    target.add_argument("--platform", choices=["bilibili", "xiaohongshu"],
                        help="强制指定平台（--user 且 ID 形态有歧义时用）")

    out = parser.add_argument_group("输出")
    out.add_argument("-o", "--output", help="输出目录（默认 ./transcripts）")
    # 三态：不写 → 用配置里的值；--overwrite → 强制重做；--no-overwrite → 强制跳过已存在
    out.add_argument("--overwrite", dest="overwrite", action="store_true", default=None,
                     help="已存在同名 .txt 时重新转录")
    out.add_argument("--no-overwrite", dest="overwrite", action="store_false",
                     help="已存在同名 .txt 时跳过（覆盖配置文件里的 overwrite: true）")
    out.add_argument("--stdout", action="store_true", help="额外把转录文本打印到 stdout")
    out.add_argument("--stdout-only", action="store_true", help="只打印到 stdout，不写文件")
    out.add_argument("--no-manifest", action="store_true", help="不写 manifest.jsonl")

    filt = parser.add_argument_group("筛选（仅 B站）")
    filt.add_argument("--limit", type=int, help="最多处理多少个视频")
    filt.add_argument("--since", help="只处理该日期之后（含）发布的视频，YYYY-MM-DD")
    filt.add_argument("--until", help="只处理该日期之前（含）发布的视频，YYYY-MM-DD")
    filt.add_argument("--from-date", dest="from_date", help="时间范围起点 YYYY-MM-DD")
    filt.add_argument("--to-date", dest="to_date", help="时间范围终点 YYYY-MM-DD")
    filt.add_argument("--series", nargs="+", help="只处理指定合集（名称，可多个）")

    asr = parser.add_argument_group("ASR")
    asr.add_argument("--asr-backend", choices=["auto", "api", "local", "mlx", "faster-whisper", "none"])
    asr.add_argument("--api-base", help="OpenAI 兼容接口，如 http://host:port/v1")
    asr.add_argument("--api-key", help="接口密钥")
    asr.add_argument("--asr-model",
                     help="ASR 模型名（走 API 时必填，取决于你的接口部署了哪个模型）")
    asr.add_argument("--language", help="转录语言，zh / en / auto（默认 zh）")

    misc = parser.add_argument_group("运行")
    misc.add_argument("--config", help="JSON 配置文件路径")
    misc.add_argument("--opencli", help="opencli 命令名或路径（默认 opencli）")
    misc.add_argument("--temp-dir", help="媒体临时目录（默认系统临时目录）")
    misc.add_argument("--keep-tab", action="store_true",
                      help="命令结束后保留浏览器标签页（默认关闭；开启会攒下标签页群组）")
    misc.add_argument("--site-session", choices=["ephemeral", "persistent"],
                      help="opencli 站点会话：ephemeral 用完即弃（默认），persistent 常驻")
    misc.add_argument("--window-mode", choices=["background", "foreground"],
                      help="opencli 浏览器窗口模式，默认 background（不抢焦点）")
    misc.add_argument("--prenav-retry-foreground", action="store_true",
                      help="预导航被拒时允许改用前台窗口重试（默认后台重试，不抢焦点）")
    misc.add_argument("--no-cleanup", action="store_true",
                      help="跳过收尾（默认会关掉 opencli 留下的标签页与会话）")
    misc.add_argument("--background-only", dest="background_only", action="store_true", default=None,
                      help="全程后台：不允许任何环节把窗口切到前台（默认开；收尾只报告群组，不删）")
    misc.add_argument("--allow-foreground", dest="background_only", action="store_false",
                      help="允许预导航重试换前台窗口（收尾仍只做后台动作）")
    misc.add_argument("--cleanup-sessions", nargs="+", metavar="SESSION",
                      help="收尾时要关闭的 interactive 会话名（默认 __doctor__）")
    misc.add_argument("--keep-media", action="store_true", help="保留下载的媒体文件")
    misc.add_argument("--skip-asr", action="store_true", help="只下载不转录（调试用）")
    misc.add_argument("--fail-on-empty", dest="fail_on_empty", action="store_true", default=None,
                      help="转录结果为空也算失败（默认不算；定时任务想严格判成败时开）")
    misc.add_argument("--no-fail-on-empty", dest="fail_on_empty", action="store_false",
                      help="转录结果为空仍算成功（默认）")
    misc.add_argument("--workers", type=int, help="B站并行线程数（默认 4）")
    misc.add_argument("--max-consecutive-failures", type=int, metavar="N",
                      help="连续失败 N 条就中止剩余任务（默认 3；0 表示不中止）")
    misc.add_argument("--jitter", dest="jitter", action="store_true", default=None,
                      help="启用小红书访问抖动（默认启用）")
    misc.add_argument("--no-jitter", dest="jitter", action="store_false",
                      help="关闭小红书访问抖动（自担风控风险）")
    misc.add_argument("--jitter-min", type=float, help="抖动下限秒数（默认 60）")
    misc.add_argument("--jitter-max", type=float, help="抖动上限秒数（默认 180）")
    misc.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    return parser


def build_config(args) -> dict:
    cfg = C.load_config(args.config)
    override: dict = {}

    def put(key, value):
        if value is not None and value is not False:
            override[key] = value

    put("output_dir", args.output)
    put("opencli_cmd", args.opencli)
    put("temp_dir", args.temp_dir)
    put("keep_media", args.keep_media)
    put("skip_asr", args.skip_asr)
    put("stdout", args.stdout)
    put("stdout_only", args.stdout_only)
    # overwrite 是三态参数，不能走 put()（put 会吞掉 False）
    if args.overwrite is not None:
        override["overwrite"] = args.overwrite
    put("opencli_site_session", args.site_session)
    put("opencli_window", args.window_mode)
    if args.keep_tab:
        override["opencli_keep_tab"] = True
    if args.prenav_retry_foreground:
        override["prenav_retry_foreground"] = True
    if args.no_cleanup:
        override["cleanup_browser"] = False
    # background_only 默认 True，用三态参数显式覆盖（put() 会吞掉 False，不能走它）
    if args.background_only is not None:
        override["background_only"] = args.background_only
    if args.cleanup_sessions:
        override["cleanup_sessions"] = args.cleanup_sessions
    if args.no_manifest:
        override["manifest"] = False
    if args.fail_on_empty is not None:
        override["fail_on_empty"] = args.fail_on_empty
    # 批量失败熔断阈值：0 或负数表示不熔断
    if args.max_consecutive_failures is not None:
        override["max_consecutive_failures"] = args.max_consecutive_failures

    asr_override: dict = {}
    if args.asr_backend:
        asr_override["backend"] = args.asr_backend
    if args.api_base:
        asr_override["api_base"] = args.api_base
    if args.api_key:
        asr_override["api_key"] = args.api_key
    if args.asr_model:
        asr_override["model"] = args.asr_model
    if args.language:
        asr_override["language"] = args.language
    if args.workers:
        override["bilibili"] = {**override.get("bilibili", {}), "workers": args.workers}
    if args.jitter is not None:
        override["jitter"] = {**override.get("jitter", {}), "enabled": args.jitter}
    if args.jitter_min is not None:
        override["jitter"] = {**override.get("jitter", {}), "min": args.jitter_min}
    if args.jitter_max is not None:
        override["jitter"] = {**override.get("jitter", {}), "max": args.jitter_max}
    if asr_override:
        override["asr"] = asr_override

    return C.deep_merge(cfg, override)


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------

def target_path(cfg: dict, platform: str, author: str, series: str, title: str, vid: str) -> Path:
    """{output}/{作者}[/{合集}]/{标题} [{id}].txt"""
    base = Path(str(cfg["output_dir"])).expanduser()
    parts = [C.safe_filename(author or "未知作者", 80)]
    if platform == "bilibili" and series:
        parts.append(C.safe_filename(series, 80))
    name = f"{C.safe_filename(title or vid)} [{vid}].txt"
    return base.joinpath(*parts) / name


def write_txt(path: Path, text: str) -> None:
    """写文本文件。**显式 LF**：Windows 下 Path.write_text 默认会把 \\n 翻成 \\r\\n，
    导致同一批产出在不同平台行尾不一致（进 git 或 Obsidian 后会看出来）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write((text or "").rstrip() + "\n")


def write_manifest(cfg: dict, record: dict) -> None:
    if not cfg.get("manifest"):
        return
    path = Path(str(cfg["output_dir"])).expanduser() / "manifest.jsonl"
    with _manifest_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def emit_text(cfg: dict, text: str, vid: str) -> None:
    """按需把文本打到 stdout。日志全部走 stderr，stdout 保持干净。"""
    if not (cfg.get("stdout") or cfg.get("stdout_only")):
        return
    with _stdout_lock:
        if cfg.get("stdout_only") and cfg.get("_batch"):
            print(f"\n===== {vid} =====", file=sys.stderr)
        print((text or "").strip())
        sys.stdout.flush()


# ---------------------------------------------------------------------------
# 单条处理
# ---------------------------------------------------------------------------

def handle_item(*, cfg: dict, platform: str, vid: str, url: str, title: str, author: str,
                date_str: str = "", series: str = "", extract_fn, download_fn,
                progress: str = "") -> dict:
    """统一处理一条：算目标路径 → 跳过判断 → 取文本 → 落盘 → manifest。"""
    head = f"{progress} " if progress else ""
    path = target_path(cfg, platform, author, series, title, vid)

    if cfg.get("stdout_only"):
        path = Path()  # 占位，不落盘
    elif path.exists() and not cfg.get("overwrite"):
        LOG.info("%s跳过（已存在）: %s", head, path)
        return {"status": "skipped", "vid": vid, "txt": str(path)}

    if cfg.get("skip_asr"):
        media = download_fn()
        LOG.info("%s--skip-asr：媒体已下载 %s", head, media)
        return {"status": "downloaded", "vid": vid, "media": str(media)}

    LOG.info("%s处理中: %s", head, title or vid)
    result = extract_fn()
    text = (result.get("text") or "").strip()
    source = result.get("source", "")

    empty = not text
    if empty:
        LOG.warning("%s转录结果为空（视频可能没有口播），仍写出空文件: %s", head, title or vid)

    if not cfg.get("stdout_only"):
        write_txt(path, text)
        if empty:
            LOG.info("%s完成（%s，0 字，空转录）→ %s", head, source, path)
        else:
            LOG.info("%s完成（%s，%d 字）→ %s", head, source, len(text), path)

    emit_text(cfg, text, vid)

    record = {
        "platform": platform,
        "id": vid,
        "url": url,
        "title": title,
        "author": author,
        "date": date_str,
        "series": series,
        "source": source,
        "chars": len(text),
        "txt": None if cfg.get("stdout_only") else str(path),
        "media": result.get("media"),
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    write_manifest(cfg, record)
    # 空转录单独一个状态：它既不是「失败」（视频确实可能没有口播），
    # 也不该混进「成功」里让批量汇总看不见（用户 2026-10-20 要求）。
    status = "empty" if empty else "ok"
    return {"status": status, **record}


# ---------------------------------------------------------------------------
# B站
# ---------------------------------------------------------------------------

def time_filter(args) -> tuple[date | None, date | None]:
    """把 CLI 时间参数解析成 (since, until)。"""
    has_range = bool(args.from_date or args.to_date)
    modes = sum([has_range, bool(args.since), bool(args.until)])
    if modes > 1:
        raise ValueError("时间参数互斥：--from-date/--to-date、--since、--until 只能选一种")

    if has_range:
        if not (args.from_date and args.to_date):
            raise ValueError("范围模式需要同时给出 --from-date 与 --to-date")
        start = datetime.strptime(args.from_date, "%Y-%m-%d").date()
        end = datetime.strptime(args.to_date, "%Y-%m-%d").date()
        if start > end:
            raise ValueError(f"--from-date ({start}) 不能晚于 --to-date ({end})")
        return start, end
    if args.since:
        return datetime.strptime(args.since, "%Y-%m-%d").date(), None
    if args.until:
        return None, datetime.strptime(args.until, "%Y-%m-%d").date()
    return None, None


def _filter_by_time(videos: list[dict], since: date | None, until: date | None) -> list[dict]:
    """按发布时间筛选。**日期解析失败的一条都不静默丢弃**（用户 2026-10-20 要求）。

    以前这里对 `video_date is None` 直接 `continue`，视频会既不报错也不出现在结果里，
    看起来像「不符合时间条件」，其实只是日期没解析出来——最难发现的一类问题。
    现在改成：逐条记 WARNING（带 id / 标题 / 原始日期串），并在结尾汇总一条。
    """
    kept: list[dict] = []
    unparsed: list[dict] = []
    for video in videos:
        raw = video.get("pubdate", "")
        video_date = vt_bilibili.parse_pubdate(raw)
        if video_date is None:
            unparsed.append(video)
            continue
        if since and video_date < since:
            continue
        if until and video_date > until:
            continue
        kept.append(video)

    if unparsed:
        LOG.warning("有 %d 条视频的发布日期无法解析，已跳过（不是不符合时间条件）:",
                    len(unparsed))
        for video in unparsed:
            LOG.warning("  %s | id=%s | 原始日期=%r",
                        video.get("title") or "(无标题)",
                        video.get("bvid") or "?", video.get("pubdate", ""))
        LOG.warning("  这些视频未被处理；如需处理请去掉时间筛选参数，或修正日期解析。")
    return kept


def _user_name_from(videos: list[dict]) -> str:
    """从前两个视频的元数据里取作者名，避免多打接口。"""
    for video in videos[:2]:
        bvid = video.get("bvid")
        if not bvid:
            continue
        try:
            info = vt_bilibili.get_video_info(bvid)
        except Exception:  # noqa: BLE001
            continue
        name = vt_bilibili.extract_author_name(info.get("author"))
        if name:
            return name
    return "未知作者"


def _merge_seasons(cfg: dict, mid: str, videos: list[dict]) -> tuple[list[dict], list[str]]:
    """补齐 series_name，并把 opencli 列表里漏掉的合集视频补进来。"""
    try:
        seasons = vt_bilibili.fetch_all_seasons(mid)
    except Exception as e:  # noqa: BLE001
        LOG.warning("获取合集列表失败，跳过合集信息: %s", e)
        return videos, []

    if not seasons:
        return videos, []

    known = {v["bvid"] for v in videos if v.get("bvid")}
    season_of: dict[str, str] = {}
    extra: list[dict] = []
    for season in seasons:
        sid, name = season["season_id"], season["name"]
        if not sid or not name:
            continue
        try:
            rows = vt_bilibili.fetch_season_videos(mid, sid)
        except Exception as e:  # noqa: BLE001
            LOG.warning("获取合集「%s」视频失败，跳过: %s", name, e)
            continue
        for row in rows:
            season_of[row["bvid"]] = name
            if row["bvid"] not in known:
                row["series_name"] = name
                extra.append(row)
                known.add(row["bvid"])

    for video in videos:
        if video["bvid"] in season_of:
            video["series_name"] = season_of[video["bvid"]]

    if extra:
        LOG.info("从 %d 个合集中补全 %d 个视频", len(seasons), len(extra))
    videos.extend(extra)
    return videos, sorted({v["series_name"] for v in videos if v.get("series_name")})


def _run_pool(cfg: dict, jobs: list, worker) -> list[dict]:
    """并发跑一批任务，单个失败只记录不中断。

    失败明细会完整保留（title / vid / url / error），由 `report_batch_failures`
    汇总后输出——用户 2026-10-20 要求：批量部分失败时必须返回具体失败的视频。

    另外两条（2026-10-20 第二轮）：
    - **连续失败熔断**：连续 N 条失败就停掉未开始的任务（疑似风控/环境挂了），
      否则会把剩下几十条一条条跑完才发现全废。
    - **Ctrl+C 不再等队列跑完**：中断时 `cancel_futures=True` 直接丢掉排队的任务。
    """
    if not jobs:
        return []
    workers = max(1, min(int(cfg["bilibili"].get("workers", 4)), len(jobs)))
    limit = int(cfg.get("max_consecutive_failures", 3) or 0)
    LOG.info("并行处理 %d 个视频（%d 线程）", len(jobs), workers)

    results: list[dict] = []
    consecutive = 0
    aborted = False
    executor = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = {executor.submit(worker, job, f"[{i}/{len(jobs)}]"): job
                   for i, job in enumerate(jobs, 1)}
        try:
            for future in as_completed(futures):
                job = futures[future]
                try:
                    result = future.result()
                except Exception as e:  # noqa: BLE001
                    LOG.error("处理失败: %s (%s): %s",
                              job.get("title", ""), job.get("bvid") or job.get("id", ""), e)
                    results.append({
                        "status": "failed",
                        "vid": job.get("bvid") or job.get("id", ""),
                        "title": job.get("title", ""),
                        "url": job.get("url", ""),
                        "error": str(e),
                    })
                    consecutive += 1
                    if limit and consecutive >= limit:
                        LOG.error("连续 %d 条失败，疑似风控或环境异常，中止剩余任务", consecutive)
                        aborted = True
                        for pending in futures:
                            pending.cancel()
                        break
                    continue

                results.append(result)
                if result.get("status") != "failed":
                    consecutive = 0
        except KeyboardInterrupt:
            LOG.warning("已中断：丢弃未开始的任务（已在跑的最多 %d 个会自然结束）", workers)
            for pending in futures:
                pending.cancel()
            raise
    finally:
        executor.shutdown(wait=True, cancel_futures=aborted)

    if aborted:
        LOG.error("批量已熔断，未处理的视频：%d 个", sum(1 for f in futures if f.cancelled()))
    return results


def _sort_for_limit(videos: list[dict]) -> list[dict]:
    """按发布时间**降序**（新→旧）排序，供 --limit 取最新的 N 条。

    用户 2026-10-20 要求修正：此前是升序 + `[:limit]`，导致 `--limit 5`
    拿到的是**最早**的 5 个投稿，与直觉相反。
    """
    return sorted(videos, key=lambda v: str(v.get("pubdate") or ""), reverse=True)


def report_batch_failures(results: list[dict], label: str = "批量",
                          *, fail_on_empty: bool = False) -> int:
    """汇总批量结果：列出**每一条**失败的视频，返回失败条数。

    用户 2026-10-20 要求：批量部分失败时必须返回具体失败的视频。
    空转录（status="empty"）单独计数：它通常意味着视频没有口播，不算失败，
    但必须显式喊出来，不能混进「成功」里静默掉。`fail_on_empty=True` 时才计入失败。
    """
    failed = [r for r in results if r.get("status") == "failed"]
    empty = [r for r in results if r.get("status") == "empty"]
    ok = sum(1 for r in results if r.get("status") == "ok")
    skipped = sum(1 for r in results if r.get("status") == "skipped")
    downloaded = sum(1 for r in results if r.get("status") == "downloaded")

    LOG.info("%s汇总：成功 %d，跳过 %d，仅下载 %d，空转录 %d，失败 %d",
             label, ok, skipped, downloaded, len(empty), len(failed))

    if empty:
        level = LOG.error if fail_on_empty else LOG.warning
        level("%s有 %d 条转录结果为空（视频可能没有口播，已写出空文件）：", label, len(empty))
        for index, item in enumerate(empty, 1):
            level("  %d) %s | id=%s | %s",
                  index, item.get("title") or "(无标题)",
                  item.get("vid") or "?", item.get("url") or "(无链接)")
        if fail_on_empty:
            LOG.error("  （--fail-on-empty 生效：空转录计入失败）")

    if not failed:
        return len(empty) if fail_on_empty else 0

    LOG.error("%s有 %d 条失败：", label, len(failed))
    for index, item in enumerate(failed, 1):
        LOG.error("  %d) %s | id=%s | %s\n     原因: %s",
                  index, item.get("title") or "(无标题)",
                  item.get("vid") or "?", item.get("url") or "(无链接)",
                  item.get("error") or "(无错误信息)")
    return len(failed) + (len(empty) if fail_on_empty else 0)


def _bilibili_worker_factory(cfg: dict, transcriber, workdir: Path, author: str):
    def worker(job: dict, progress: str) -> dict:
        vid = job["bvid"]
        return handle_item(
            cfg=cfg, platform="bilibili", vid=vid, url=job.get("url", ""),
            title=job.get("title", ""), author=author, date_str=str(job.get("pubdate", "")),
            series=job.get("series_name", ""), progress=progress,
            extract_fn=lambda: vt_bilibili.extract_text(vid, cfg, transcriber, workdir),
            download_fn=lambda: vt_bilibili.download_media(vid, vt_bilibili.item_dir(workdir, vid), cfg),
        )
    return worker


def _single_exit_code(cfg: dict, result: dict, title: str, url: str) -> int:
    """单条模式的退出码：只在 `--fail-on-empty` 且结果为空时返回 1。"""
    if result.get("status") == "empty" and cfg.get("fail_on_empty"):
        LOG.error("转录结果为空且 --fail-on-empty 生效: %s | %s", title or "(无标题)", url)
        return 1
    return 0


def run_bilibili_single(cfg: dict, transcriber, workdir: Path, raw_url: str) -> int:
    bvid = vt_bilibili.extract_bvid(raw_url)
    if not bvid:
        raise ValueError(f"无法从输入中提取 bvid: {raw_url}")
    url = f"https://www.bilibili.com/video/{bvid}"

    info = vt_bilibili.get_video_info(bvid)
    title = info.get("title") or bvid
    author = vt_bilibili.extract_author_name(info.get("author")) or "未知作者"
    pubdate = vt_bilibili.info_date(info)
    LOG.info("视频: %s | 作者: %s | bvid: %s | 发布: %s", title, author, bvid, pubdate or "未知")

    parts = str(info.get("parts") or "1")
    if parts.isdigit() and int(parts) > 1:
        LOG.warning("该视频有 %s 个分P，本 skill 只处理默认的 P1（不支持 --page 选P）", parts)

    result = handle_item(
        cfg=cfg, platform="bilibili", vid=bvid, url=url, title=title,
        author=author, date_str=str(pubdate),
        extract_fn=lambda: vt_bilibili.extract_text(bvid, cfg, transcriber, workdir),
        download_fn=lambda: vt_bilibili.download_media(bvid, vt_bilibili.item_dir(workdir, bvid), cfg),
    )
    return _single_exit_code(cfg, result, title, url)


def run_bilibili_user(cfg: dict, transcriber, workdir: Path, user_input: str, args) -> int:
    mid = vt_bilibili.extract_mid(user_input)
    since, until = time_filter(args)
    LOG.info("B站 mid: %s", mid)

    videos = vt_bilibili.iter_user_videos(
        mid, cfg, since=since, until=until,
        on_page=lambda page, got, total, head, tail:
            LOG.info("  第 %d 页：%d 条（累计 %d）%s ~ %s", page, got, total,
                     head or "?", tail or "?"),
    )
    if not videos:
        raise ValueError(f"没有取到任何投稿视频 (mid={mid})")

    videos, detected = _merge_seasons(cfg, mid, videos)

    if since or until:
        videos = _filter_by_time(videos, since, until)
        if not videos:
            raise ValueError("时间筛选后没有匹配的视频")

    if args.series:
        valid = set(detected)
        missing = [s for s in args.series if s not in valid]
        if missing:
            raise ValueError(f"合集不存在: {', '.join(missing)}；可选合集: {detected or '(无)'}")
        videos = [v for v in videos if v.get("series_name") in set(args.series)]

    author = _user_name_from(videos)
    LOG.info("作者: %s | 待处理 %d 个视频", author, len(videos))
    LOG.info("合集: %s", ", ".join(sorted({v['series_name'] for v in videos if v.get('series_name')})) or "无")

    videos = _sort_for_limit(videos)
    if args.limit and args.limit > 0:
        videos = videos[:args.limit]
        LOG.info("限制处理数量: %d（取最新的）", args.limit)

    cfg["_batch"] = True
    return report_batch_failures(
        _run_pool(cfg, videos, _bilibili_worker_factory(cfg, transcriber, workdir, author)),
        "B站用户投稿", fail_on_empty=bool(cfg.get("fail_on_empty")))


def run_bilibili_season(cfg: dict, transcriber, workdir: Path, user_input: str, args) -> int:
    mid, season_id = vt_bilibili.extract_season_params(user_input)
    since, until = time_filter(args)
    name = vt_bilibili.fetch_season_name(mid, season_id)
    LOG.info("合集: %s (mid=%s, season_id=%s)", name, mid, season_id)

    videos = vt_bilibili.fetch_season_videos(mid, season_id)
    if not videos:
        raise ValueError(f"合集「{name}」里没有视频")
    for video in videos:
        video["series_name"] = name

    # 用户 2026-10-20 修正：合集模式此前放行了时间参数却从不生效，
    # 现在与 --user 模式一致地应用 --since/--until/--from-date/--to-date。
    if since or until:
        before = len(videos)
        videos = _filter_by_time(videos, since, until)
        LOG.info("时间筛选: %d → %d 个视频", before, len(videos))
        if not videos:
            raise ValueError(f"合集「{name}」按时间筛选后没有匹配的视频")

    author = _user_name_from(videos)
    videos = _sort_for_limit(videos)
    if args.limit and args.limit > 0:
        videos = videos[:args.limit]
        LOG.info("限制处理数量: %d（取最新的）", args.limit)
    LOG.info("作者: %s | 待处理 %d 个视频", author, len(videos))

    cfg["_batch"] = True
    return report_batch_failures(
        _run_pool(cfg, videos, _bilibili_worker_factory(cfg, transcriber, workdir, author)),
        f"合集「{name}」", fail_on_empty=bool(cfg.get("fail_on_empty")))


# ---------------------------------------------------------------------------
# 小红书
# ---------------------------------------------------------------------------

def _xhs_worker_factory(cfg: dict, transcriber, workdir: Path, jitter):
    def worker(job: dict, progress: str) -> dict:
        url = job["url"]
        vid = job.get("id") or vt_xiaohongshu.extract_note_id(url)
        title = job.get("title") or vid
        author = job.get("author") or "未知作者"
        return handle_item(
            cfg=cfg, platform="xiaohongshu", vid=vid, url=url, title=title, author=author,
            date_str=str(job.get("date", "")), progress=progress,
            extract_fn=lambda: vt_xiaohongshu.extract_text(url, cfg, transcriber, workdir, jitter),
            download_fn=lambda: vt_xiaohongshu.download_media(
                url, vt_xiaohongshu.item_dir(workdir, vid), jitter),
        )
    return worker


def run_xhs_single(cfg: dict, transcriber, workdir: Path, url: str, jitter) -> int:
    meta = vt_xiaohongshu.fetch_meta(url, jitter)
    title = meta["title"] or meta["vid"]
    author = meta["author"] or "未知作者"
    LOG.info("笔记: %s | 作者: %s | id: %s", title, author, meta["vid"])
    worker = _xhs_worker_factory(cfg, transcriber, workdir, jitter)
    result = worker({"id": meta["vid"], "url": url, "title": title, "author": author,
                     "date": meta["date"]}, "")
    return _single_exit_code(cfg, result, title, url)


def run_xhs_user(cfg: dict, transcriber, workdir: Path, user_input: str, args, jitter) -> int:
    notes = vt_xiaohongshu.iter_user_notes(user_input, cfg, jitter)
    if not notes:
        raise ValueError(f"该用户没有视频笔记: {user_input}")

    try:
        author = vt_xiaohongshu.fetch_meta(notes[0]["url"], jitter)["author"] or "未知作者"
    except Exception as e:  # noqa: BLE001
        LOG.warning("获取作者名失败，使用占位作者: %s", e)
        author = "未知作者"

    # 小红书笔记没有发布日期字段，用笔记 ID 前 8 位十六进制（Unix 秒）排序，
    # 这样 --limit 才是「取最新的 N 条」，与 B站一致（此前是直接切前 N 条，顺序不可控）。
    notes = sorted(notes, key=lambda n: vt_xiaohongshu.note_timestamp(n.get("id") or ""),
                   reverse=True)
    if args.limit and args.limit > 0:
        notes = notes[:args.limit]
        LOG.info("限制处理数量: %d（取最新的）", args.limit)
    LOG.info("作者: %s | 待处理 %d 个视频（串行）", author, len(notes))

    cfg["_batch"] = True
    worker = _xhs_worker_factory(cfg, transcriber, workdir, jitter)
    results: list[dict] = []
    limit = int(cfg.get("max_consecutive_failures", 3) or 0)
    consecutive = 0
    for index, note in enumerate(notes, 1):
        job = {**note, "author": author}
        try:
            results.append(worker(job, f"[{index}/{len(notes)}]"))
            consecutive = 0
        except Exception as e:  # noqa: BLE001
            vid = note.get("id", "")
            LOG.error("处理失败: %s (%s): %s", note.get("title", ""), vid, e)
            results.append({
                "status": "failed",
                "vid": vid,
                "title": note.get("title", ""),
                "url": note.get("url", ""),
                "error": str(e),
            })
            consecutive += 1
            if limit and consecutive >= limit:
                remaining = len(notes) - index
                LOG.error(
                    "连续 %d 条失败，疑似触发小红书风控，中止剩余 %d 条。\n"
                    "  建议：等 25 分钟以上再重跑（风控没有明确解除信号），"
                    "或调大 jitter.min/max 降低频率。", consecutive, remaining)
                break
    return report_batch_failures(results, "小红书用户主页",
                                 fail_on_empty=bool(cfg.get("fail_on_empty")))


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    C.setup_logging(args.verbose)
    cfg = build_config(args)
    C.configure(cfg)

    raw_url = args.url_opt or args.url
    targets = [bool(raw_url), bool(args.user), bool(args.season)]
    if sum(targets) != 1:
        parser.error("必须且只能指定一个目标：位置参数 url、--url、--user、--season")

    if (args.since or args.until or args.from_date or args.to_date or args.series) and not args.season \
            and not (args.user and _is_bilibili_user(args.user, args.platform)):
        parser.error("--since/--until/--from-date/--to-date/--series 只适用于 B站用户或合集")

    # 时间参数在**任何浏览器动作之前**就校验掉：否则参数写错也要先跑一次
    # opencli doctor + 收尾，白白开合一次浏览器会话，日志还会掩盖真正的报错。
    if args.season or (args.user and _is_bilibili_user(args.user, args.platform)):
        try:
            time_filter(args)
        except ValueError as e:
            parser.error(str(e))

    workdir = C.temp_dir_for(cfg)
    LOG.debug("媒体临时目录: %s", workdir)

    # 记录本次用到哪些平台，收尾时据此关闭对应浏览器会话
    used_platforms: tuple[str, ...] = ()
    if args.season or (args.user and _is_bilibili_user(args.user, args.platform)):
        used_platforms = ("bilibili",)
    elif args.user:
        used_platforms = ("xiaohongshu",)
    elif raw_url:
        detected = args.platform or C.detect_platform(raw_url)
        used_platforms = (detected,) if detected else ()

    transcriber = Transcriber(cfg)
    jitter = C.make_jitter(cfg)
    # 抖动只对小红书生效，别在纯 B站任务里打这句（会被误读成配置有问题）
    if "xiaohongshu" in used_platforms:
        if jitter:
            LOG.info("小红书访问抖动已启用: %.0f~%.0fs（每 %d 次保底一次高延迟；仅小红书任务生效）",
                     jitter._min, jitter._max, jitter._guarantee_interval)  # noqa: SLF001
        else:
            LOG.info("访问抖动已关闭（小红书任务将全速访问，注意风控）")

    # 拍一张 Chrome 窗口快照：收尾时只关「本次运行新出现」的 about:blank 占位窗口，
    # 绝不碰用户自己原有的空白窗口（见 close_blank_container_windows）。
    chrome_windows_before = C.snapshot_chrome_windows()

    try:
        if args.season:
            LOG.info("== B站合集模式 ==")
            C.check_opencli(cfg)
            failed = run_bilibili_season(cfg, transcriber, workdir, args.season, args)
        elif args.user:
            platform = args.platform or C.detect_user_platform(args.user)
            if platform is None:
                raise ValueError(
                    f"无法判断 --user 的平台: {args.user}\n"
                    "  B站请给纯数字 mid 或 space.bilibili.com 链接；"
                    "小红书请给 user/profile 链接或 user_id；或用 --platform 强制指定"
                )
            LOG.info("== %s 批量模式（--user）==", platform)
            C.check_opencli(cfg)
            if platform == "bilibili":
                failed = run_bilibili_user(cfg, transcriber, workdir, args.user, args)
            else:
                failed = run_xhs_user(cfg, transcriber, workdir, args.user, args, jitter)
        else:
            platform = args.platform or C.detect_platform(raw_url)
            if platform is None:
                raise ValueError(
                    f"无法识别平台: {raw_url}\n"
                    "  支持 bilibili.com / b23.tv / BV 号，以及 xiaohongshu.com / xhslink.com 链接"
                )
            LOG.info("== %s 单视频模式 ==", platform)
            C.check_opencli(cfg)
            if platform == "bilibili":
                failed = run_bilibili_single(cfg, transcriber, workdir, raw_url)
            else:
                failed = run_xhs_single(cfg, transcriber, workdir, raw_url, jitter)
    except (ValueError, FileNotFoundError) as e:
        LOG.error("参数或环境错误: %s", e)
        return 2
    except (RuntimeError, AsrError) as e:
        LOG.error("执行失败: %s", e)
        return 1
    except KeyboardInterrupt:
        LOG.warning("已被用户中断")
        return 130
    finally:
        # 收尾（用户硬性要求）：关掉 opencli 留下的标签页与会话，
        # 不留名为 `OpenCLi Browser` 的标签页群组。
        # 策略是「**全程后台**」（用户 2026-10-20 定，取代 2026-10-11 的「不行再转前台」）：
        # 关会话走后台；删群组只能靠右键菜单（必须前台），本 skill 不再尝试，
        # 发现残留只报告，由用户自己处置。
        # 注意：退出码 0 不等于清干净了，判据以 Chrome 里群组真的消失为准。
        if cfg.get("cleanup_browser", True):
            try:
                outcome = C.finalize_browser_cleanup(
                    used_platforms, tuple(cfg.get("cleanup_sessions") or ()),
                    blank_windows_before=chrome_windows_before)
            except Exception as e:  # noqa: BLE001
                LOG.debug("浏览器收尾失败: %s", e)
            else:
                groups = outcome.get("groups")
                if groups is None:
                    LOG.debug("群组检查跳过（非 Windows 或缺 cleanup_opencli_groups.ps1）")
                elif groups.get("count"):
                    LOG.warning(
                        "Chrome 里仍有 %d 个 OpenCLi Browser 群组残留：%s\n"
                        "  本 skill 全程后台，不会切前台删除。要清掉："
                        "① 右键该群组 →「删除群组」；② 重启 Chrome。",
                        groups["count"], groups.get("names"),
                    )
                else:
                    LOG.info("已执行 opencli 收尾，Chrome 内无 OpenCLi Browser 群组")

    if failed:
        LOG.error("全部完成，但有 %d 条失败（明细见上方列表）", failed)
        return 1
    LOG.info("全部完成")
    return 0


def _is_bilibili_user(user_input: str, forced: str | None) -> bool:
    if forced:
        return forced == "bilibili"
    return C.detect_user_platform(user_input) == "bilibili"


if __name__ == "__main__":
    raise SystemExit(main())
