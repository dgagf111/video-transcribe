# video-transcribe

Turn 小红书 / B站 videos into **plain-text transcripts** (download + ASR). The output is the raw ASR / subtitle text — no formatting, no content filtering, no summarization.

An [Agent Skill](https://docs.claude.com/en/docs/claude-code/skills), written in pure Python 3 standard library. **No `pip install` required.**

## What it does

| | Bilibili | Xiaohongshu |
|---|---|---|
| Text source | **Official subtitles first**, ASR only when absent | No platform subtitles — always download → extract audio → ASR |
| Batch | User profile / collection, **parallel** (4 threads) | User profile, **serial** + randomized jitter |
| Throttling | none | 60–180s jitter before each call (anti-bot) |

Supports single videos, Bilibili uploader profiles, Bilibili collections, Xiaohongshu user profiles, and stdout-only mode.

## Requirements

- **[opencli](https://www.npmjs.com/package/opencli)** — performs the actual downloads and reuses your browser login via Browser Bridge
- **Chrome / Chromium** with the opencli Browser Bridge extension, logged in to the target sites
- **ffmpeg** (including `ffprobe`) on PATH
- **ASR, one of**:
  - API: any OpenAI-compatible `/audio/transcriptions`, needs `api_base` + `api_key` + **`model`**
  - Local: `faster-whisper` (cross-platform) or `mlx-whisper` (Apple Silicon only)

> **Platform note**: the core pipeline (download / audio extraction / ASR / file output) is cross-platform.
> Browser cleanup (closing `about:blank` placeholder windows, checking tab groups) is **Windows-only** —
> it relies on Win32 APIs and PowerShell 5.1. Other platforms skip it silently.

## Quick start

```bash
# Bilibili single video (subtitles preferred, ASR fallback)
python scripts/vt.py "https://www.bilibili.com/video/BV1xx411c7mD"

# Xiaohongshu single video
python scripts/vt.py "https://www.xiaohongshu.com/explore/<id>?xsec_token=..."

# Bilibili uploader profile
python scripts/vt.py --user <mid> --limit 5 --since 2026-01-01

# Bilibili collection
python scripts/vt.py --season "https://space.bilibili.com/<mid>/lists/<season_id>"

# Print to stdout, write nothing
python scripts/vt.py "BV1xx411c7mD" --stdout-only
```

Configuring ASR (three ways, highest priority first):

```bash
# 1) CLI flags
python scripts/vt.py "BV..." --api-base http://127.0.0.1:8000/v1 \
    --api-key KEY --asr-model <model-name>

# 2) Environment variables
ASR_API_BASE=http://... ASR_API_KEY=KEY ASR_MODEL=<model-name> python scripts/vt.py "BV..."

# 3) Config file
cp config.example.json config.json   # then fill in api_base / api_key / model
python scripts/vt.py "BV..." --config config.json
```

> ⚠️ `asr.model` has **no default**. When using an API endpoint you must set it explicitly —
> what to put there depends on which model your endpoint serves. Leaving it empty raises
> `未配置 ASR 模型名` instead of sending a guessed name upstream.

## Output

Writes to `./transcripts/` by default; override with `-o`:

```
{output}/B站/{author}/{collection}/{title} [{bvid}].txt     # collection may be empty
{output}/小红书/{author}/{title} [{note_id}].txt
```

The body is the transcript itself, with **no frontmatter**. Metadata goes to `manifest.jsonl`
(`platform / id / url / title / author / date / source / chars / txt / ts`). `source` is
`subtitle` or `asr`, so you can tell which files are real transcripts and which are platform subtitles.

Existing files are skipped; pass `--overwrite` to redo them.

### Exit codes

| Code | Meaning |
|---|---|
| `0` | All good (including skipped-existing, `--skip-asr` downloads, and empty transcripts by default) |
| `1` | Execution failed, or at least one batch item failed |
| `2` | Bad arguments (conflicting date flags, unrecognized platform, missing config file) |
| `130` | Interrupted (Ctrl+C) |

Batch mode lists every failed video individually (title / id / url / full error), so it plugs straight into cron or CI.

## Tests

Fully offline — opencli and ASR are stubbed out, **no real network requests**, no real opencli or Chrome needed:

```bash
python -m pytest tests -q
```

48 tests covering jitter semantics (virtual clock), exit-code wiring, batch circuit breaker, PowerShell interpreter selection, and BOM self-healing.

## Design notes

A few decisions that came out of debugging, documented in `SKILL.md`:

- **Jitter is measured against full pipeline time**: `effective interval = max(processing time, target)`. If processing is slower than the target, no extra wait; if faster, top up the difference. Earlier versions recorded the timestamp *before* `sleep`, so the wait credited itself away and jitter became a no-op whenever processing was slow.
- **Background only**: every opencli call passes `--window background` and injects `OPENCLI_WINDOW=background`. Cleanup only does what can be done in the background — deleting tab groups requires bringing a window to the foreground, so that capability was removed entirely; leftovers are reported, not deleted.
- **Cleanup must use PowerShell 5.1**: for the same UIA traversal, 5.1 returns in ~1.7s while pwsh 7 hangs. The script also needs a UTF-8 BOM, otherwise 5.1 decodes the Chinese comments as ANSI and fails to parse.
- **opencli exit codes are unreliable**: it sometimes exits non-zero after a successful download. The real check is "file exists and has an audio stream", not the exit code.

## License

MIT
