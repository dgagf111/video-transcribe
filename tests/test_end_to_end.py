"""端到端：真的跑 vt.main()，确认退出码接线正确，且全程零网络。

`no_network` 夹具把 urllib 的 urlopen 换成直接失败的桩——如果任何一条路径
偷偷发了真实请求（B站公开 API、ASR 接口、小红书），测试立刻报错。
因此这套测试对小红书风控是**完全无接触**的。
"""

from __future__ import annotations

import pytest

from conftest import FakeCompleted


DOCTOR_OK = FakeCompleted(0, '{"ok":true,"data":{"daemonRunning":true,'
                             '"extensionConnected":true,"connectivity":{"ok":true}}}')


@pytest.fixture
def bili_env(tmp_path, monkeypatch, no_network, no_cleanup):
    """搭一个 B站单视频环境：字幕路径，不下载、不 ASR、不开浏览器。"""
    import vt
    import vt_bilibili

    monkeypatch.setattr(vt_bilibili, "get_video_info", lambda bvid, **k: {
        "title": "测试视频", "author": "测试UP", "publish_time": "2026-01-01 12:00",
        "parts": "1",
    })

    monkeypatch.setattr(vt.C, "run_opencli",
                        lambda args, **kw: DOCTOR_OK)

    state = {"subtitle": "这是字幕正文。", "downloaded": False}

    def fake_subtitle(bvid, lang="zh-CN", **kw):
        return state["subtitle"]

    def fake_download(bvid, out_dir, cfg):
        state["downloaded"] = True
        raise AssertionError("走字幕路径时不该下载")

    monkeypatch.setattr(vt_bilibili, "get_subtitle", fake_subtitle)
    monkeypatch.setattr(vt_bilibili, "download_media", fake_download)

    out = tmp_path / "out"
    state["out"] = out
    return state


def test_single_bilibili_success_exit_0(bili_env):
    import vt

    code = vt.main(["BV1xx411c7mD", "-o", str(bili_env["out"]), "--no-manifest"])
    assert code == 0
    files = list(bili_env["out"].rglob("*.txt"))
    assert len(files) == 1
    assert files[0].read_text(encoding="utf-8").strip() == "这是字幕正文。"
    assert not bili_env["downloaded"]


def test_single_bilibili_download_failure_exit_1(bili_env, monkeypatch):
    """字幕没有 + 下载失败 → 执行失败，退出码 1。"""
    import vt
    import vt_bilibili

    bili_env["subtitle"] = ""

    def failing_download(bvid, out_dir, cfg):
        raise RuntimeError("下载真的失败了")

    monkeypatch.setattr(vt_bilibili, "download_media", failing_download)

    code = vt.main(["BV1xx411c7mD", "-o", str(bili_env["out"]), "--no-manifest"])
    assert code == 1


def test_single_bilibili_empty_transcript_fails_with_flag(bili_env, monkeypatch):
    """--fail-on-empty 时空转录退出 1。"""
    import vt
    import vt_bilibili

    # 让 extract_text 直接返回空文本（模拟纯音乐视频）
    monkeypatch.setattr(vt_bilibili, "extract_text",
                        lambda bvid, cfg, tr, wd: {"text": "", "source": "asr", "media": None})

    code = vt.main(["BV1xx411c7mD", "-o", str(bili_env["out"]), "--no-manifest",
                    "--fail-on-empty"])
    assert code == 1


def test_single_bilibili_empty_transcript_ok_without_flag(bili_env, monkeypatch):
    import vt
    import vt_bilibili

    monkeypatch.setattr(vt_bilibili, "extract_text",
                        lambda bvid, cfg, tr, wd: {"text": "", "source": "asr", "media": None})

    code = vt.main(["BV1xx411c7mD", "-o", str(bili_env["out"]), "--no-manifest"])
    assert code == 0


def test_second_run_skips_without_overwrite(bili_env):
    """默认幂等：第二次跑直接跳过，不重新取文本。"""
    import vt
    import vt_bilibili

    out = bili_env["out"]
    assert vt.main(["BV1xx411c7mD", "-o", str(out), "--no-manifest"]) == 0

    calls = {"n": 0}

    def counting_subtitle(*a, **k):
        calls["n"] += 1
        return "不该被调用"

    bili_env_orig = vt_bilibili.get_subtitle
    vt_bilibili.get_subtitle = counting_subtitle
    try:
        assert vt.main(["BV1xx411c7mD", "-o", str(out), "--no-manifest"]) == 0
    finally:
        vt_bilibili.get_subtitle = bili_env_orig

    assert calls["n"] == 0, "没有 --overwrite 时不该重新取文本"


def test_second_run_redoes_with_overwrite(bili_env):
    """--overwrite 才会重做。"""
    import vt
    import vt_bilibili

    out = bili_env["out"]
    assert vt.main(["BV1xx411c7mD", "-o", str(out), "--no-manifest"]) == 0

    calls = {"n": 0}

    def counting_subtitle(bvid, lang="zh-CN", **k):
        calls["n"] += 1
        return "重做后的文本"

    orig = vt_bilibili.get_subtitle
    vt_bilibili.get_subtitle = counting_subtitle
    try:
        assert vt.main(["BV1xx411c7mD", "-o", str(out), "--no-manifest",
                        "--overwrite"]) == 0
    finally:
        vt_bilibili.get_subtitle = orig

    assert calls["n"] == 1
    txt = next((out).rglob("*.txt"))
    assert "重做后的文本" in txt.read_text(encoding="utf-8")


def test_no_overwrite_flag_wins_over_config(tmp_path, bili_env):
    """config 里 overwrite: true，--no-overwrite 仍然跳过。"""
    import json

    import vt

    cfg_file = tmp_path / "c.json"
    cfg_file.write_text(json.dumps({"overwrite": True}), encoding="utf-8")

    out = bili_env["out"]
    assert vt.main(["BV1xx411c7mD", "-o", str(out), "--no-manifest"]) == 0

    import vt_bilibili
    calls = {"n": 0}

    def counting(bvid, lang="zh-CN", **k):
        calls["n"] += 1
        return "不该被调用"

    orig = vt_bilibili.get_subtitle
    vt_bilibili.get_subtitle = counting
    try:
        vt.main(["BV1xx411c7mD", "-o", str(out), "--no-manifest",
                 "--config", str(cfg_file), "--no-overwrite"])
    finally:
        vt_bilibili.get_subtitle = orig
    assert calls["n"] == 0
