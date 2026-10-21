"""video-transcribe · 公共层。

配置合并、日志、文件名安全化、抖动、opencli 调用与跨进程互斥锁。

纯标准库实现：不 import requests / pyyaml / openai / dotenv。
所有对 opencli 的调用统一走 opencli_launcher()，以解决 Windows 上
npm 装出的 .cmd / .ps1 shim 无法被 CreateProcess 直接执行的问题。
"""

from __future__ import annotations

import errno
import json
import logging
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import nullcontext
from pathlib import Path

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

LOG = logging.getLogger("video-transcribe")

# ---------------------------------------------------------------------------
# 默认配置
# ---------------------------------------------------------------------------

DEFAULTS: dict = {
    # ---- 运行 ----
    "output_dir": "./transcripts",
    "temp_dir": None,          # None → 系统临时目录下的 video-transcribe/
    "opencli_cmd": "opencli",
    # 浏览器类命令的收尾开关：命令结束后不保留标签页租约、站点会话用完即弃，
    # 避免在 Chrome 里留下 OpenCLi 标签页群组（见 SKILL.md「浏览器残留」）。
    "opencli_keep_tab": False,
    "opencli_site_session": "ephemeral",
    # 窗口模式：background（默认，不抢焦点）| foreground（调试用，会跳到前台）
    # adapter 命令本就默认 background；显式写是为了连收尾动作一起约束住。
    "opencli_window": "background",
    # 预导航被拒时的重试是否允许改用前台窗口（默认否：不抢用户焦点）。
    # 后台重试通常也能成功；只有确实反复失败、且用户不在意跳前台时才开。
    "prenav_retry_foreground": False,
    # 收尾：用完关掉 opencli 打开的标签页 / OpenCLi Browser 群组（默认开）。
    # cleanup_sessions 里是 interactive 会话名；doctor 用的是 __doctor__。
    "cleanup_browser": True,
    "cleanup_sessions": ["__doctor__"],
    # 全程后台（用户 2026-10-20 定，默认开）：整条流水线任何环节都不允许把窗口切到前台。
    # 生效范围：① 预导航重试强制后台（覆盖 prenav_retry_foreground）；
    # ② 收尾只做后台关会话 + 只读报告群组，残留群组只提示、不再尝试删除。
    # 设为 False 只影响预导航重试（允许它按 prenav_retry_foreground 换前台窗口）。
    "background_only": True,
    "lock_timeout": 900,       # 跨进程互斥锁最长等待秒数
    "overwrite": False,        # 已存在同名 .txt 时是否重新转录
    "keep_media": False,       # 是否保留下载的媒体文件
    "manifest": True,          # 是否写 output_dir/manifest.jsonl
    "skip_asr": False,         # 只下载不转录（调试用）
    "stdout": False,           # 额外把转录文本打到 stdout
    "stdout_only": False,      # 只打 stdout，不写文件
    # 空转录（视频没有口播）默认不算失败，但要显式计数并在汇总里列出来。
    # 定时任务/CI 想要「有任何一条空转录就判失败」时设为 true（或 --fail-on-empty）。
    "fail_on_empty": False,
    # 批量熔断：连续 N 条失败就中止剩余任务（0 = 不熔断）。
    # 主要防小红书风控：被风控后每条都会失败，不熔断会白白耗掉 N 条 × 抖动的时间。
    "max_consecutive_failures": 3,
    # opencli 列表/状态类调用的重试次数（拉投稿列表、doctor 都偶发瞬断）
    "opencli_retries": 3,
    "opencli_retry_base_delay": 2.0,

    # ---- ASR ----
    "asr": {
        "backend": "auto",            # auto | api | local
        "api_base": "",               # 如 http://127.0.0.1:8000/v1
        "api_key": "",
        # 默认留空：模型名取决于具体部署，猜一个具体名字会让别人拿到
        # 与「模型不存在」无关的报错。走 API 时必须显式配置（--asr-model / ASR_MODEL）。
        "model": "",
        "language": "zh",
        "timeout": 1800,
        "max_retries": 3,
        "retry_base_delay": 1.0,
        "compress_mb": 80,            # 超过该大小先压成 64k 单声道 m4a
        "local_model": "",            # 空 → 按本地后端取默认
        "default_mlx_model": "mlx-community/whisper-large-v3-turbo",
        "default_faster_model": "large-v3",
        "local_fallback_model": "",   # 检测到重复幻觉时改用的模型，空则同 local_model
    },

    # ---- B站 ----
    "bilibili": {
        "quality": "audio",
        "fallback_quality": "480p",   # 首选质量拿不到音频轨时改用它
        "page_size": 50,              # user-videos 每页数量（接口上限 50）
        "max_pages": None,
        "subtitle_lang": "zh-CN",
        "workers": 4,
    },

    # ---- 小红书 ----
    "xiaohongshu": {
        "user_limit": 200,
        "retries": 3,
    },

    # ---- 访问间隔抖动（仅小红书 opencli 调用前生效）----
    "jitter": {
        "enabled": True,
        "min": 60,
        "max": 180,
        "guarantee_interval": 10,
        "guarantee_min": 110,
    },
}

# 环境变量覆盖：配置路径 → 环境变量名（按顺序取第一个非空的）
ENV_MAP: list[tuple[tuple[str, ...], tuple[str, ...]]] = [
    (("output_dir",), ("VT_OUTPUT_DIR",)),
    (("opencli_cmd",), ("OPENCLI_CMD",)),
    # 只认 ASR_* 前缀。曾经在这里回退读 LLM_API_BASE / LLM_API_KEY，
    # 但那两个变量是给聊天模型用的；机器上若设了它们，ASR 请求会被静默打到
    # 一个根本不做语音转写的端点，报出来的错跟语音无关，极难排查。
    # 用户 2026-10-20 明确要求：不允许回退读 LLM_* 变量。
    (("asr", "api_base"), ("ASR_API_BASE",)),
    (("asr", "api_key"), ("ASR_API_KEY",)),
    (("asr", "model"), ("ASR_MODEL",)),
    (("asr", "language"), ("ASR_LANGUAGE",)),
    (("asr", "backend"), ("ASR_BACKEND",)),
]


def deep_merge(base: dict, override: dict) -> dict:
    """递归合并 override 到 base 的副本（override 优先）。"""
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _set_path(cfg: dict, path: tuple[str, ...], value) -> None:
    node = cfg
    for key in path[:-1]:
        node = node.setdefault(key, {})
    node[path[-1]] = value


def apply_env(cfg: dict) -> dict:
    """用环境变量覆盖配置（环境变量优先级高于配置文件）。"""
    out = deep_merge({}, cfg)
    for path, env_names in ENV_MAP:
        for name in env_names:
            value = os.environ.get(name)
            if value:
                _set_path(out, path, value)
                break
    return out


def load_config(path: str | None = None) -> dict:
    """默认值 → config 文件 → 环境变量。CLI 参数由调用方在其后再覆盖。"""
    cfg = deep_merge({}, DEFAULTS)
    if path:
        file_path = Path(path).expanduser()
        if not file_path.is_file():
            raise FileNotFoundError(f"配置文件不存在: {file_path}")
        raw = json.loads(file_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(f"配置文件根节点必须是对象: {file_path}")
        cfg = deep_merge(cfg, raw)
    return apply_env(cfg)


# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------

def setup_logging(verbose: bool = False) -> None:
    """配置控制台日志。中文输出，UTF-8 安全。"""
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                                           datefmt="%H:%M:%S"))
    root = logging.getLogger("video-transcribe")
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.propagate = False


# ---------------------------------------------------------------------------
# opencli 调用
# ---------------------------------------------------------------------------

# 构造好的启动前缀，例如 ["C:\\...\\opencli.CMD"] 或 ["cmd.exe", "/c", "opencli"]
_launcher: list[str] | None = None
_lock_timeout: int = DEFAULTS["lock_timeout"]
_keep_tab: bool = DEFAULTS["opencli_keep_tab"]
_site_session: str = DEFAULTS["opencli_site_session"]
_window_mode: str = DEFAULTS["opencli_window"]
_prenav_foreground: bool = DEFAULTS["prenav_retry_foreground"]
_background_only: bool = DEFAULTS["background_only"]


def resolve_opencli(cmd: str = "opencli") -> list[str]:
    """把 opencli 命令解析成可直接交给 subprocess 的 argv 前缀。

    背景：Windows 上 npm 装的是 opencli.cmd / opencli.ps1，
    subprocess.run(["opencli", ...]) 会抛 FileNotFoundError（CreateProcess
    不做 PATHEXT 解析）。shutil.which() 能解析出 .CMD 的绝对路径，
    实测该绝对路径可被 CreateProcess 直接执行。
    """
    if os.sep in cmd or (os.altsep and os.altsep in cmd) or Path(cmd).suffix:
        return [cmd]

    found = shutil.which(cmd)
    if found:
        return [found]

    if os.name == "nt":
        # 兜底：手工在 PATH 里找常见的 shim 扩展名
        exts = (".cmd", ".bat", ".exe")
        for directory in os.environ.get("PATH", "").split(os.pathsep):
            if not directory:
                continue
            for ext in exts:
                candidate = os.path.join(directory, cmd + ext)
                if os.path.isfile(candidate):
                    return [candidate]
            # 只有 .ps1 时用 cmd.exe /c 兜底
            if os.path.isfile(os.path.join(directory, cmd + ".ps1")):
                return [os.environ.get("COMSPEC", "cmd.exe"), "/c", cmd]

    return [cmd]


def set_opencli(cmd: str) -> None:
    """设置全局 opencli 启动前缀（由 configure 调用）。"""
    global _launcher
    _launcher = resolve_opencli(cmd)
    LOG.debug("opencli 启动前缀: %s", _launcher)


def opencli_launcher() -> list[str]:
    if _launcher is None:
        set_opencli("opencli")
    return list(_launcher)  # type: ignore[arg-type]


def set_lock_timeout(seconds: int) -> None:
    global _lock_timeout
    if not isinstance(seconds, int) or seconds <= 0:
        raise ValueError(f"lock_timeout 必须是正整数，实际: {seconds}")
    _lock_timeout = seconds


def configure(cfg: dict) -> None:
    """按配置初始化 opencli 前缀、锁超时与浏览器收尾开关。"""
    global _keep_tab, _site_session, _window_mode, _prenav_foreground, _background_only
    set_opencli(cfg["opencli_cmd"])
    set_lock_timeout(int(cfg["lock_timeout"]))
    _keep_tab = bool(cfg.get("opencli_keep_tab", False))
    _site_session = str(cfg.get("opencli_site_session") or "")
    _window_mode = str(cfg.get("opencli_window") or "background")
    _prenav_foreground = bool(cfg.get("prenav_retry_foreground", False))
    _background_only = bool(cfg.get("background_only", True))


def prenav_retry_args(args: list) -> list:
    """预导航被拒时的重试参数。

    默认**原样后台重试**（不抢焦点）。只有 `prenav_retry_foreground=true`
    **且 `background_only=false`** 时才改成 `--window foreground`——那是 opencli
    官方建议的绕法，代价是把窗口弹到用户面前；background_only 开着时它被强制忽略。
    """
    if _prenav_foreground and not _background_only:
        return [*args, "--window", "foreground"]
    return list(args)


def browser_args() -> list[str]:
    """浏览器类 opencli 命令的公共收尾参数。

    每次 opencli 访问浏览器都会在 Chrome 里开标签页；不加这些开关时，
    命令结束后标签页会被保留（用户会看到累积的「OpenCLi」标签页群组）。

    - `--keep-tab false`：命令结束后不保留标签页租约
    - `--site-session ephemeral`：站点会话用完即弃，不复用常驻标签页
    - `--window background`：**不抢前台焦点**（interactive 原语默认前台，
      用户在做事时会把窗口弹到最前面，这是之前被投诉的点）

    适配器命令的会话名是随机生成的，事后无法通过 `browser <session> tab close`
    定位，所以只能靠执行时就收尾（实测 `tab list` 对 doctor/bilibili/xiaohongshu
    等会话名均返回空）。
    """
    args = ["--keep-tab", "true" if _keep_tab else "false"]
    if _site_session:
        args += ["--site-session", _site_session]
    if _window_mode:
        args += ["--window", _window_mode]
    return args


# opencli 适配器会话名：persistent 模式固定为 site:<site>，可事后定位；
# ephemeral 模式是 site:<site>:<uuid>，随机生成、事后不可枚举。
_ADAPTER_SITES = ("bilibili", "xiaohongshu")

# interactive 会话（`opencli browser <session> ...` 原语）会在 Chrome 里建
# 「OpenCLi Browser」标签页群组。skill 自身不直接用这些原语，但 `opencli doctor`
# 用了 `__doctor__` 会话，所以默认也把它列进收尾名单。
_DEFAULT_CLEANUP_SESSIONS = ("__doctor__",)


def teardown_browser(platforms: tuple[str, ...] | None = None,
                     sessions: tuple[str, ...] | None = None) -> dict:
    """收尾第一步（后台）：关掉 opencli 留下的浏览器会话，尽量不留标签页与标签页群组。

    规则（用户 2026-10-10 定；2026-10-20 改为**全程后台**，不再有任何前台步骤）：
    用完必须关掉 opencli 打开的标签页与名为 `OpenCLi Browser` 的群组。
    本函数只做**能后台完成**的那部分，且只在该动手时才调 opencli，避免收尾本身又开新会话。

    - 适配器会话：只有配置成 `persistent` / 保留标签页时才会残留固定名 `site:<site>`；
      默认（ephemeral）opencli 自己在成功/异常/超时三条路径都关窗，无需插手。
    - interactive 会话：`close` 会走扩展的 `releaseLease()`，对 owned 容器标签页执行
      `chrome.tabs.remove()`；`unbind` 再兜一层 `close-window`。
    - 显式带 `--window background`：interactive 原语**默认是前台窗口**，会抢走用户焦点；
      这一步属于「后台优先」里的后台。
    - 群组删除做不到后台，本 skill 已不再提供该能力（见 report_opencli_groups）。
    """
    done: dict[str, list[str]] = {"closed": [], "unbound": []}
    want_adapter = _keep_tab or _site_session == "persistent"

    names: list[str] = []
    if want_adapter:
        names += [f"site:{site}" for site in (platforms or _ADAPTER_SITES)]
    names += [s for s in (sessions if sessions is not None else _DEFAULT_CLEANUP_SESSIONS) if s]

    window = ["--window", _window_mode or "background"]
    for name in names:
        for sub, key in (("close", "closed"), ("unbind", "unbound")):
            try:
                # opencli ≥2.0：`browser` 的会话名改为显式选项 `--session`，
                # 位置参数形式 `browser <session> <cmd>` 已移除。
                proc = run_opencli(["browser", sub, "--session", name, *window],
                                   timeout=60, use_lock=False)
                blob = (proc.stdout or "") + (proc.stderr or "")
                if '"ok":true' in blob.replace(" ", "") or "lease released" in blob:
                    done[key].append(name)
            except Exception as e:  # noqa: BLE001 —— 收尾失败不应影响主流程
                LOG.debug("收尾 %s %s 未成功（可能本就不存在）: %s", name, sub, e)
        if name in done["closed"] or name in done["unbound"]:
            LOG.info("  已收尾 opencli 浏览器会话: %s", name)
    return done


def _powershell_exe() -> str:
    """选一个能跑 UIA 查询的 PowerShell。

    **必须优先 Windows PowerShell 5.1（powershell.exe），不能用 pwsh 7**：
    实测（2026-10-21）同一段 `FindAll(Descendants, Button)` 遍历，
    5.1 用 1.7 秒返回，pwsh 7 直接卡死（45s / 120s 都不返回）。
    用 pwsh 7 时每次运行都要白等 `timeout=120` 才放弃，群组检查永远拿不到结果。

    powershell.exe 是 Windows 自带的，不需要额外安装；找不到时才退回 pwsh。
    """
    for name in ("powershell", "pwsh"):
        found = shutil.which(name)
        if found:
            return found
    return "powershell"


def _ensure_utf8_bom(script: Path) -> Path:
    """确保 ps1 带 UTF-8 BOM，返回实际要跑的脚本路径。

    为什么需要：Windows PowerShell 5.1 读**无 BOM** 的 UTF-8 文件会按 ANSI（本机是 cp936）
    解码，中文注释被拆坏后整份脚本解析失败，报 `Unexpected token '}'`。
    而 BOM 是文件字节层面的东西，git 检出、编辑器保存、任何一次 `edit` 都可能把它丢掉
    —— 2026-10-21 实测改一次注释就丢一次。所以这里不依赖「文件恰好带 BOM」，
    而是运行前自己检查、缺了就补一份带 BOM 的临时副本，把脚本本身当只读输入。

    已经是 BOM 或无法读取时原样返回（读失败交给下游 subprocess 报错）。
    """
    try:
        raw = script.read_bytes()
    except OSError:
        return script
    if raw.startswith(b"\xef\xbb\xbf"):
        return script
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return script

    fixed = Path(tempfile.gettempdir()) / f"video-transcribe-{script.stem}.ps1"
    try:
        fixed.write_bytes(b"\xef\xbb\xbf" + text.encode("utf-8"))
    except OSError as e:  # noqa: BLE001 —— 补不了就退回原文件
        LOG.debug("无法写入带 BOM 的临时脚本，改用原文件: %s", e)
        return script
    LOG.debug("已生成带 BOM 的临时脚本（原文件缺 BOM，5.1 会解析失败）: %s", fixed)
    return fixed


def report_opencli_groups() -> dict | None:
    """只读检查 Chrome 里是否还有 `OpenCLi Browser` 群组（不抢焦点、不改动任何东西）。

    为什么只读：检查本身不需要任何输入合成，只查一次 UIA 就能看清。
    本 skill 不再提供删除群组的能力（用户 2026-10-20 定：全程后台，不允许切前台），
    发现残留时只报告，由用户自己决定怎么处理。

    解释器选择见 `_powershell_exe()`：必须用 5.1，pwsh 7 会卡死。
    BOM 由 `_ensure_utf8_bom()` 兜底，不依赖仓库里的文件字节。
    """
    if sys.platform != "win32":
        return None
    script = Path(__file__).resolve().parent / "cleanup_opencli_groups.ps1"
    if not script.is_file():
        return None
    script = _ensure_utf8_bom(script)
    try:
        proc = subprocess.run(
            [_powershell_exe(), "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(script), "-Report"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
        )
    except Exception as e:  # noqa: BLE001
        LOG.debug("群组检查失败: %s", e)
        return None
    for line in (proc.stdout or "").splitlines():
        if line.startswith("VT_GROUPS_JSON:"):
            try:
                return json.loads(line[len("VT_GROUPS_JSON:"):])
            except json.JSONDecodeError:
                return None
    return None


def _chrome_window_titles() -> dict[int, str] | None:
    """枚举 Chrome 顶层窗口 → {hwnd: title}。非 Windows 返回 None。

    用 ctypes 直接调 EnumWindows：不依赖 PowerShell / UIA，几十毫秒级，可随时调用。
    （PowerShell 侧曾经枚举不到窗口，是 UIPI/会话隔离导致，Python 进程这边没有该问题。）
    """
    if sys.platform != "win32":
        return None
    import ctypes
    import ctypes.wintypes as wt

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    proc_type = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    user32.EnumWindows.argtypes = [proc_type, wt.LPARAM]
    user32.GetClassNameW.argtypes = [wt.HWND, ctypes.c_wchar_p, ctypes.c_int]
    user32.GetWindowTextW.argtypes = [wt.HWND, ctypes.c_wchar_p, ctypes.c_int]

    found: dict[int, str] = {}

    def callback(hwnd, _lparam):
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        if cls.value == "Chrome_WidgetWin_1":
            title = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, title, 512)
            found[int(hwnd)] = title.value
        return True

    user32.EnumWindows(proc_type(callback), 0)
    return found


def snapshot_chrome_windows() -> set[int] | None:
    """运行开始前拍一张 Chrome 窗口快照（hwnd 集合），收尾时用来算差集。"""
    windows = _chrome_window_titles()
    return set(windows) if windows is not None else None


def _chrome_blank_candidate_windows(baseline: set[int]) -> list[int]:
    """挑出可能是 opencli 占位的 about:blank 窗口。

    判据（必须同时满足）：
      1. 不在 baseline 里；
      2. 标题以 about:blank 开头；
      3. **只有一个标签页** —— 用 Ctrl+Tab 探测成本太高，改用窗口标题的辅助判据：
         Chrome 的窗口标题在多标签时形如「A 和另外 N 个页面」，单标签时就是标签页标题本身。
         所以 `about:blank - Google Chrome`（无「和另外」）即单标签。
    宁可漏关也不误伤：多标签的空白窗口、标题不符的一律跳过。

    判据 3 依赖 Chrome 的界面语言：中文是「和另外」、英文是 `and `、日文是「另有」，
    其他语言的多标签窗口会落到「看起来像单标签」那一侧。这是刻意选的：
    关不掉一个占位窗口只是留个残留，误关用户正在用的窗口才是事故。
    """
    windows = _chrome_window_titles()
    if not windows:
        return []

    skip_markers = ("和另外", "and ", "另有")
    candidates: list[int] = []
    for hwnd, title in windows.items():
        if hwnd in baseline:
            continue
        normalized = title.strip()
        lowered = normalized.lower()
        if not lowered.startswith("about:blank"):
            continue
        if any(marker in normalized for marker in skip_markers):
            continue  # 多标签窗口，不是 opencli 的单标签占位
        candidates.append(hwnd)
    return candidates


def close_blank_container_windows(before: set[int] | None = None, *,
                                  close_all: bool = False) -> list[int]:
    """后台关掉 opencli 留下的 `about:blank` 占位窗口，返回被关掉的 hwnd。

    为什么会有这个窗口：扩展的 `releaseLease()` 在没有其他 owned 租约时，不会关掉容器窗口，
    而是 `chrome.tabs.update(tabId, {url: BLANK_PAGE})` 把它留成**可复用占位**
    （扩展日志 `Released owned tab lease ... as reusable placeholder`）。窗口不会自己消失，
    下次运行会复用它。它是**真实可见的窗口**，不是最小化的后台窗口，所以必须收掉。

    关闭方式：`PostMessage(WM_CLOSE)` —— 等价于点窗口的关闭按钮，走 Chrome 正常关窗路径，
    **不抢焦点**（实测前后 GetForegroundWindow 不变）。不用 UIA/SendInput，也就不需要前台。

    安全性靠三重判据，宁可漏关也不误伤用户自己的空白窗口：
      1. 标题以 `about:blank` 开头（Chrome 对空白页的标题是 `about:blank - Google Chrome`）；
      2. 该窗口只有 1 个标签页（opencli 的占位固定单标签；用户的多标签窗口一律跳过）；
      3. 默认只处理「`before` 快照里没有」的新窗口；`close_all=True` 才连存量一起处理
         （单跑 `cleanup_browser.py --close-blank` 用，此时没有运行前快照可比）。
    没传 `before` 且 `close_all=False` 时**什么都不做**——没有基线就无法区分用户自己的空白窗口。
    """
    if sys.platform != "win32":
        return []
    if not close_all and not before:
        return []

    import ctypes
    import ctypes.wintypes as wt

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
    user32.IsWindow.argtypes = [wt.HWND]
    WM_CLOSE = 0x0010

    candidates = _chrome_blank_candidate_windows(before or set())
    if not candidates:
        return []

    closed: list[int] = []
    for hwnd in candidates:
        try:
            user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        except Exception as e:  # noqa: BLE001 —— 收尾失败不影响主流程
            LOG.debug("关闭占位窗口 %s 失败: %s", hwnd, e)
            continue
        # 给 Chrome 一点时间真的关掉窗口；最多等 2s，关了就继续下一个
        for _ in range(20):
            time.sleep(0.1)
            if not user32.IsWindow(hwnd):
                closed.append(hwnd)
                break

    if closed:
        LOG.info("  已关闭 %d 个 opencli 遗留的 about:blank 占位窗口", len(closed))
    leftover = len(candidates) - len(closed)
    if leftover:
        LOG.warning("  还有 %d 个 about:blank 占位窗口没关掉（可能正被占用），下次运行会复用", leftover)
    return closed


def finalize_browser_cleanup(platforms: tuple[str, ...] | None = None,
                             sessions: tuple[str, ...] | None = None,
                             blank_windows_before: set[int] | None = None,
                             close_blank_all: bool = False) -> dict:
    """收尾总入口：**全程后台**——关会话 → 关占位窗口 → 只读查群组。

    用户 2026-10-20 定：整条流水线不允许把窗口切到前台，所以这里不再尝试删除群组
    （删群组只能走 Chrome 右键菜单，必须前台）。发现残留时只报告，交用户处置。

    `blank_windows_before` 是运行**开始前**的 Chrome 窗口快照；传了就顺带关掉本次运行
    新出现的 `about:blank` 占位窗口。`close_blank_all=True` 则连存量一起关
    （单跑 cleanup_browser.py 用）。

    返回 `{released, blank_closed, groups}`。
    """
    released = teardown_browser(platforms, sessions)
    blank_closed = close_blank_container_windows(
        blank_windows_before, close_all=close_blank_all)
    groups = report_opencli_groups()

    if groups and groups.get("count"):
        LOG.warning("后台收尾后 Chrome 里仍有 %d 个 OpenCLi Browser 群组（只报告，不切前台）: %s",
                    groups["count"], groups.get("names"))

    return {"released": released, "blank_closed": blank_closed, "groups": groups}


def run_opencli(args: list, *, timeout: float = 300, use_lock: bool = True,
                check: bool = False) -> subprocess.CompletedProcess:
    """执行一次 opencli 调用。

    必须显式指定 encoding="utf-8"：Node 写的是 UTF-8，
    而 Windows 中文环境下 text=True 会按 cp936 解码，中文标题直接变乱码。

    同时注入 `OPENCLI_WINDOW`：opencli 的 `daemon-client` 会读它作为全局窗口模式兜底
    （`params.windowMode ?? envWindowMode`），设成 background 可保证不抢用户焦点。
    """
    argv = opencli_launcher() + [str(a) for a in args]
    env = dict(os.environ)
    if _window_mode:
        env.setdefault("OPENCLI_WINDOW", _window_mode)
    ctx = OpencliLock() if use_lock else nullcontext()
    try:
        with ctx:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                env=env,
            )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(
            f"opencli 调用超时 ({timeout}s): {' '.join(argv[1:])}"
        ) from e

    if check and proc.returncode != 0:
        raise RuntimeError(
            f"opencli 调用失败 (exit {proc.returncode}): {' '.join(argv[1:])}\n"
            f"  stdout:\n{(proc.stdout or '').strip() or '(空)'}\n"
            f"  stderr:\n{(proc.stderr or '').strip() or '(空)'}"
        )
    return proc


def _first_json_doc(text: str):
    """取出输出里第一个合法 JSON 文档（原样，不拆信封）。

    opencli 下载时会把进度条也写到 stdout，JSON 结果在末尾；进度行形如
    `[1/1] BVxxx_标题.mp4 ...`，raw_decode 会失败并被跳过。

    必须是「第一个成功」而不是「最后一个成功」：对 `[{...}, {...}]` 这种
    输出，扫描到内部元素的 `{` 也能解析成功，取最后一个会把数组退化成
    单个元素（字幕会被判成非列表，元数据会只剩一个 field）。
    """
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch not in "[{":
            continue
        try:
            obj, _ = decoder.raw_decode(text[i:])
        except ValueError:
            continue
        if isinstance(obj, (list, dict)):
            return obj
    return None


def _extract_json(text: str):
    """取第一个 JSON 文档并拆掉 opencli ≥2.0 的结果信封。"""
    obj = _first_json_doc(text)
    return None if obj is None else unwrap_envelope(obj)


def is_failure_envelope(obj) -> bool:
    """opencli ≥2.0 的失败信封：`{"ok": false, "error": {...}}`。"""
    return isinstance(obj, dict) and obj.get("ok") is False


def unwrap_envelope(obj):
    """剥掉 opencli ≥2.0 的 `{ok, data}` / `{ok, error}` 结果信封。

    1.x 直接把结果放在顶层；2.0.0 起 CLI 默认输出
    `{"ok": true, "data": ...}`，失败时 `{"ok": false, "error": {...}}`。
    下游（fetch_meta / iter_user_notes / 字幕解析…）都按 1.x 的裸结构写，
    所以必须在这一层统一拆掉信封，否则 `data` 里的列表会被当成 dict 丢掉。

    只认「恰好带 ok 键的顶层对象」，避免误拆业务数据里本来就叫 data 的字段。
    **失败信封原样返回**，由 `opencli_json` 识别后抛出完整错误（用户 2026-10-20 要求：
    失败时直接返回 OpenCLi 的完整错误，不要退化成下游莫名其妙的格式错误）。
    """
    if not isinstance(obj, dict) or "ok" not in obj:
        return obj
    if obj.get("ok"):
        return obj.get("data")
    return obj


def format_opencli_error(args: list, error, stdout: str = "", stderr: str = "") -> str:
    """把 opencli 的失败信封拼成**完整**、可诊断的错误文本。

    用户 2026-10-20 明确要求：直接把 OpenCLi 的完整错误返回即可，
    因此这里不截断 error / stdout / stderr（此前 300 字的截断正是真因被吃掉的原因）。
    """
    if isinstance(error, str):
        detail = error
    else:
        try:
            detail = json.dumps(error, ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            detail = repr(error)
    return (
        f"opencli 返回失败: {' '.join(str(a) for a in args)}\n"
        f"  error:\n{detail or '(无 error 字段)'}\n"
        f"  stdout:\n{(stdout or '').strip() or '(空)'}\n"
        f"  stderr:\n{(stderr or '').strip() or '(空)'}"
    )


def _is_empty_result(obj) -> bool:
    """失败信封 / 文本里是否表示「没有结果」（如无字幕），这不算错误。"""
    blob = json.dumps(obj, ensure_ascii=False) if not isinstance(obj, str) else obj
    return "EMPTY_RESULT" in blob


def opencli_json(args: list, *, timeout: float = 300, use_lock: bool = True):
    """执行 opencli 并解析 JSON 结果。EMPTY_RESULT 返回 None。

    失败信封（`{ok: false, error: ...}`）**直接抛出 opencli 的完整错误**
    （用户 2026-10-20 要求）：不截断、不退化、不让下游报出无关的格式错误。
    解析不到 JSON 且不是 EMPTY_RESULT 时同样抛 RuntimeError。
    """
    proc = run_opencli(args, timeout=timeout, use_lock=use_lock)
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    raw = _first_json_doc(out)
    if raw is not None:
        if is_failure_envelope(raw):
            # 无字幕之类的「空结果」不是错误，交给调用方按「没有」处理
            if _is_empty_result(raw):
                return None
            raise RuntimeError(
                format_opencli_error(args, raw.get("error"), out, err))
        data = unwrap_envelope(raw)
        if data is not None:
            return data

    blob = out + "\n" + err
    if "EMPTY_RESULT" in blob:
        return None
    code_match = re.search(r"code:\s*(\w+)", blob)
    if code_match and code_match.group(1) == "EMPTY_RESULT":
        return None
    raise RuntimeError(
        f"opencli 输出无法解析为 JSON: {' '.join(str(a) for a in args)}\n"
        f"  stdout:\n{out or '(空)'}\n  stderr:\n{err or '(空)'}"
    )


def opencli_json_with_prenav_retry(args: list, *, timeout: float = 300,
                                   use_lock: bool = True, window: str | None = None):
    """首次因 pre-navigation 失败时重试一次。

    opencli 在 ephemeral 站点会话下每次都会先导航到域名根
    （`shouldRunPreNav` 对 ephemeral 无条件返回 true），该次导航偶发被拒，
    报 `Pre-navigation to <url> failed: Navigation rejected.`，而后续真正取数的
    那次并未执行。

    默认**原样后台重试**（不抢焦点）；只有 `prenav_retry_foreground=true` 时才换成
    `--window foreground`（opencli 官方建议的绕法，代价是把窗口弹到用户面前）。
    """
    attempt_args = args
    try:
        return opencli_json(attempt_args, timeout=timeout, use_lock=use_lock)
    except RuntimeError as e:
        message = str(e)
        if "Pre-navigation" not in message and "Navigation rejected" not in message:
            raise
        LOG.warning("opencli 预导航被拒，重试一次（%s）",
                    "前台窗口" if _prenav_foreground else "仍在后台")

    return opencli_json(prenav_retry_args(args), timeout=timeout, use_lock=use_lock)


def _looks_like_prenav_failure(blob: str) -> bool:
    return "Pre-navigation" in blob or "Navigation rejected" in blob


def run_opencli_with_prenav_retry(args: list, *, timeout: float = 300,
                                  use_lock: bool = True, window: str | None = None,
                                  check: bool = False) -> subprocess.CompletedProcess:
    """run_opencli 版本：命令本身只回非零退出码、把错误写在 stderr 时也能重试。

    下载类命令（如 `bilibili download`）失败时不抛异常，只返回非零退出码，
    因此要自己看 stdout/stderr 判断是不是预导航被拒。
    """
    proc = run_opencli(args, timeout=timeout, use_lock=use_lock)
    blob = (proc.stdout or "") + "\n" + (proc.stderr or "")
    if proc.returncode != 0 and _looks_like_prenav_failure(blob):
        LOG.warning("opencli 预导航被拒，重试一次（%s）",
                    "前台窗口" if _prenav_foreground else "仍在后台")
        proc = run_opencli(prenav_retry_args(args), timeout=timeout, use_lock=use_lock)
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"opencli 调用失败 (exit {proc.returncode}): {' '.join(str(a) for a in args)}\n"
            f"  stdout:\n{(proc.stdout or '').strip() or '(空)'}\n"
            f"  stderr:\n{(proc.stderr or '').strip() or '(空)'}"
        )
    return proc


def check_opencli(cfg: dict | None = None) -> None:
    """运行 opencli doctor，daemon / 扩展 / 连通性 三项必须全过（failfast）。

    opencli ≥2.0 的 doctor 输出 `{"ok":true,"data":{...}}`，状态变成布尔字段
    （`daemonRunning` / `extensionConnected` / `connectivity.ok`），不再有
    1.x 那种 `[OK] Daemon:` 文本行；因此两种形式都要认，否则升级后会一律误判
    「状态异常」。

    **会重试**（2026-10-20 新增）：`opencli doctor` 会瞬时报 `Extension not connected`
    （Chrome 明明开着、几秒后再跑就全 OK），不重试会让整批任务在开头就退出。
    重试次数与间隔取 `cfg["opencli_retries"]` / `opencli_retry_base_delay`。
    """
    retries = int((cfg or {}).get("opencli_retries", DEFAULTS["opencli_retries"]) or 0)
    base_delay = float((cfg or {}).get("opencli_retry_base_delay",
                                       DEFAULTS["opencli_retry_base_delay"]) or 1.0)
    last_error: Exception | None = None

    for attempt in range(retries + 1):
        try:
            _check_opencli_once()
            return
        except RuntimeError as e:
            last_error = e
            if attempt >= retries:
                break
            delay = base_delay * (2 ** attempt)
            LOG.warning("opencli doctor 未通过（第 %d/%d 次），%.1fs 后重试: %s",
                        attempt + 1, retries + 1, delay, str(e).splitlines()[0])
            time.sleep(delay)

    raise last_error or RuntimeError("opencli 状态异常")


def _check_opencli_once() -> None:
    """跑一次 doctor 并判定，不重试。"""
    proc = run_opencli(["doctor"], timeout=120)
    stdout = proc.stdout or ""
    if proc.returncode != 0:
        raise RuntimeError(
            f"opencli doctor 执行失败 (exit {proc.returncode})\n"
            f"  stderr:\n{(proc.stderr or '').strip() or '(空)'}"
        )

    # 2.0+：结构化 JSON 判定。注意 _extract_json() 已经把 {ok,data} 信封拆开了，
    # 这里要判断的是「拆开后的对象本身是否就是 status」，而不是再找 ok 键。
    payload = _extract_json(stdout)
    if isinstance(payload, dict) and "daemonRunning" in payload:
        conn = payload.get("connectivity")
        checks = {
            "Daemon": bool(payload.get("daemonRunning")),
            "Extension": bool(payload.get("extensionConnected")),
            "Connectivity": bool(conn.get("ok")) if isinstance(conn, dict) else bool(conn),
        }
        failed = [k for k, v in checks.items() if not v]
        if not failed:
            return
        raise RuntimeError(
            "opencli 状态异常，以下检查未通过: " + ", ".join(failed) + "\n"
            "  请确认 Chrome/Chromium 已打开、opencli Browser Bridge 扩展已连接、且目标站点已登录。\n"
            f"  doctor 输出:\n{stdout}"
        )

    # 1.x：文本行判定
    failed = [k for k in ("Daemon", "Extension", "Connectivity") if f"[OK] {k}" not in stdout]
    if failed:
        raise RuntimeError(
            "opencli 状态异常，以下检查未通过: " + ", ".join(failed) + "\n"
            "  请确认 Chrome/Chromium 已打开、opencli Browser Bridge 扩展已连接、且目标站点已登录。\n"
            f"  doctor 输出:\n{stdout}"
        )


# ---------------------------------------------------------------------------
# 跨进程互斥锁
# ---------------------------------------------------------------------------

_LOCK_FILE = os.path.join(tempfile.gettempdir(), "opencli_mutex.lock")
_LOCK_BUSY_ERRNOS = {errno.EACCES, errno.EDEADLK, errno.EAGAIN}


def _try_lock(fd) -> bool:
    if sys.platform == "win32":
        os.lseek(fd.fileno(), 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError as e:
            if e.errno in _LOCK_BUSY_ERRNOS:
                return False
            raise
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def _unlock(fd) -> None:
    if sys.platform == "win32":
        os.lseek(fd.fileno(), 0, os.SEEK_SET)
        msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)


class OpencliLock:
    """跨进程互斥锁（POSIX flock / Windows msvcrt.locking），阻塞等待。"""

    def __init__(self, timeout: float | None = None):
        self._timeout = float(timeout if timeout is not None else _lock_timeout)
        self._fd = None

    def acquire(self) -> None:
        self._fd = open(_LOCK_FILE, "w")
        deadline = time.monotonic() + self._timeout
        try:
            while True:
                if _try_lock(self._fd):
                    return
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"opencli 锁获取超时 ({self._timeout:.0f}s)，持锁进程可能卡死。"
                        f"lock_file={_LOCK_FILE}"
                    )
                time.sleep(0.1)
        except BaseException:
            self._fd.close()
            self._fd = None
            raise

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            _unlock(self._fd)
        finally:
            self._fd.close()
            self._fd = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()


# ---------------------------------------------------------------------------
# 访问间隔抖动
# ---------------------------------------------------------------------------

class Jitter:
    """加权随机抖动延迟器。

    语义（2026-10-20 用户定义）：**按「两次请求之间的全流程耗时」判断要不要等**。

    - 全流程耗时 ≥ 本次抽到的 target  →  不等，直接发下一次请求
    - 全流程耗时 <  本次抽到的 target  →  等 target − 全流程耗时

    这里的「全流程耗时」是上一次请求发出到这一次请求发出之间的真实间隔，即
    下载请求本身 + 抽音频 + ASR 上传 + 落盘，**不含上一次的等待**。所以：
    处理比预设慢就不额外等，处理比预设快才按差值补足。

    实现上靠 `_last` 记录**请求真正发出的时刻**（sleep 之后），不是「开始等待」的时刻
    ——记在 sleep 之前会把上一轮的等待也塞进 elapsed，等于等待自己抵掉自己，
    处理一慢就永远不生效。

    target 的抽取：低延迟段权重更高（线性衰减），且每 guarantee_interval 次至少一次
    落在 [guarantee_min, max] 区间。
    非线程安全：每个线程/流程应各自持有独立实例。
    """

    def __init__(self, min_delay: float = 60, max_delay: float = 180,
                 guarantee_interval: int = 10, guarantee_min: float = 110):
        if min_delay >= max_delay:
            raise ValueError(f"jitter.min ({min_delay}) 必须小于 jitter.max ({max_delay})")
        if not (min_delay <= guarantee_min <= max_delay):
            raise ValueError(
                f"jitter.guarantee_min ({guarantee_min}) 必须落在 [{min_delay}, {max_delay}] 内"
            )
        self._min = min_delay
        self._max = max_delay
        self._guarantee_interval = guarantee_interval
        self._guarantee_min = guarantee_min
        self._count = 0
        self._last: float | None = None

        segments = 10
        step = (max_delay - min_delay) / segments
        self._bounds = [(min_delay + i * step, min_delay + (i + 1) * step) for i in range(segments)]
        self._weights = [segments - i for i in range(segments)]

    def wait(self) -> float:
        now = time.time()
        self._count += 1
        if self._count % self._guarantee_interval == 0:
            target = random.uniform(self._guarantee_min, self._max)
        else:
            low, high = random.choices(self._bounds, weights=self._weights, k=1)[0]
            target = random.uniform(low, high)

        if self._last is not None:
            # elapsed = 上一次请求发出 → 现在，即上一轮下载+抽音频+ASR 的全流程耗时
            elapsed = now - self._last
            if elapsed >= target:
                self._last = now
                return 0.0
            delay = target - elapsed
        else:
            delay = 0.0

        if delay > 0:
            LOG.debug("抖动等待 %.1fs（全流程耗时 %.1fs，目标 %.1fs）", delay, now - self._last, target)
            time.sleep(delay)
        # 记「请求真正发出的时刻」：放在 sleep 之后，让下一次的 elapsed 只反映处理耗时。
        self._last = time.time()
        return delay


def make_jitter(cfg: dict) -> "Jitter | None":
    j = cfg["jitter"]
    if not j.get("enabled"):
        return None
    return Jitter(
        min_delay=float(j["min"]),
        max_delay=float(j["max"]),
        guarantee_interval=int(j.get("guarantee_interval", 10)),
        guarantee_min=float(j.get("guarantee_min", 110)),
    )


# ---------------------------------------------------------------------------
# 文件名 / 平台识别
# ---------------------------------------------------------------------------

_ILLEGAL = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def safe_filename(title: str, max_bytes: int = 200) -> str:
    """去掉文件系统非法字符并按 UTF-8 字节数截断（留出 id 后缀与扩展名余量）。"""
    cleaned = _ILLEGAL.sub("", str(title or "")).strip().strip(".")
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        return "untitled"
    if len(cleaned.encode("utf-8")) <= max_bytes:
        return cleaned
    result = ""
    for ch in cleaned:
        if len((result + ch).encode("utf-8")) > max_bytes:
            break
        result += ch
    return result.rstrip() or "untitled"


def detect_platform(value: str) -> str | None:
    """从 URL 或裸 ID 判断平台。"""
    v = (value or "").strip()
    low = v.lower()
    if "bilibili.com" in low or "b23.tv" in low or "acg.tv" in low:
        return "bilibili"
    if re.fullmatch(r"bv[0-9a-z]{8,}", low) or re.fullmatch(r"av\d+", low):
        return "bilibili"
    if "xiaohongshu.com" in low or "xhslink.com" in low or "xhs.cn" in low:
        return "xiaohongshu"
    return None


def detect_user_platform(value: str) -> str | None:
    """--user 场景下的平台判断（比 URL 判断更保守，模糊时返回 None）。"""
    v = (value or "").strip()
    low = v.lower()
    if "space.bilibili.com" in low:
        return "bilibili"
    if "xiaohongshu.com" in low or "xhslink.com" in low:
        return "xiaohongshu"
    # 纯数字一定是 B站 mid；小红书 user_id 是 24 位十六进制，含字母
    if re.fullmatch(r"\d+", v):
        return "bilibili"
    if re.fullmatch(r"[0-9a-fA-F]{16,32}", v):
        return "xiaohongshu"
    return None


def temp_dir_for(cfg: dict) -> Path:
    """媒体临时目录。"""
    configured = cfg.get("temp_dir")
    base = Path(configured).expanduser() if configured else Path(tempfile.gettempdir()) / "video-transcribe"
    base.mkdir(parents=True, exist_ok=True)
    return base
