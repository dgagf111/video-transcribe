"""video-transcribe · ASR 层。

优先走 OpenAI 兼容的 /audio/transcriptions（模型名由 asr.model 指定），
未配置或调用失败时回退本地 whisper。

- API 上传用标准库手写 multipart/form-data，不依赖 openai 包
- 本地后端支持 mlx-whisper（Apple Silicon）与 faster-whisper（跨平台，可选）
- 送进 ASR 前统一转成 64k 单声道 m4a，避免把整个视频轨塞给接口
"""

from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

LOG = logging.getLogger("video-transcribe")

_FFMPEG = "ffmpeg"
_FFPROBE = "ffprobe"

_faster_lock = threading.Lock()
_faster_models: dict = {}


class AsrError(RuntimeError):
    """ASR 相关错误。"""


class AsrAuthError(AsrError):
    """鉴权失败（401/403）。这类错误不该被「回退本地」掩盖，必须原样抛出。"""


# ---------------------------------------------------------------------------
# 媒体探测与音频准备
# ---------------------------------------------------------------------------

def _run_tool(cmd: list) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=3600)
    except FileNotFoundError as e:
        tool = cmd[0]
        raise AsrError(
            f"找不到 {tool}，请先安装 ffmpeg（含 ffprobe 与 ffmpeg）并确保在 PATH 中。"
        ) from e
    except subprocess.TimeoutExpired as e:
        raise AsrError(f"{cmd[0]} 执行超时: {' '.join(cmd[:6])}...") from e


def probe_streams(path: str | Path) -> set[str]:
    """用 ffprobe 取流类型集合，例如 {'video', 'audio'}。"""
    proc = _run_tool([
        _FFPROBE, "-v", "error",
        "-show_entries", "stream=codec_type",
        "-of", "csv=p=0", str(path),
    ])
    if proc.returncode != 0:
        raise AsrError(f"ffprobe 解析失败: {path}\n  {(proc.stderr or '').strip()}")
    return {line.strip() for line in (proc.stdout or "").splitlines() if line.strip()}


def has_audio_stream(path: str | Path) -> bool:
    return "audio" in probe_streams(path)


_PASSTHROUGH_SUFFIXES = (".m4a", ".mp3", ".wav", ".aac", ".flac", ".ogg", ".opus")


def to_asr_audio(media_path: str | Path, workdir: str | Path, *, bitrate: str = "64k",
                 compress_mb: float = 80) -> Path:
    """把下载到的媒体变成适合送 ASR 的音频文件。

    规则：
    - 没有音频轨 → 直接报错（opencli 偶尔会挑到纯视频流）
    - 已经是纯音频且体积未超阈值 → 原样返回，不重复转码
    - 含视频轨或体积超阈值 → ffmpeg 转成单声道 64k m4a
    """
    src = Path(media_path)
    if not src.is_file():
        raise AsrError(f"媒体文件不存在: {src}")

    streams = probe_streams(src)
    if "audio" not in streams:
        raise AsrError(f"文件不含音频轨（可能下到了纯视频流）: {src}")

    size_mb = src.stat().st_size / 1024 / 1024
    if "video" not in streams and src.suffix.lower() in _PASSTHROUGH_SUFFIXES and size_mb <= compress_mb:
        return src

    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    out = workdir / (src.stem[:60] + ".asr.m4a")
    proc = _run_tool([
        _FFMPEG, "-y", "-i", str(src),
        "-vn", "-ac", "1", "-b:a", bitrate,
        "-f", "mp4", str(out),
    ])
    if proc.returncode != 0 or not out.is_file():
        raise AsrError(
            f"ffmpeg 抽取音频失败: {src}\n  {(proc.stderr or '').strip()[-400:]}"
        )
    LOG.debug("音频转换: %.1fMB → %.1fMB (%s)", size_mb, out.stat().st_size / 1024 / 1024, out.name)
    return out


# ---------------------------------------------------------------------------
# 文本质量检查
# ---------------------------------------------------------------------------

def has_excessive_repetition(text: str, max_repeat: int = 3, phrase_len: int = 15) -> bool:
    """检测 whisper 类模型的重复幻觉：任一 15-gram 出现超过 max_repeat 次。"""
    if len(text) < phrase_len * max_repeat:
        return False
    seen: dict[str, int] = {}
    for i in range(len(text) - phrase_len + 1):
        gram = text[i:i + phrase_len]
        count = seen.get(gram, 0) + 1
        seen[gram] = count
        if count > max_repeat:
            return True
    return False


# ---------------------------------------------------------------------------
# API ASR（标准库 multipart）
# ---------------------------------------------------------------------------

def _guess_content_type(path: Path) -> str:
    return {
        ".m4a": "audio/mp4", ".mp4": "video/mp4", ".mp3": "audio/mpeg",
        ".wav": "audio/wav", ".aac": "audio/aac", ".flac": "audio/flac",
        ".ogg": "audio/ogg", ".opus": "audio/opus", ".webm": "video/webm",
    }.get(path.suffix.lower(), "application/octet-stream")


def _safe_upload_name(path: Path, fallback: str = "audio") -> str:
    """multipart 的 filename 用 ASCII，避免个别服务端解析非 ASCII 文件名出错。"""
    try:
        path.name.encode("ascii")
        return path.name
    except UnicodeEncodeError:
        return fallback + path.suffix


def _multipart_body(fields: dict, file_path: Path, file_field: str = "file"):
    boundary = "----video-transcribe" + uuid.uuid4().hex
    chunks: list[bytes] = []
    for key, value in fields.items():
        if value is None:
            continue
        chunks.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode("utf-8")
        )
    chunks.append(
        (f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; '
         f'filename="{_safe_upload_name(file_path)}"\r\n'
         f'Content-Type: {_guess_content_type(file_path)}\r\n\r\n').encode("utf-8")
    )
    chunks.append(file_path.read_bytes())
    chunks.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks), boundary


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in (408, 425, 429) or exc.code >= 500
    if isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError, OSError)):
        return True
    return False


def api_transcribe(audio_path: Path, cfg: dict) -> str:
    """POST {api_base}/audio/transcriptions，返回文本。"""
    asr = cfg["asr"]
    api_base = str(asr["api_base"]).rstrip("/")
    url = api_base + "/audio/transcriptions"
    # 模型名没有通用默认值：不同部署挂的模型完全不同。留空时直接报可操作的错，
    # 而不是拿一个猜出来的名字去请求、再收到与「模型不存在」无关的报错。
    model = str(asr.get("model") or "").strip()
    if not model:
        raise AsrError(
            "未配置 ASR 模型名（asr.model 为空）。\n"
            "  请用 --asr-model <模型名>、环境变量 ASR_MODEL，"
            "或在配置文件里写 asr.model。\n"
            "  具体填什么取决于你的接口部署了哪个模型（例如 faster-whisper 服务常见的 "
            "Systran/faster-whisper-large-v3）。"
        )
    fields = {"model": model}
    language = str(asr.get("language") or "").strip()
    if language and language.lower() != "auto":
        fields["language"] = language

    body, boundary = _multipart_body(fields, audio_path)
    headers = {
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Content-Length": str(len(body)),
        "Accept": "application/json",
        "User-Agent": "video-transcribe/1.0",
    }
    api_key = str(asr.get("api_key") or "")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    attempts = int(asr.get("max_retries", 3)) + 1
    base_delay = float(asr.get("retry_base_delay", 1.0))
    timeout = float(asr.get("timeout", 1800))
    last_exc: BaseException | None = None

    for attempt in range(attempts):
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
            return _parse_api_text(raw, url)
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")
            except Exception:
                pass
            message = f"ASR 接口返回 HTTP {e.code}: {detail or e.reason}"
            # 401/403 是配置问题，重试与回退都没意义，直接抛
            if e.code in (401, 403):
                raise AsrAuthError(
                    f"{message}\n"
                    "  请检查 asr.api_key / --api-key（或环境变量 ASR_API_KEY）是否正确。"
                ) from e
            last_exc = AsrError(message)
            if not _is_retryable(e):
                raise last_exc from e
        except Exception as e:  # noqa: BLE001 —— 交给 _is_retryable 判定
            last_exc = e
            if not _is_retryable(e):
                raise AsrError(f"ASR 请求失败: {type(e).__name__}: {e}") from e

        if attempt < attempts - 1:
            delay = base_delay * (2 ** attempt)
            LOG.warning("ASR 第 %d/%d 次失败，%.1fs 后重试: %s", attempt + 1, attempts, delay,
                        last_exc)
            time.sleep(delay)

    raise AsrError(f"ASR 重试 {attempts} 次仍失败: {last_exc}")


def _parse_api_text(raw: str, url: str) -> str:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise AsrError(f"ASR 返回不是 JSON（{url}）: {raw}") from e
    if isinstance(data, dict):
        for key in ("text", "transcription", "result"):
            value = data.get(key)
            if isinstance(value, str):
                return value.strip()
        raise AsrError(f"ASR 返回中找不到 text 字段（{url}）: {raw}")
    raise AsrError(f"ASR 返回结构异常（{url}）: {raw}")


# ---------------------------------------------------------------------------
# 本地 whisper
# ---------------------------------------------------------------------------

def _lang_or_none(language: str) -> str | None:
    value = (language or "").strip()
    return None if not value or value.lower() == "auto" else value


def _transcribe_mlx(path: Path, repo: str, language: str) -> str:
    import mlx_whisper  # 仅 Apple Silicon 可导入

    result = mlx_whisper.transcribe(str(path), path_or_hf_repo=repo,
                                    language=_lang_or_none(language))
    return (result.get("text") or "").strip()


def _transcribe_faster(path: Path, repo: str, language: str) -> str:
    from faster_whisper import WhisperModel

    with _faster_lock:
        model = _faster_models.get(repo)
        if model is None:
            LOG.info("加载 faster-whisper 模型: %s（首次较慢）", repo)
            model = WhisperModel(repo, device="auto", compute_type="int8")
            _faster_models[repo] = model
    segments, _info = model.transcribe(str(path), language=_lang_or_none(language), vad_filter=True)
    return "".join(seg.text for seg in segments).strip()


def _local_backend(cfg: dict) -> tuple[str, str]:
    """返回 (后端名, 模型名)。backend 可为 auto|api|local|mlx|faster-whisper|none。"""
    asr = cfg["asr"]
    requested = str(asr.get("backend", "auto")).lower()
    repo = str(asr.get("local_model") or "")
    importlib = __import__("importlib.util", fromlist=["find_spec"])

    def has(module: str) -> bool:
        try:
            return importlib.find_spec(module) is not None
        except (ImportError, ValueError):
            return False

    if requested in ("mlx",):
        if not has("mlx_whisper"):
            raise AsrError("指定了 mlx 后端，但 mlx-whisper 未安装（仅 Apple Silicon 可用）")
        return "mlx", repo or asr["default_mlx_model"]
    if requested in ("faster", "faster-whisper", "faster_whisper"):
        if not has("faster_whisper"):
            raise AsrError("指定了 faster-whisper 后端，但未安装：pip install faster-whisper")
        return "faster", repo or asr["default_faster_model"]

    # auto / local：mlx 优先（Apple Silicon 上更快），否则 faster-whisper
    if has("mlx_whisper"):
        return "mlx", repo or asr["default_mlx_model"]
    if has("faster_whisper"):
        return "faster", repo or asr["default_faster_model"]

    raise AsrError(
        "没有可用的本地转录后端：mlx-whisper（Apple Silicon）与 faster-whisper 都未安装。\n"
        "  方案一：配置 asr.api_base / asr.api_key 走 API ASR；\n"
        "  方案二：pip install faster-whisper 后使用本地转录。"
    )


def local_transcribe(audio_path: Path, cfg: dict, *, force_model: str | None = None) -> tuple[str, str]:
    """本地转录，返回 (文本, 后端标签)。检测到重复幻觉时自动换 fallback 模型。"""
    asr = cfg["asr"]
    backend, repo = _local_backend(cfg)
    if force_model:
        repo = force_model
    language = str(asr.get("language") or "")

    if backend == "mlx":
        text = _transcribe_mlx(audio_path, repo, language)
    else:
        text = _transcribe_faster(audio_path, repo, language)

    if not force_model and has_excessive_repetition(text):
        fallback = str(asr.get("local_fallback_model") or "")
        if not fallback:
            if backend == "mlx":
                fallback = "mlx-community/whisper-medium-mlx-8bit"
            else:
                fallback = "medium"
        if fallback != repo:
            LOG.warning("检测到重复幻觉，改用 fallback 模型重转: %s", fallback)
            return local_transcribe(audio_path, cfg, force_model=fallback), f"{backend}:{fallback}"

    return text, f"{backend}:{repo}"


# ---------------------------------------------------------------------------
# 统一入口
# ---------------------------------------------------------------------------

class Transcriber:
    """按配置选择 API ASR 或本地 whisper。"""

    def __init__(self, cfg: dict):
        self._cfg = cfg
        self._asr = cfg["asr"]

    def _use_api(self) -> bool:
        backend = str(self._asr.get("backend", "auto")).lower()
        if backend == "local":
            return False
        if backend == "api":
            return True
        return bool(str(self._asr.get("api_base") or "").strip())

    def transcribe(self, media_path: str | Path, workdir: str | Path) -> tuple[str, str, float]:
        """返回 (文本, 后端标签, 耗时秒数)。"""
        t0 = time.time()
        backend = str(self._asr.get("backend", "auto")).lower()
        if backend == "none":
            raise AsrError("asr.backend=none，已禁用转录")

        audio = to_asr_audio(
            media_path, workdir,
            compress_mb=float(self._asr.get("compress_mb", 80)),
        )

        if self._use_api():
            if not str(self._asr.get("api_base") or "").strip():
                raise AsrError("asr.backend=api 但未配置 asr.api_base")
            api_error: AsrError | None = None
            try:
                text = api_transcribe(audio, self._cfg)
                return text, f"api:{self._asr.get('model') or '?'}", time.time() - t0
            except AsrAuthError:
                raise  # 鉴权问题不能靠回退掩盖
            except AsrError as e:
                if backend == "api":
                    raise
                api_error = e
                LOG.warning("API ASR 失败，尝试回退本地 whisper: %s", e)
            except Exception as e:  # noqa: BLE001
                if backend == "api":
                    raise
                api_error = AsrError(f"{type(e).__name__}: {e}")
                LOG.warning("API ASR 异常，尝试回退本地 whisper: %s", e)

            try:
                text, label = local_transcribe(audio, self._cfg)
                return text, label, time.time() - t0
            except AsrError as local_error:
                raise AsrError(
                    f"API ASR 失败：{api_error}\n"
                    f"  本地回退也不可用：{local_error}"
                ) from local_error

        text, label = local_transcribe(audio, self._cfg)
        return text, label, time.time() - t0
