#!/usr/bin/env python3
"""video-transcribe · opencli 浏览器残留收尾（**全程后台**）。

用户可以随时单独跑它，也可以在 `vt.py` 结束后由收尾逻辑调用。

策略（用户 2026-10-20 定，取代 2026-10-11 的「清理优先后台，不行再转前台」）：

1. **后台**关掉 opencli 的会话 / 标签页：`close` + `unbind`，带 `--window background`
   与 `OPENCLI_WINDOW=background`（interactive 原语默认是前台窗口，不写就会弹到用户面前）。
   实测运行前后 `GetForegroundWindow()` 完全不变。
2. 只读查群组（UIA 查询，不抢焦点、不合成输入）。

**删除群组的能力已移除**：删群组只能走 Chrome 右键菜单「删除群组」，而打开该菜单必须
把窗口切到前台；全程后台的约束下这件事不该由本 skill 做。发现残留时只报告，交用户处置。

用法：
    python cleanup_browser.py               # 后台关会话 → 只读报告群组
    python cleanup_browser.py --sessions a b  # 指定 interactive 会话名
    python cleanup_browser.py --json        # 机器可读输出
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

import vt_common as C  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="cleanup_browser.py",
        description="收尾 opencli 浏览器残留：后台关会话 + 只读报告群组（全程不抢焦点）",
    )
    parser.add_argument("--config", help="JSON 配置文件路径")
    parser.add_argument("--opencli", help="opencli 命令名或路径")
    parser.add_argument("--sessions", nargs="+", metavar="SESSION",
                        help="要关闭的 interactive 会话名（默认取配置里的 cleanup_sessions）")
    parser.add_argument("--platforms", nargs="+", choices=["bilibili", "xiaohongshu"],
                        help="顺带处理这些平台的固定适配器会话 site:<平台>")
    parser.add_argument("--close-blank", action="store_true",
                        help="顺带关掉 opencli 遗留的 about:blank 占位窗口（单标签判据）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("-v", "--verbose", action="store_true", help="调试日志")
    args = parser.parse_args(argv)

    C.setup_logging(args.verbose)
    cfg = C.load_config(args.config)
    if args.opencli:
        cfg["opencli_cmd"] = args.opencli
    # 关会话这一步强制后台
    cfg["opencli_window"] = "background"
    C.configure(cfg)

    sessions = tuple(args.sessions) if args.sessions else tuple(cfg.get("cleanup_sessions") or ())
    platforms = tuple(args.platforms) if args.platforms else None

    # 单独跑时没有「运行前快照」，只能按标题 + 单标签判据处理，所以要用 --close-blank 显式开启
    blank_before = C.snapshot_chrome_windows() if args.close_blank else None

    C.LOG.info("收尾（全程后台）：会话=%s 平台=%s 关占位窗口=%s",
               list(sessions) or "无", list(platforms) if platforms else "无", args.close_blank)
    outcome = C.finalize_browser_cleanup(platforms, sessions,
                                         blank_windows_before=blank_before,
                                         close_blank_all=args.close_blank)
    released = outcome["released"]
    groups = outcome["groups"]
    C.LOG.info("已释放: close=%s unbind=%s", released["closed"], released["unbound"])

    if groups is None:
        C.LOG.info("群组检查跳过（非 Windows 或缺 cleanup_opencli_groups.ps1）")
    elif groups.get("count"):
        C.LOG.warning(
            "Chrome 里仍有 %d 个 OpenCLi Browser 群组：%s\n"
            "  本 skill 全程后台，不会切前台删除。要清掉："
            "① 右键该群组 →「删除群组」；② 重启 Chrome。",
            groups["count"], groups.get("names"),
        )
    else:
        C.LOG.info("Chrome 里没有 OpenCLi Browser 群组，干净")

    if args.json:
        print(json.dumps(outcome, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
