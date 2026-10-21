---
name: video-transcribe
description: 把小红书 / B站视频转成纯文本转录（下载 + ASR，不含任何 LLM 后处理）。Use when the user asks to 转录小红书视频、转录B站视频、视频转文字、下载视频并转文字、提取口播文案、把视频变成文稿、批量转录某个UP主/用户主页的视频，or needs transcript/plain-text extraction from xiaohongshu.com / xhslink.com / bilibili.com (BV号) links. Supports single video, user profile batch (B站 mid / 小红书 user_id), and B站 collections; B站 prefers official subtitles and only falls back to ASR when absent.
---

# video-transcribe · 视频转文本（下载 + ASR）

把视频变成**可阅读的纯文本**。输出是 ASR/字幕的**原文**，不做格式化、不做内容过滤、
不做摘要——需要结构化再交给别的工具。

来源是一条小红书 / B站 的「下载 → ASR」流水线，**去掉了其中的 LLM 格式化、
内容噪音过滤与关键词过滤三个环节**，只保留「拿到文本」本身。

## 0. 何时使用 / 不适用

适用：把一条或一批视频的口播内容变成 .txt；给某个 UP 主 / 小红书用户的视频建文稿库；
先把长视频转成文本，再自己读、自己总结。

不适用：需要 Markdown 结构化排版（另找格式化工具）；需要内容摘要或关键信息抽取；
纯图文笔记（小红书图文没有可转录内容，脚本会明确报错）。

## 1. 两条平台路径（差异是刻意的）

| | B站 | 小红书 |
|---|---|---|
| 文本来源 | **官方字幕优先**（人工或 AI 字幕），无字幕才下载音频走 ASR | 无平台字幕，始终 下载视频 → 抽音频 → ASR |
| 下载方式 | `opencli bilibili download` | `opencli xiaohongshu download` |
| 鉴权 | opencli Browser Bridge（复用浏览器登录态） | 同左 |
| 批量 | `--user <mid>` / `--season <合集链接>`，**并行**（默认 4 线程） | `--user <user_id>`，**串行**（小红书风控更紧） |
| 访问间隔 | 无抖动 | 每次 opencli 调用前随机抖动（默认 60–180s，每 10 次保底一次高延迟） |

小红书那条路径的抖动不是可选项：并发请求容易触发 `SECURITY_BLOCK`。
默认抖动较慢是**有意为之**，要提速就调 `jitter.min/max`，自担风控风险。

**抖动的判定口径**（`vt_common.Jitter.wait`）：按**两次请求之间的全流程耗时**决定要不要等。

| 全流程耗时 vs target | 行为 |
|---|---|
| ≥ target | 不等，直接发下一次请求 |
| < target | 等 `target − 全流程耗时` |

「全流程耗时」= 上一次请求发出到这一次请求发出之间的真实间隔，即下载请求 + 抽音频 +
ASR 上传 + 落盘，**不含上一次的等待**。实现上靠 `_last` 记录**请求真正发出的时刻**。
所以实际间隔恒等于 `max(全流程耗时, target)`：处理比预设慢就不额外等，处理比预设快才补足差值。

## 2. 前置依赖

- **opencli** 已安装，且 `opencli doctor` 的 Daemon / Extension / Connectivity 三项全过
  （脚本启动时会自己跑一次，不通过直接报错退出）。
- Chrome/Chromium 已打开，已装 opencli Browser Bridge 扩展，且**目标站点已登录**。
- **ffmpeg**（含 `ffprobe`）在 PATH 中——抽音频与音频轨校验都要用。
- **ASR 能力二选一**：
  - API：OpenAI 兼容的 `/audio/transcriptions`，需要 `api_base` + `api_key` + **`model`**
    （模型名没有通用默认值，取决于你的接口部署了哪个模型，必须显式配）；
  - 本地：`faster-whisper`（跨平台）或 `mlx-whisper`（仅 Apple Silicon）。

纯 Python 3 标准库实现，**不需要** `pip install`；唯一外部依赖是上面这些可执行程序
与可选的本地 whisper 包。

> **平台边界**：核心流程（下载 / 抽音频 / ASR / 落盘）跨平台；
> **浏览器收尾只在 Windows 生效**（关 `about:blank` 占位窗口、查标签页群组依赖 Win32 API
> 与 PowerShell），其他平台自动跳过，不会报错。
> 收尾用 Windows 自带的 **PowerShell 5.1**（`powershell.exe`），不需要装 PowerShell 7。

## 3. 快速上手

```bash
# B站单视频（优先字幕，无字幕自动走 ASR）
python scripts/vt.py "https://www.bilibili.com/video/BV1xx411c7mD"

# 小红书单视频
python scripts/vt.py "https://www.xiaohongshu.com/explore/<id>?xsec_token=..."

# B站用户主页批量（并行）
python scripts/vt.py --user <mid> --limit 5 --since 2026-01-01

# B站合集
python scripts/vt.py --season "https://space.bilibili.com/<mid>/lists/<season_id>"

# 直接要文本，不落盘
python scripts/vt.py "BV1xx411c7mD" --stdout-only
```

配置 ASR 接口（三选一，优先级从高到低）：

```bash
# 1) 命令行（model 走 API 时必填）
python scripts/vt.py "BV..." --api-base http://127.0.0.1:8000/v1 \
    --api-key KEY --asr-model <模型名>
# 2) 环境变量
ASR_API_BASE=http://... ASR_API_KEY=KEY ASR_MODEL=<模型名> python scripts/vt.py "BV..."
# 3) 配置文件
python scripts/vt.py "BV..." --config config.json
```

`config.example.json` 可复制成 `config.json`。**不要把带 api_key 的配置提交到版本库。**

> ⚠️ **只认 `ASR_*` 前缀**。早期版本在 `ASR_API_BASE` / `ASR_API_KEY` 缺失时会回退读
> `LLM_API_BASE` / `LLM_API_KEY`，2026-10-20 已**移除该回退**：那两个变量是给聊天模型用的，
> 机器上若设了它们，ASR 请求会被静默打到不做语音转写的端点，报出的错跟语音毫无关系。

> ⚠️ **`asr.model` 没有默认值**（2026-10-21 起）。以前默认填了一个具体模型名，
> 别人配了 `api_base` 却没指定模型时，会拿那个名字去请求一个大概率不存在的模型，
> 收到的报错跟「模型名不对」毫无关系。现在留空会直接报「未配置 ASR 模型名」并给出配置路径。

## 4. 输出

默认 `./transcripts/`，用 `-o` 改。每个视频一个 txt：

```
{output}/B站/{作者}/{合集}/{标题} [{bv号}].txt     # 合集可为空
{output}/小红书/{作者}/{标题} [{笔记id}].txt
```

- 正文就是转录文本，**没有 frontmatter**（本 skill 的产物是纯文本；元数据在 manifest 里）。
- `manifest.jsonl` 每行一条：`platform / id / url / title / author / date / source /
  chars / txt / ts`。`source` 为 `subtitle` 或 `asr`，可用来判断哪些视频是真转录、哪些是拿的字幕。
- 已存在同名 txt 时**跳过**（不重复下载/转录）；要重做加 `--overwrite`。
- `--stdout` 额外打印到 stdout；`--stdout-only` 只打印不落盘。stdout 只有文本，
  日志全走 stderr，方便直接管道。

### 退出码与批量失败明细（2026-10-20 起）

| 退出码 | 含义 |
|---|---|
| `0` | 全部成功（含「已存在跳过」、`--skip-asr` 的仅下载、以及默认的空转录） |
| `1` | 执行失败，**或批量里有条目失败**（含 `--fail-on-empty` 时的空转录） |
| `2` | 参数错误（时间参数冲突、平台无法识别、配置文件不存在等） |
| `130` | 用户中断（Ctrl+C） |

> ⚠️ **opencli 未就绪返回 1，不是 2**：`check_opencli()` 抛 `RuntimeError`，走的是「执行失败」那条分支。
> 只有 `ValueError` / `FileNotFoundError` 才是 2。定时任务若想区分「环境坏了」与「内容失败了」，
> 需要看日志首行，不能只看退出码。

批量模式（`--user` / `--season`）结束时会打一段汇总，并**逐条列出每个失败的视频**：
标题、id、链接、完整错误原因。全部成功时退出码才是 0；有任何一条失败就返回 1，
可以直接用于定时任务 / CI 判成败。

> 「跳过（已存在）」与 `--skip-asr` 的「仅下载」**都算成功**，不触发非零退出码——
> 它们是正常的幂等行为与用户主动选择，算失败会让重跑变成假报警。

### 空转录单独计数（2026-10-20 新增）

转录结果为空（视频只有 BGM / 纯画面）**默认不算失败**——那是内容本身的属性，不是流水线故障。
但也不再静默：汇总里单列一档「空转录 N」，并逐条列出标题 / id / 链接。
需要严格判成败时加 `--fail-on-empty`，空转录就计入失败并让退出码变 1。

### 连续失败熔断（2026-10-20 新增）

批量连续失败到阈值（默认 3，`--max-consecutive-failures` 可调，0 关闭）就**中止剩余任务**。
主要防小红书风控：一旦 `SECURITY_BLOCK`，后面每条都会失败，不熔断会白耗
「剩余条数 × 每条 60–180s 抖动」的时间。B站线程池同样生效（`--workers` 并发时按完成顺序计数）。

## 5. 关键参数

| 参数 | 说明 |
|---|---|
| `--user` / `--season` / 位置参数 url | 三选一；平台自动识别，`--platform` 可强制 |
| `--limit` | 最多处理几个视频（**取最新发布的 N 个**；B站按发布时间降序，小红书按笔记 ID 时间戳降序） |
| `--since` / `--until` / `--from-date`+`--to-date` | B站时间筛选（互斥，YYYY-MM-DD）；**`--user` 与 `--season` 都生效**；小红书不支持 |
| `--series` | 只处理指定合集（可多个） |
| `--workers` | B站并行线程数（默认 4） |
| `--overwrite` / `--no-overwrite` | 已存在同名 txt 时重做 / 跳过。**三态**：都不写就用配置文件里的值 |
| `--fail-on-empty` / `--no-fail-on-empty` | 空转录是否算失败（默认不算） |
| `--max-consecutive-failures N` | 连续失败 N 条就中止剩余任务（默认 3，0 关闭） |
| `--jitter-min` / `--jitter-max` / `--no-jitter` | 小红书访问抖动 |
| `--asr-backend` | `auto`(默认) / `api` / `local` / `mlx` / `faster-whisper` / `none` |
| `--keep-media` | 保留下载的音视频（默认用完即删） |
| `--skip-asr` | 只下载不转录，调试用 |
| `--keep-tab` / `--site-session` | 见下面「浏览器标签页与风控」 |
| `--no-cleanup` | 跳过收尾 |
| `--background-only` / `--allow-foreground` | 全程后台（默认开）/ 允许预导航重试换前台窗口 |

> ⚠️ **`--overwrite` 为什么要三态**：`build_config` 里的 `put()` 会吞掉 `False`，
> 所以命令行「没写」和「写了 false」以前无法区分，配置文件里的 `overwrite: true`
> 永远压不住——批量重跑会把已转录的全部重新下载 + 重新 ASR。现在 `--no-overwrite` 能明确压掉它。
> 跑批量前如果只想补增量，确认配置里是 `"overwrite": false` 或显式加 `--no-overwrite`。

## 6. 浏览器标签页与群组清理（硬性要求，必读）

**规则**：opencli 的每次浏览器访问都会在 Chrome 里开标签页。用完必须关掉**它打开的那些标签页**
以及对应的 **`OpenCLi Browser` 标签页群组**。
**全程后台**（用户 2026-10-20 定，取代 2026-10-11 的「清理优先后台，不行再转前台」）：
整条流水线任何环节都不允许把窗口切到前台，`background_only` 默认开。
**不要把这个清理推给用户**，也不要拿命令退出码当「清干净了」。

本 skill 的做法：

- **只用适配器命令**（`opencli bilibili …` / `opencli xiaohongshu …`）。扩展源码里写明
  「Adapter automation no longer creates or discovers a visible tab group」——适配器**不建**可见群组。
  **绝不要**用 `opencli browser <session> …` 这类 interactive 原语，那才是 `OpenCLi Browser` 群组的来源。
- 所有浏览器调用统一带 `--keep-tab false --site-session ephemeral --window background`
  （`resolveKeepTab()` 默认 false、`resolveSiteSession()` 默认 `ephemeral`；显式写是防环境变量覆盖）。
- 每次运行结束在 `finally` 里执行收尾（`vt_common.finalize_browser_cleanup`），三步：
  1. **后台**关掉配置的 interactive 会话名（默认 `__doctor__`——因为启动时的 `opencli doctor` 用的就是它）；
     若配置成 `persistent` / `--keep-tab`，额外关闭固定适配器会话 `site:<平台>`。
     用 `close`（扩展内部走 `releaseLease()` → 对 owned 标签页执行 `chrome.tabs.remove()`）
     再补一次 `unbind`（`close-window`）。
  2. **后台**关掉本次运行新出现的 `about:blank` 占位窗口（见下）。
  3. 只读查群组；若仍有 `OpenCLi Browser` 群组，**只报告，不删除**（见下）。
  用 `--no-cleanup` 跳过整个收尾。

### 收尾第二步：about:blank 占位窗口必须关掉

opencli 的容器窗口**不会自己消失**。扩展的 `releaseLease()` 在没有其他 owned 租约时不会关窗，
而是 `chrome.tabs.update(tabId, {url: BLANK_PAGE})` 把它留成**可复用占位**（扩展日志
`Released owned tab lease ... as reusable placeholder`），下次运行复用同一个窗口。
它是**真实可见的窗口**（`IsWindowVisible=True`、`IsIconic=False`，正常桌面尺寸），
不是最小化的后台窗口，留在桌面上就是污染。

关法：`PostMessage(WM_CLOSE)`——等价于点窗口的关闭按钮，走 Chrome 正常关窗路径，
**不抢焦点**（实测前后 `GetForegroundWindow()` 不变）。不用 UIA / SendInput，也就不需要前台。

判据（宁可漏关，也不误伤用户自己的空白窗口）：

| 判据 | 说明 |
|---|---|
| 快照差集 | 运行**开始前**拍一张 Chrome 窗口 hwnd 快照，只处理新出现的。用户原有的空白窗口天然被排除 |
| 标题前缀 | 以 `about:blank` 开头（Chrome 的空白页标题是 `about:blank - Google Chrome`） |
| 单标签 | 标题里不含「和另外」——Chrome 多标签窗口标题形如「A 和另外 N 个页面」 |

> 「和另外」依赖 Chrome 界面语言：中文是「和另外」、英文是 `and `、日文是「另有」。
> 其他语言的多标签窗口会被当成单标签。这是刻意的取舍——漏关一个占位窗口只是留残留，
> 误关用户正在用的窗口才是事故。

没传快照就什么都不做。单独跑 `cleanup_browser.py` 时没有基线，需要显式加 `--close-blank`
才按「标题 + 单标签」判据处理存量。

已实测（2026-10-20）：端到端跑完 `vt.py` 输出「已关闭 1 个 opencli 遗留的 about:blank 占位窗口」，
结束后 `blank_count=0`；同时开一个**用户自己的** `about:blank` 窗口再跑，那个窗口存活、没被误关。

### 全程后台：为什么删群组不再做了

关标签页能后台完成；**删群组做不到后台**，两条原因：

1. 扩展没有 group 删除 API（`background.js` 只用 `tabGroups.get/query/update`）；
2. Chrome 侧唯一入口是右键菜单「删除群组」，而打开该菜单**必须把窗口切到前台**。

用户 2026-10-20 要求全程后台，所以这条路径整个拆掉了（`delete_opencli_groups()` 与
`cleanup_opencli_groups.ps1 -Delete` 均已移除）。实测这条流水线**本来就不产生群组**：
适配器命令不建可见群组，`__doctor__` 会话由后台 `close` 收掉，端到端跑完 `count=0`。
万一真有残留，收尾只打一行提示，由用户自己右键删除或重启 Chrome。

> 实测补充：看着像「空群组」时，它里面往往**还留着一个 `about:blank` 标签页**
> （右键菜单的「分组内的标签页」会列出它）。所以要按「删除群组」处理，而不是等它自己变空。

### 全程后台的其他保证

- 所有 opencli 调用都带 `--window background`，并注入环境变量 **`OPENCLI_WINDOW=background`**
  （opencli 的 `daemon-client` 读它作全局窗口模式兜底）。**interactive 原语默认是前台窗口**，
  不写这个开关就会把 Chrome 弹到用户面前——实测踩过。
- 预导航失败的重试也**强制在后台**：`background_only` 开着时，即使配了
  `prenav_retry_foreground: true` / `--prenav-retry-foreground` 也不会换成前台窗口。
- 实测（2026-10-20）：单条 adapter 调用期间 opencli 新建的 Chrome 窗口 `IsWindowVisible=True`
  但 `IsIconic=True`、坐标 `(-32000,-32000)`、无 `APPWINDOW`，任务栏不新增按钮；
  端到端跑 `vt.py` 时 `GetForegroundWindow()` 全程未变。
  唯一例外是收尾要关的 `about:blank` 占位窗口——它本身是可见窗口，但关闭走 `WM_CLOSE`，同样不抢焦点。

收尾入口（可单独跑，也被 `vt.py` 收尾复用）：

```bash
python scripts/cleanup_browser.py                # 后台关会话 → 只读报告群组
python scripts/cleanup_browser.py --close-blank  # 顺带关掉存量的 about:blank 占位窗口
python scripts/cleanup_browser.py --json
```

### 怎么才算真的清干净了（我踩过的两个坑）

1. **`opencli browser <session> tab list` 判断不了残留**：它内部用 `isDebuggableUrl()` 过滤，
   opencli 自己开的容器标签页是 `about:blank`，**会被隐藏**，所以它常年返回 `[]`。
2. **窗口标题枚举也判断不了**：这些标签页位于**既有 Chrome 窗口**内，不是独立顶层窗口。
   我曾用 EnumWindows 得出「无残留」，结果用户实际看到两个标签页 + 一个群组——**该结论无效**。

**唯一可靠的判据是 Chrome 的标签栏/群组本身**：人眼，或本 skill 的
`cleanup_opencli_groups.ps1 -Report`（只读 UIA 查询，不抢焦点）。

> ⚠️ **这个脚本必须用 PowerShell 5.1 跑，不能用 pwsh 7**（2026-10-21 实测）：
> 同一段 `FindAll(Descendants, Button)` 遍历，5.1 约 1.7 秒返回，pwsh 7 直接卡死
> （45s / 120s 都不返回）。`vt_common._powershell_exe()` 会自动选 `powershell.exe`；
> 手工跑时也请用 `powershell -File ...` 而不是 `pwsh -File ...`。
> 另外脚本必须存成 **UTF-8 with BOM**，5.1 读无 BOM 的 UTF-8 会解析失败报
> `Unexpected token '}'`——代码侧有 `_ensure_utf8_bom()` 兜底，手工跑则依赖文件自带 BOM。

### 其他风控注意

批量转录会连续触发浏览器操作。B站按 `--workers` 并行、无间隔；小红书串行 + 随机抖动。
测试或调试时请用 `--limit 1`，不要反复全量跑；也**不要用 `browser` 原语反复探查状态**——
每个探针都可能留下一个标签页。

## 7. 已知陷阱（都已在代码里处理）

1. **`opencli --quality audio` 不保证只有音频**：实测仍会产出
   `<bvid>_<标题>.mp4`（hevc 视频 + aac 音频 + mjpeg 封面）。所以下载后一律用
   `ffprobe` 校验音频轨，缺失就删除重试，最后退到 `bilibili.fallback_quality`。
2. **opencli 偶发返回非零退出码但文件已下载成功**（实测 exit 75）。脚本以
   「文件是否存在 + 音频轨是否可用」为准，非零退出码只记 WARNING。
3. **ephemeral 会话下每次调用都会先导航到域名根**（opencli 源码里
   `shouldRunPreNav` 对 ephemeral 无条件返回 true），偶发报
   `Pre-navigation ... Navigation rejected.`。脚本会**原样在后台重试一次**
   （不想跳前台）；只有显式开 `prenav_retry_foreground` / `--prenav-retry-foreground`
   才会改用 `--window foreground` 重试。
4. **Windows 上 `subprocess` 拉不起 `opencli`**：npm 装出的是 `.cmd`/`.ps1` shim，
   CreateProcess 不做 PATHEXT 解析。脚本用 `shutil.which()` 解析出绝对路径再调用。
5. **必须显式 `encoding="utf-8"`**：Node 写 UTF-8，而 Windows 中文环境下
   `text=True` 会按 cp936 解码，中文标题直接变乱码。
6. **stdout 混有进度条**：下载时进度条也写到 stdout，JSON 在末尾。解析取
   **第一个**合法 JSON 文档（取最后一个会把 `[{...},{...}]` 退化成单个元素）。
7. **图文笔记**：opencli 的 xiaohongshu download 会下图片，脚本检测到只有图片时
   明确报错「这是一篇图文笔记」而不是当成空转录。
8. **多分P视频**：只处理默认 P1，不支持下 `--page` 选P；检测到多P会打 WARNING。

### 2026-10-20 修掉的四个「静默」问题

这四条以前都是**不报错但结果不对**，最难排查，现在都改成显式暴露：

1. **`--season` 配时间筛选曾经无效**：合集模式放行了 `--since/--until/--from-date/--to-date`，
   但函数内部从没调用过时间筛选，会静默处理整个合集。现在与 `--user` 模式一致生效。
2. **批量退出码恒为 0**：线程池把每条异常就地吞掉，全部失败也返回成功。
   现在会汇总并逐条列出失败视频，有任何失败就返回 1。
3. **`--limit` 取到最旧的 N 条**：排序是升序 + `[:limit]`，与「取最新」的直觉相反。
   现在改为降序。⚠️ 如果你之前依赖「取最早 N 条」的行为，需要相应调整。
4. **日期解析失败的视频会凭空消失**：`parse_pubdate` 返回 `None` 时被直接 `continue`，
   看起来像「不符合时间条件」。现在会逐条 WARNING（带标题 / id / 原始日期串）并汇总。

另外两处也一并改了：`ENV_MAP` **不再回退读 `LLM_API_BASE` / `LLM_API_KEY`**；
opencli 的失败信封（`{ok:false, error:{...}}`）现在**直接抛出完整错误**（不再截断 300 字），
不再退化成下游「期望列表、实际是字典」这类与真因无关的报错。
`EMPTY_RESULT`（无字幕）仍按「没有结果」处理，不算错误。

### 2026-10-20 第三轮修掉的六个问题（用户逐条点名）

1. **重跑批量会把已转录的全部重做**：`config.json` 里的 `overwrite: true` 压不住，
   因为 `put()` 吞掉 `False`、命令行没有反向开关。现在 `--overwrite` 是三态，`--no-overwrite`
   可以明确压掉配置（配置文件里也应设成 `false`）。
2. **小红书被风控后仍一条条跑完**：串行循环没有熔断，每条还带 60–180s 抖动，
   等于把 N 条的时间全耗掉才发现全废。现在连续失败到阈值就中止（见第 4 节）。
3. **B站列表拉取没有任何重试**：`iter_user_videos` 用的是最裸的 `opencli_json`，
   一次抖动就让整批在开头退出，而字幕/下载反而有重试。现在走预导航重试 + 指数退避
   （`opencli_retries` / `opencli_retry_base_delay`）；`check_opencli()` 同样会重试。
4. **小红书 `--limit` 拿的不是最新 N 条**：以前直接 `notes[:limit]`，顺序取决于接口返回。
   小红书笔记没有日期字段，但**ID 前 8 位十六进制就是创建时间的 Unix 秒**
   （日期按运行机本地时区换算）。现在按它降序排序，并顺带把日期填进 manifest。
5. **空转录混在「成功」里**：只打一行 WARNING、照样写空文件、返回 `status=ok`。
   现在单独 `status=empty`，汇总单列一档；`--fail-on-empty` 可让它计入失败。
6. **Ctrl+C 要等队列跑完**：`with ThreadPoolExecutor(...)` 退出时 `shutdown(wait=True)`。
   现在中断时 `cancel_futures=True`，只等已经在跑的少数几个自然结束。

顺带两处观感问题：`write_txt` 显式用 LF（以前 Windows 上产出 CRLF）；
「小红书抖动已启用」这句日志不再对纯 B站任务打印。

### 2026-10-20 第四轮：抖动改为按「全流程耗时」判定

**旧实现的问题**：`_last` 记在 `time.sleep()` **之前**，于是下一次的 `elapsed` 里混进了
上一次的等待时长。等待会自我抵扣——上一轮等了 100 秒，下一轮只要处理 2 秒就能压过 102 秒的
target。处理一慢，等待就恒为 0，抖动形同虚设（单条 185 秒时 93.5% 的 download 调用零等待）。

**现在**：`_last` 记在 `time.sleep()` **之后**，即请求真正发出的时刻。`elapsed` 只反映
「上一次请求发出 → 这一次请求发出」之间的下载 + 抽音频 + ASR + 落盘耗时。

| 全流程耗时 | 行为 |
|---|---|
| ≥ target | 不等 |
| < target | 等 `target − 全流程耗时` |

等价于 **实际间隔 = max(全流程耗时, target)**。效果：

| 单条全流程耗时 | 旧：零等待率 / 平均间隔 | 新：零等待率 / 平均间隔 |
|---|---|---|
| 40 s | 25.5% / 81 s | 0.0% / 108 s |
| 100 s | 54.1% / 114 s | 40.0% / 118 s |
| 160 s | 87.0% / 161 s | 80.7% / 161 s |
| 185 s | 93.5% / 185 s | 87.5% / 185 s |

最明显的是快速档：40 秒的处理在旧实现下平均 81 秒一条（低于名义下限 60 秒 + 处理耗时），
新实现下 108 秒一条，把「补足差值」真正做出来了。

> ⚠️ 全流程耗时仍会盖过 target：单条 185 秒时实际间隔就是 185 秒，target 抽到 61 秒也不会
> 让它变短——抖动只负责「补足」，不负责「拉长到比处理还慢」。要更慢只能把 `jitter.min`
> 提到高于单条耗时（如 240/300）。

离线回归：`tests/test_jitter.py`（8 项，虚拟时钟），
其中 `test_wait_does_not_credit_the_next_call` 专门钉住「这次等过的不许替下次抵扣」。

## 8. 文件结构

```
video-transcribe/
├── SKILL.md
├── config.example.json
├── scripts/
│   ├── vt.py                        # CLI 入口（目标解析 / 批量编排 / 落盘 / manifest）
│   ├── vt_common.py                 # 配置 / 日志 / opencli 调用 / 互斥锁 / 抖动 / 文件名 / 收尾
│   ├── vt_asr.py                    # ASR：API（标准库 multipart）+ 本地 whisper 兜底
│   ├── vt_bilibili.py               # B站：字幕优先、列表分页、合集 API、下载
│   ├── vt_xiaohongshu.py            # 小红书：下载、图文识别、用户主页列表
│   ├── cleanup_browser.py           # 收尾入口：后台关会话 → 关占位窗口 → 只读报告群组
│   └── cleanup_opencli_groups.ps1   # 群组的只读报告（UIA，不抢焦点；须 5.1 + UTF-8 BOM）
└── tests/                           # 离线测试（全桩，零网络；不需要真实 opencli / Chrome）
    ├── conftest.py                  # 桩与夹具
    ├── test_jitter.py               # 抖动语义（虚拟时钟）
    ├── test_fixes.py                # 2026-10-20 三轮修复
    ├── test_end_to_end.py           # 真的跑 vt.main()，验退出码
    └── test_portability.py          # 解释器选择 / BOM 自愈 / asr.model 留空
```

跑测试（在 skill 根目录，只依赖 pytest）：

```bash
python -m pytest tests -q
```

## 9. 排错

| 现象 | 原因与处理 |
|---|---|
| `opencli 状态异常` | Chrome 没开 / 扩展没连 / 目标站点没登录 |
| `opencli 返回失败: ...` | opencli 返回了 `{ok:false}` 信封。错误里带**完整** error / stdout / stderr，直接按里面的 code 与 message 排查 |
| `未配置 ASR 模型名` | `asr.model` 为空。用 `--asr-model` / `ASR_MODEL` / 配置文件填上你的接口实际部署的模型 |
| `没有可用的本地转录后端` | 未配 `api_base` 且两个本地 whisper 都没装 |
| `ASR 接口返回 HTTP 401` | api_key 不对（脚本对 401/403 不做回退、不重试，避免掩盖真因） |
| 转录结果是空白或极短 | 视频本身没有口播（纯 BGM/画面）；本 skill 不做内容过滤，会如实输出 |
| `找不到 ffmpeg` | 安装 ffmpeg 并确保 ffprobe 也在 PATH |
| 想确认是不是拿的字幕 | 看 `manifest.jsonl` 里的 `source` 字段（`manifest: false` 时没有这个文件） |
| 某些视频「没被处理」但没报错 | 看有没有「发布日期无法解析，已跳过」的 WARNING——那是日期解析失败，不是筛选结果 |
