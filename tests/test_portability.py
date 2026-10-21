"""离线验证 2026-10-21 修掉的「只在本机成立」问题。

覆盖三处：
1. 收尾脚本必须用 Windows PowerShell 5.1（pwsh 7 的 UIA 遍历会卡死）
2. ps1 缺 UTF-8 BOM 时自愈（5.1 读无 BOM 的 UTF-8 会解析失败）
3. asr.model 默认留空，未配置时给出可操作的错误

全部走桩，不发真实请求、不碰真实 Chrome。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from conftest import FakeCompleted


# ---------------------------------------------------------------------------
# 问题 1：PowerShell 解释器选择
# ---------------------------------------------------------------------------

def test_prefers_windows_powershell_over_pwsh(monkeypatch):
    """必须优先 powershell.exe（5.1），因为 pwsh 7 跑 UIA 遍历会卡死。"""
    import vt_common as C

    def fake_which(name):
        return {"powershell": r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.EXE",
                "pwsh": r"C:\Program Files\PowerShell\7\pwsh.exe"}.get(name)

    monkeypatch.setattr(C.shutil, "which", fake_which)
    chosen = C._powershell_exe()
    assert chosen.endswith("powershell.EXE"), f"应选 5.1，实际选了 {chosen}"


def test_falls_back_to_pwsh_when_51_missing(monkeypatch):
    """没有 5.1 时退回 pwsh，至少保留可用性。"""
    import vt_common as C

    def fake_which(name):
        return {"pwsh": r"C:\Program Files\PowerShell\7\pwsh.exe"}.get(name)

    monkeypatch.setattr(C.shutil, "which", fake_which)
    assert C._powershell_exe().endswith("pwsh.exe")


def test_report_groups_uses_selected_exe(monkeypatch, tmp_path):
    """report_opencli_groups 实际调用的是 _powershell_exe() 选出的解释器。"""
    import vt_common as C

    used: list[str] = []

    def fake_run(argv, **_kw):
        used.append(argv[0])
        return FakeCompleted(0, 'VT_GROUPS_JSON:{"count":0,"names":[]}', "")

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(C, "_powershell_exe", lambda: "powershell.exe")
    monkeypatch.setattr(C.subprocess, "run", fake_run)

    # 用真实存在的脚本文件，绕开 is_file() 检查
    script = Path(C.__file__).resolve().parent / "cleanup_opencli_groups.ps1"
    assert script.is_file(), "测试前提：收尾脚本应存在"

    result = C.report_opencli_groups()
    assert used == ["powershell.exe"]
    assert result == {"count": 0, "names": []}


# ---------------------------------------------------------------------------
# 问题 2：UTF-8 BOM 自愈
# ---------------------------------------------------------------------------

def test_bom_added_when_missing(tmp_path):
    """无 BOM 的 ps1 会被复制成带 BOM 的临时副本。"""
    import vt_common as C

    src = tmp_path / "no_bom.ps1"
    src.write_bytes("Write-Output '中文'\n".encode("utf-8"))   # 无 BOM

    fixed = C._ensure_utf8_bom(src)
    assert fixed != src, "缺 BOM 时应返回临时副本"
    assert fixed.read_bytes().startswith(b"\xef\xbb\xbf")


def test_bom_left_alone_when_present(tmp_path):
    """已带 BOM 的文件原样返回，不产生临时副本。"""
    import vt_common as C

    src = tmp_path / "with_bom.ps1"
    src.write_bytes(b"\xef\xbb\xbf" + "Write-Output '中文'\n".encode("utf-8"))

    assert C._ensure_utf8_bom(src) == src


def test_bom_selfheal_preserves_content(tmp_path):
    """补 BOM 不改变脚本正文（只多 3 个字节）。"""
    import vt_common as C

    body = "param([switch]$Report)\n# 中文注释\nWrite-Output 'ok'\n"
    src = tmp_path / "x.ps1"
    src.write_bytes(body.encode("utf-8"))

    fixed = C._ensure_utf8_bom(src)
    assert fixed.read_bytes()[3:].decode("utf-8") == body


def test_repo_script_has_bom():
    """仓库里的收尾脚本本身应当带 BOM（手工直接跑 powershell 时需要）。

    即使这里失败，运行路径也有 _ensure_utf8_bom 兜底，不会真的坏掉；
    但保持文件自带 BOM 能让「手工执行脚本」这条路径同样可用。
    """
    import vt_common as C

    script = Path(C.__file__).resolve().parent / "cleanup_opencli_groups.ps1"
    assert script.read_bytes().startswith(b"\xef\xbb\xbf"), (
        "cleanup_opencli_groups.ps1 缺 UTF-8 BOM，"
        "PowerShell 5.1 会解析失败（运行时有兜底，但手工跑会报错）"
    )


# ---------------------------------------------------------------------------
# 问题 3：asr.model 默认留空 + 明确报错
# ---------------------------------------------------------------------------

def test_default_asr_model_is_empty():
    """默认配置不能猜一个具体模型名。"""
    import vt_common as C

    assert C.DEFAULTS["asr"]["model"] == "", "默认模型名必须留空，否则别人开箱即错"


def test_api_transcribe_rejects_empty_model(tmp_path):
    """未配置模型时直接报可操作的错，不发请求。"""
    from vt_asr import AsrError, api_transcribe

    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"fake")

    cfg = {"asr": {"api_base": "http://127.0.0.1:8000/v1", "api_key": "k", "model": ""}}
    with pytest.raises(AsrError, match="未配置 ASR 模型名"):
        api_transcribe(audio, cfg)


def test_api_transcribe_rejects_whitespace_model(tmp_path):
    """只有空白的模型名同样算未配置。"""
    from vt_asr import AsrError, api_transcribe

    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"fake")

    cfg = {"asr": {"api_base": "http://127.0.0.1:8000/v1", "api_key": "k", "model": "   "}}
    with pytest.raises(AsrError, match="未配置 ASR 模型名"):
        api_transcribe(audio, cfg)


def test_example_config_model_is_empty():
    """config.example.json 里也不能带具体模型名或本机端口。"""
    import json
    import vt_common as C

    path = Path(C.__file__).resolve().parent.parent / "config.example.json"
    data = json.loads(path.read_text(encoding="utf-8"))

    assert data["asr"]["model"] == ""
    assert data["asr"]["api_base"] == "http://127.0.0.1:8000/v1", (
        "示例配置不该带本机实际使用的端口"
    )
