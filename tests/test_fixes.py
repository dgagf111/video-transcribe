"""离线验证 2026-10-20 第三轮修掉的 8 个问题。

全部走桩：opencli 与 ASR 都不发真实请求，因此不存在风控与访问间隔问题。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from conftest import FakeCompleted


# ---------------------------------------------------------------------------
# 问题 1：overwrite 三态
# ---------------------------------------------------------------------------

def test_overwrite_defaults_to_config_value():
    """不写 --overwrite 时，用配置里的值（默认 False）。"""
    import vt

    args = vt.build_parser().parse_args(["BV1xx411c7mD"])
    cfg = vt.build_config(args)
    assert cfg["overwrite"] is False


def test_no_overwrite_beats_config_true(tmp_path):
    """config 里 overwrite: true，命令行 --no-overwrite 必须能压掉它。"""
    import vt

    cfg_file = tmp_path / "c.json"
    cfg_file.write_text(json.dumps({"overwrite": True}), encoding="utf-8")

    args = vt.build_parser().parse_args(["BV1xx411c7mD", "--config", str(cfg_file)])
    assert vt.build_config(args)["overwrite"] is True

    args = vt.build_parser().parse_args(
        ["BV1xx411c7mD", "--config", str(cfg_file), "--no-overwrite"])
    assert vt.build_config(args)["overwrite"] is False


def test_overwrite_flag_still_works():
    import vt

    args = vt.build_parser().parse_args(["BV1xx411c7mD", "--overwrite"])
    assert vt.build_config(args)["overwrite"] is True


# ---------------------------------------------------------------------------
# 问题 5：空转录单独状态
# ---------------------------------------------------------------------------

def _make_cfg(tmp_path, **extra):
    import vt

    cfg = vt.C.deep_merge(vt.C.deep_merge({}, vt.C.DEFAULTS), {
        "output_dir": str(tmp_path),
        "manifest": False,
    })
    cfg.update(extra)
    return cfg


def test_empty_transcript_gets_empty_status(tmp_path):
    import vt

    cfg = _make_cfg(tmp_path)
    result = vt.handle_item(
        cfg=cfg, platform="bilibili", vid="BV1", url="https://x/BV1",
        title="纯音乐", author="作者", extract_fn=lambda: {"text": "", "source": "asr"},
        download_fn=lambda: tmp_path / "m.mp4",
    )
    assert result["status"] == "empty"
    assert result["chars"] == 0


def test_empty_transcript_counts_as_success_by_default(tmp_path):
    import vt

    results = [
        {"status": "ok", "title": "a", "vid": "1", "url": "u"},
        {"status": "empty", "title": "b", "vid": "2", "url": "u"},
    ]
    assert vt.report_batch_failures(results, "测试") == 0


def test_empty_transcript_fails_with_flag(tmp_path):
    import vt

    results = [{"status": "empty", "title": "b", "vid": "2", "url": "u"}]
    assert vt.report_batch_failures(results, "测试", fail_on_empty=True) == 1


def test_fail_on_empty_is_tristate():
    import vt

    args = vt.build_parser().parse_args(["BV1"])
    assert vt.build_config(args)["fail_on_empty"] is False

    args = vt.build_parser().parse_args(["BV1", "--fail-on-empty"])
    assert vt.build_config(args)["fail_on_empty"] is True


# ---------------------------------------------------------------------------
# 问题 8：LF 行尾
# ---------------------------------------------------------------------------

def test_write_txt_uses_lf(tmp_path):
    import vt

    path = tmp_path / "a.txt"
    vt.write_txt(path, "第一行\n第二行")
    raw = path.read_bytes()
    assert b"\r\n" not in raw
    assert raw.endswith(b"\n")


# ---------------------------------------------------------------------------
# 问题 2：小红书熔断
# ---------------------------------------------------------------------------

def test_xhs_batch_aborts_after_consecutive_failures(tmp_path, monkeypatch):
    """连续失败到阈值就停，不再把剩下的笔记跑完。"""
    import vt
    import vt_xiaohongshu

    notes = [{"id": f"6900f1d{i:02x}" + "0" * 16, "title": f"n{i}", "url": f"https://x/{i}",
              "date": ""} for i in range(10)]
    monkeypatch.setattr(vt_xiaohongshu, "iter_user_notes", lambda *a, **k: notes)
    monkeypatch.setattr(vt_xiaohongshu, "fetch_meta",
                        lambda *a, **k: {"author": "作者", "vid": "x", "title": "t", "date": ""})

    processed: list[str] = []

    def boom(*args, **kwargs):
        raise RuntimeError("SECURITY_BLOCK")

    monkeypatch.setattr(vt, "handle_item", lambda **kw: (processed.append(kw["vid"]), boom())[1])

    cfg = _make_cfg(tmp_path, max_consecutive_failures=3)
    args = vt.build_parser().parse_args(["--user", "abc123"])
    failed = vt.run_xhs_user(cfg, None, tmp_path, "abc123", args, None)

    assert failed == 3
    assert len(processed) == 3, f"应该只处理 3 条就熔断，实际 {len(processed)} 条"


def test_xhs_batch_zero_disables_breaker(tmp_path, monkeypatch):
    import vt
    import vt_xiaohongshu

    notes = [{"id": f"6900f1d{i:02x}" + "0" * 16, "title": f"n{i}", "url": f"https://x/{i}",
              "date": ""} for i in range(5)]
    monkeypatch.setattr(vt_xiaohongshu, "iter_user_notes", lambda *a, **k: notes)
    monkeypatch.setattr(vt_xiaohongshu, "fetch_meta",
                        lambda *a, **k: {"author": "作者", "vid": "x", "title": "t", "date": ""})
    monkeypatch.setattr(vt, "handle_item",
                        lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")))

    cfg = _make_cfg(tmp_path, max_consecutive_failures=0)
    args = vt.build_parser().parse_args(["--user", "abc123"])
    failed = vt.run_xhs_user(cfg, None, tmp_path, "abc123", args, None)
    assert failed == 5


def test_xhs_batch_resets_counter_on_success(tmp_path, monkeypatch):
    """中间成功一次就重置计数，不该误熔断。"""
    import vt
    import vt_xiaohongshu

    notes = [{"id": f"6900f1d{i:02x}" + "0" * 16, "title": f"n{i}", "url": f"https://x/{i}",
              "date": ""} for i in range(6)]
    monkeypatch.setattr(vt_xiaohongshu, "iter_user_notes", lambda *a, **k: notes)
    monkeypatch.setattr(vt_xiaohongshu, "fetch_meta",
                        lambda *a, **k: {"author": "作者", "vid": "x", "title": "t", "date": ""})

    seq = iter(["fail", "fail", "ok", "fail", "fail", "ok"])

    def fake_handle(**kw):
        if next(seq) == "ok":
            return {"status": "ok", "vid": kw["vid"], "title": "", "url": ""}
        raise RuntimeError("boom")

    monkeypatch.setattr(vt, "handle_item", fake_handle)

    cfg = _make_cfg(tmp_path, max_consecutive_failures=3)
    args = vt.build_parser().parse_args(["--user", "abc123"])
    failed = vt.run_xhs_user(cfg, None, tmp_path, "abc123", args, None)
    assert failed == 4, "两条失败、一条成功、两条失败、一条成功 → 全部跑完，4 条失败"


# ---------------------------------------------------------------------------
# 问题 4：小红书 --limit 取最新
# ---------------------------------------------------------------------------

def test_note_timestamp_decodes_id():
    import vt_xiaohongshu

    # 6900f1d3 → 2025-10-29（用户此前实测解出的日期）
    assert vt_xiaohongshu.note_timestamp("6900f1d30000000000000000") == int("6900f1d3", 16)
    assert vt_xiaohongshu.note_timestamp("short") == 0
    assert vt_xiaohongshu.note_timestamp("") == 0
    assert vt_xiaohongshu.note_timestamp("zzzzzzzzzz") == 0


def test_note_datetime_formats():
    import vt_xiaohongshu

    assert vt_xiaohongshu.note_datetime("6900f1d30000000000000000").startswith("2025-10-29")
    assert vt_xiaohongshu.note_datetime("bad") == ""


def test_xhs_limit_takes_newest(tmp_path, monkeypatch):
    """--limit 2 必须拿到 ID 时间戳最大的两条（此前是直接切前 2 条）。"""
    import vt
    import vt_xiaohongshu

    # 故意按「旧→新」之外的乱序给出
    ids = ["6900f1d3" + "0" * 16, "6800aaaa" + "0" * 16, "6a00bbbb" + "0" * 16]
    notes = [{"id": i, "title": i, "url": f"https://x/{i}", "date": ""} for i in ids]
    monkeypatch.setattr(vt_xiaohongshu, "iter_user_notes", lambda *a, **k: notes)
    monkeypatch.setattr(vt_xiaohongshu, "fetch_meta",
                        lambda *a, **k: {"author": "作者", "vid": "x", "title": "t", "date": ""})

    seen: list[str] = []
    monkeypatch.setattr(vt, "handle_item",
                        lambda **kw: (seen.append(kw["vid"]),
                                      {"status": "ok", "vid": kw["vid"]})[1])

    cfg = _make_cfg(tmp_path)
    args = vt.build_parser().parse_args(["--user", "abc123", "--limit", "2"])
    vt.run_xhs_user(cfg, None, tmp_path, "abc123", args, None)

    assert seen == ["6a00bbbb" + "0" * 16, "6900f1d3" + "0" * 16], "应按时间戳降序取最新两条"


# ---------------------------------------------------------------------------
# 问题 3：列表重试
# ---------------------------------------------------------------------------

def test_doctor_retries_transient_failure(monkeypatch, no_cleanup):
    import vt_common as C

    calls = {"n": 0}

    def fake_run(args, **_kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return FakeCompleted(0, '{"ok":false,"error":{"code":"Extension not connected"}}')
        return FakeCompleted(0, '{"ok":true,"data":{"daemonRunning":true,'
                                '"extensionConnected":true,"connectivity":{"ok":true}}}')

    monkeypatch.setattr(C, "run_opencli", fake_run)
    monkeypatch.setattr(C.time, "sleep", lambda *_: None)

    C.check_opencli({"opencli_retries": 2, "opencli_retry_base_delay": 0})
    assert calls["n"] == 2, "第一次失败后应重试一次"


def test_doctor_gives_up_after_retries(monkeypatch):
    import vt_common as C

    calls = {"n": 0}

    def fake_run(args, **_kw):
        calls["n"] += 1
        return FakeCompleted(0, '{"ok":true,"data":{"daemonRunning":false,'
                                '"extensionConnected":false,"connectivity":{"ok":false}}}')

    monkeypatch.setattr(C, "run_opencli", fake_run)
    monkeypatch.setattr(C.time, "sleep", lambda *_: None)

    with pytest.raises(RuntimeError, match="状态异常"):
        C.check_opencli({"opencli_retries": 2, "opencli_retry_base_delay": 0})
    assert calls["n"] == 3, "重试 2 次 = 一共跑 3 次"


def test_bilibili_list_retries(monkeypatch):
    import vt_bilibili

    calls = {"n": 0}

    def fake_json(args, **_kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("Pre-navigation to https://space.bilibili.com failed: Navigation rejected.")
        return [{"bvid": "BV1", "title": "t", "url": "u", "date": "2026-01-01"}]

    monkeypatch.setattr(vt_bilibili, "opencli_json_with_prenav_retry", fake_json)
    monkeypatch.setattr(vt_bilibili.time, "sleep", lambda *_: None)

    videos = vt_bilibili.iter_user_videos(
        "123", {"bilibili": {"page_size": 50, "max_pages": None},
                "opencli_retries": 3, "opencli_retry_base_delay": 0})
    assert len(videos) == 1
    assert calls["n"] == 2


def test_bilibili_list_raises_after_retries(monkeypatch):
    import vt_bilibili

    monkeypatch.setattr(vt_bilibili, "opencli_json_with_prenav_retry",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("Extension not connected")))
    monkeypatch.setattr(vt_bilibili.time, "sleep", lambda *_: None)

    with pytest.raises(RuntimeError, match="拉取用户投稿列表失败"):
        vt_bilibili.iter_user_videos(
            "123", {"bilibili": {"page_size": 50, "max_pages": None},
                    "opencli_retries": 2, "opencli_retry_base_delay": 0})


# ---------------------------------------------------------------------------
# 问题 6：B站线程池熔断 + 中断
# ---------------------------------------------------------------------------

def test_pool_breaker_stops_remaining(tmp_path):
    import vt

    cfg = _make_cfg(tmp_path, max_consecutive_failures=2)
    cfg["bilibili"] = {"workers": 1}
    jobs = [{"bvid": f"BV{i}", "title": f"v{i}", "url": "u"} for i in range(10)]

    def worker(job, progress):
        raise RuntimeError("boom")

    results = vt._run_pool(cfg, jobs, worker)
    assert len(results) == 2, f"连续失败 2 条就该熔断，实际跑了 {len(results)} 条"
    assert all(r["status"] == "failed" for r in results)


def test_pool_collects_failure_details(tmp_path):
    import vt

    cfg = _make_cfg(tmp_path, max_consecutive_failures=0)
    cfg["bilibili"] = {"workers": 1}
    jobs = [{"bvid": "BV1", "title": "标题", "url": "https://x/BV1"}]

    def worker(job, progress):
        raise RuntimeError("具体原因")

    results = vt._run_pool(cfg, jobs, worker)
    assert results[0]["title"] == "标题"
    assert results[0]["url"] == "https://x/BV1"
    assert results[0]["error"] == "具体原因"


# ---------------------------------------------------------------------------
# 问题 7：单条模式空结果退出码
# ---------------------------------------------------------------------------

def test_single_exit_code_respects_flag(tmp_path):
    import vt

    empty = {"status": "empty", "vid": "x"}
    ok = {"status": "ok", "vid": "x"}

    assert vt._single_exit_code({"fail_on_empty": False}, empty, "t", "u") == 0
    assert vt._single_exit_code({"fail_on_empty": True}, empty, "t", "u") == 1
    assert vt._single_exit_code({"fail_on_empty": True}, ok, "t", "u") == 0


# ---------------------------------------------------------------------------
# 参数接线
# ---------------------------------------------------------------------------

def test_max_consecutive_failures_cli():
    import vt

    args = vt.build_parser().parse_args(["BV1", "--max-consecutive-failures", "5"])
    assert vt.build_config(args)["max_consecutive_failures"] == 5

    args = vt.build_parser().parse_args(["BV1"])
    assert vt.build_config(args)["max_consecutive_failures"] == 3
