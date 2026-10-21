"""离线测试基座：把 opencli 与 ASR 全部替换成桩，**不发任何真实网络请求**。

用户 2026-10-20 要求：测试必须注意风控，不能频繁、并行访问，每次访问间隔 30 秒。
这里彻底绕开这条约束——所有 opencli 调用都被 `run_opencli` 的桩接管，
ASR 的 `api_transcribe` 也被桩接管，测试过程中一个 socket 都不会打开。

跑法（在 skill 根目录）：
    python -m pytest tests -q
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

# 相对本文件定位，不写死任何安装路径：tests/ 与 scripts/ 是同级目录
SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


class FakeCompleted:
    """替代 subprocess.CompletedProcess 的最小实现。"""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture
def no_network(monkeypatch):
    """任何真实网络请求都直接让测试失败（兜底保险）。"""

    def boom(*args, **kwargs):
        raise AssertionError("测试中不允许发起真实网络请求")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    return boom


@pytest.fixture
def opencli_stub(monkeypatch):
    """把 run_opencli 换成可编程的桩。

    用法：
        opencli_stub.respond(["doctor"], FakeCompleted(0, '{"ok":true,"data":{...}}'))
        opencli_stub.respond_prefix(["bilibili", "user-videos"], FakeCompleted(...))
    """
    import vt_common as C

    calls: list[list] = []
    exact: dict[tuple, object] = {}
    prefixes: list[tuple[tuple, object]] = []

    def responder(args, **_kw):
        return FakeCompleted(0, "", "")

    def fake_run_opencli(args, *, timeout=300, use_lock=True, check=False):
        calls.append(list(args))
        key = tuple(str(a) for a in args)
        if key in exact:
            value = exact[key]
        else:
            value = None
            for prefix, candidate in prefixes:
                if key[:len(prefix)] == prefix:
                    value = candidate
                    break
            if value is None:
                value = responder(list(args))
        if callable(value):
            value = value(list(args))
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(C, "run_opencli", fake_run_opencli)

    class Stub:
        calls = calls

        @staticmethod
        def respond(args, result):
            exact[tuple(str(a) for a in args)] = result

        @staticmethod
        def respond_prefix(args, result):
            prefixes.append((tuple(str(a) for a in args), result))

        @staticmethod
        def set_default(fn):
            nonlocal responder
            responder = fn

        @staticmethod
        def count(prefix):
            prefix = tuple(str(a) for a in prefix)
            return sum(1 for c in calls if tuple(c)[:len(prefix)] == prefix)

    return Stub


@pytest.fixture
def no_cleanup(monkeypatch):
    """跳过浏览器收尾，避免测试去碰真实的 Chrome。"""
    import vt_common as C

    monkeypatch.setattr(C, "snapshot_chrome_windows", lambda: set())
    monkeypatch.setattr(C, "finalize_browser_cleanup",
                        lambda *a, **k: {"released": {"closed": [], "unbound": []},
                                         "blank_closed": [], "groups": None})
    return True
