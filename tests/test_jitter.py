"""离线验证抖动语义（2026-10-20 用户定义）。

规则：**按两次请求之间的全流程耗时判断要不要等**。
  全流程耗时 ≥ target → 不等，直接发
  全流程耗时 <  target → 等 target − 全流程耗时

全流程耗时 = 上一轮下载请求 + 抽音频 + ASR 上传 + 落盘，**不含上一轮的等待**。

全部用虚拟时钟，不发任何真实请求、不 sleep 真实时间。
"""

from __future__ import annotations

import pytest

import vt_common as C


class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.slept = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(round(float(seconds), 3))
        self.now += float(seconds)

    def advance(self, seconds):
        """模拟「下载 + 抽音频 + ASR」这段本地处理耗时。"""
        self.now += float(seconds)


@pytest.fixture
def clock(monkeypatch):
    c = FakeClock()
    monkeypatch.setattr(C.time, "time", c.time)
    monkeypatch.setattr(C.time, "sleep", c.sleep)
    return c


@pytest.fixture
def fixed_target(monkeypatch):
    """把 target 钉死成 100 秒，方便断言。"""
    monkeypatch.setattr(C.random, "uniform", lambda a, b: 100.0)
    monkeypatch.setattr(C.random, "choices", lambda *a, **k: [(60.0, 180.0)])
    return 100.0


def test_first_call_never_waits(clock, fixed_target):
    """第一次调用没有参照点，直接放行。"""
    j = C.Jitter(60, 180, 10, 110)
    assert j.wait() == 0.0
    assert clock.slept == []


def test_slow_pipeline_skips_wait(clock, fixed_target):
    """全流程耗时 150s ≥ target 100s → 不等待。"""
    j = C.Jitter(60, 180, 10, 110)
    j.wait()
    clock.advance(150)          # 下载 + 抽音频 + ASR
    assert j.wait() == 0.0
    assert clock.slept == []


def test_fast_pipeline_waits_exactly_the_gap(clock, fixed_target):
    """全流程耗时 40s < target 100s → 等 60s。"""
    j = C.Jitter(60, 180, 10, 110)
    j.wait()
    clock.advance(40)
    assert j.wait() == pytest.approx(60.0)
    assert clock.slept == [60.0]


def test_pipeline_equal_to_target_does_not_wait(clock, fixed_target):
    """刚好等于 target 就不等（判定是 >=）。"""
    j = C.Jitter(60, 180, 10, 110)
    j.wait()
    clock.advance(100)
    assert j.wait() == 0.0
    assert clock.slept == []


def test_wait_does_not_credit_the_next_call(clock, fixed_target):
    """关键回归：这一次等过的时长，不能替下一次抵扣。

    上一版把 _last 记在 sleep 之前，elapsed 里混进了上次的等待，
    于是「处理慢就不等」会退化成「永远不等」。这里钉死这一点。
    """
    j = C.Jitter(60, 180, 10, 110)
    j.wait()                     # 第一次，放行
    clock.advance(40)            # 处理 40s
    assert j.wait() == pytest.approx(60.0)   # 等 60s 补足 100s
    clock.advance(10)            # 再处理 10s
    # elapsed 只算这 10s（等待的 60s 不算），所以要等 90s，而不是 30s
    assert j.wait() == pytest.approx(90.0)
    assert clock.slept == [60.0, 90.0]


def test_last_marks_when_request_was_actually_sent(clock, fixed_target):
    """_last 记的是请求真正发出的时刻（sleep 之后）。"""
    j = C.Jitter(60, 180, 10, 110)
    j.wait()
    sent_at = clock.now
    assert j._last == pytest.approx(sent_at)  # noqa: SLF001

    clock.advance(30)
    j.wait()                                  # 等 70s
    assert clock.slept == [70.0]
    # 等待结束后 _last 应当推进到「请求发出的那一刻」，即 30+70 之后
    assert j._last == pytest.approx(sent_at + 30 + 70)  # noqa: SLF001


def test_guarantee_bucket_uses_guarantee_min(clock, monkeypatch):
    """每第 10 次调用走保底档，target 从 [guarantee_min, max] 抽。"""
    seen = []

    def fake_uniform(a, b):
        seen.append((a, b))
        return (a + b) / 2

    monkeypatch.setattr(C.random, "uniform", fake_uniform)
    monkeypatch.setattr(C.random, "choices", lambda *a, **k: [(60.0, 180.0)])

    j = C.Jitter(60, 180, 10, 110)
    for _ in range(10):
        j.wait()
        clock.advance(500)   # 每次都远超 target，不产生真实等待

    assert seen[0] == (60.0, 180.0)     # 普通档
    assert seen[-1] == (110.0, 180.0)   # 第 10 次保底档


def test_disabled_jitter_returns_none(monkeypatch):
    """jitter.enabled=false 时 make_jitter 返回 None，调用点据此跳过等待。"""
    cfg = C.deep_merge({}, C.DEFAULTS)
    cfg["jitter"]["enabled"] = False
    assert C.make_jitter(cfg) is None

    cfg["jitter"]["enabled"] = True
    assert isinstance(C.make_jitter(cfg), C.Jitter)
