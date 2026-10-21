# video-transcribe

把小红书 / B站 视频转成**纯文本转录**（下载 + ASR）。输出就是 ASR / 字幕的原文，不做格式化、不做内容过滤、不做摘要。

一个 [Agent Skill](https://docs.claude.com/en/docs/claude-code/skills)，纯 Python 3 标准库实现，**不需要 `pip install`**。

## 它能做什么

| | B站 | 小红书 |
|---|---|---|
| 文本来源 | **官方字幕优先**（人工或 AI 字幕），无字幕才下载音频走 ASR | 无平台字幕，始终 下载视频 → 抽音频 → ASR |
| 批量 | 用户主页 / 合集，**并行**（默认 4 线程） | 用户主页，**串行** + 随机抖动 |
| 访问间隔 | 无抖动 | 每次调用前抖动 60–180s（防风控） |

支持：单条视频、B站 UP 主主页批量、B站合集、小红书用户主页批量、直接输出到 stdout 不落盘。

## 前置依赖

- **[opencli](https://www.npmjs.com/package/opencli)** —— 负责实际下载，通过 Browser Bridge 扩展复用浏览器的登录态
- **Chrome / Chromium** 已装 opencli Browser Bridge 扩展，且目标站点已登录
- **ffmpeg**（含 `ffprobe`）在 PATH 中
- **ASR 能力二选一**：
  - API：OpenAI 兼容的 `/audio/transcriptions`，需要 `api_base` + `api_key` + **`model`**
  - 本地：`faster-whisper`（跨平台）或 `mlx-whisper`（仅 Apple Silicon）

> **平台边界**：核心流程（下载 / 抽音频 / ASR / 落盘）跨平台。
> 浏览器收尾（关 `about:blank` 占位窗口、查标签页群组）只在 **Windows** 生效，
> 依赖 Win32 API 与 PowerShell 5.1，其他平台自动跳过。

## 快速上手

```bash
# B站单视频（优先字幕，无字幕自动走 ASR）
python scripts/vt.py "https://www.bilibili.com/video/BV1xx411c7mD"

# 小红书单视频
python scripts/vt.py "https://www.xiaohongshu.com/explore/<id>?xsec_token=..."

# B站用户主页批量
python scripts/vt.py --user <mid> --limit 5 --since 2026-01-01

# B站合集
python scripts/vt.py --season "https://space.bilibili.com/<mid>/lists/<season_id>"

# 只要文本，不落盘
python scripts/vt.py "BV1xx411c7mD" --stdout-only
```

配置 ASR（三选一，优先级从高到低）：

```bash
# 1) 命令行
python scripts/vt.py "BV..." --api-base http://127.0.0.1:8000/v1 \
    --api-key KEY --asr-model <模型名>

# 2) 环境变量
ASR_API_BASE=http://... ASR_API_KEY=KEY ASR_MODEL=<模型名> python scripts/vt.py "BV..."

# 3) 配置文件
cp config.example.json config.json   # 然后填 api_base / api_key / model
python scripts/vt.py "BV..." --config config.json
```

> ⚠️ `asr.model` **没有默认值**，走 API 时必须显式指定 —— 具体填什么取决于你的接口部署了哪个模型。
> 留空会直接报 `未配置 ASR 模型名`，而不是拿一个猜的名字去请求。

## 输出

默认写 `./transcripts/`，用 `-o` 改：

```
{output}/B站/{作者}/{合集}/{标题} [{bv号}].txt     # 合集可为空
{output}/小红书/{作者}/{标题} [{笔记id}].txt
```

正文就是转录文本，**没有 frontmatter**。元数据在 `manifest.jsonl`（`platform / id / url / title / author / date / source / chars / txt / ts`），`source` 为 `subtitle` 或 `asr`，可用来判断哪些是拿的字幕、哪些是真转录。

已存在同名 txt 时跳过；要重做加 `--overwrite`。

### 退出码

| 码 | 含义 |
|---|---|
| `0` | 全部成功（含「已存在跳过」、`--skip-asr` 的仅下载、以及默认的空转录） |
| `1` | 执行失败，或批量里有条目失败 |
| `2` | 参数错误（时间参数冲突、平台无法识别、配置文件不存在） |
| `130` | 用户中断（Ctrl+C） |

批量模式结束时会逐条列出每个失败的视频（标题 / id / 链接 / 完整错误），可直接用于定时任务判成败。

## 测试

全离线，opencli 与 ASR 全部走桩，**不发任何真实网络请求**，不需要真实 opencli 或 Chrome：

```bash
python -m pytest tests -q
```

48 项，覆盖抖动语义（虚拟时钟）、退出码接线、批量熔断、PowerShell 解释器选择、BOM 自愈等。

## 设计说明

几条踩过坑才定下来的做法，写在 `SKILL.md` 里：

- **抖动按「全流程耗时」判定**：`实际间隔 = max(处理耗时, target)`。处理比预设慢就不额外等，比预设快才补足差值。早先版本把计时点记在 `sleep` 之前，等待会自我抵扣，处理一慢抖动就形同虚设。
- **全程后台**：所有 opencli 调用带 `--window background` 并注入 `OPENCLI_WINDOW=background`。收尾只做能后台完成的动作 —— 删除标签页群组需要切前台，所以这个能力被整个移除了，发现残留只报告。
- **收尾必须用 PowerShell 5.1**：同一段 UIA 遍历，5.1 约 1.7 秒返回，pwsh 7 直接卡死。脚本还需要 UTF-8 BOM，否则 5.1 按 ANSI 解码中文会解析失败。
- **opencli 的退出码不可靠**：偶发非零退出但文件已下载成功。判据是「文件存在 + 有音频轨」，不是退出码。

## 许可

MIT
