"""A station must not answer unlimited $CQ ATIS requests (outbound amplification)."""

import asyncio

import pytest

from openfsd_injector.config import (
    AppConfig,
    AtisPluginConfig,
    AuthConfig,
    StationConfig,
    VoiceConfig,
)
from openfsd_injector.plugins.atis import ReplyRateLimiter, StationRuntime

FAKE_PASSWORD = "fake-password-not-a-real-credential"


class FakeClient:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.connected = asyncio.Event()

    async def send(self, packet: str) -> None:
        self.sent.append(packet)


def make_runtime(tmp_path, limit: int = 2, window: float = 60.0) -> StationRuntime:
    app = AppConfig(
        auth=AuthConfig(cid=999999, password=FAKE_PASSWORD),
        atis=AtisPluginConfig(
            reply_rate_limit=limit,
            reply_rate_window_seconds=window,
            voice=VoiceConfig(cache_dir=str(tmp_path / "cache")),
        ),
    )
    station = StationConfig(
        icao="EGLL",
        name="Heathrow Information",
        callsign="EGLL_ATIS",
        frequency=128.080,
        lat=51.4775,
        lon=-0.4614,
    )
    runtime = StationRuntime(app, station)
    runtime.client = FakeClient()
    runtime.state.letter = "C"
    runtime.state.lines = ["HEATHROW INFORMATION C", "EGLL 031250Z 27008KT 9999"]
    return runtime


def test_limiter_allows_burst_then_blocks():
    now = [1000.0]
    limiter = ReplyRateLimiter(3, 60.0, clock=lambda: now[0])
    assert [limiter.allow("BAW123") for _ in range(4)] == [True, True, True, False]


def test_limiter_recovers_after_window():
    now = [1000.0]
    limiter = ReplyRateLimiter(1, 10.0, clock=lambda: now[0])
    assert limiter.allow("BAW123") is True
    assert limiter.allow("BAW123") is False
    now[0] += 10.0
    assert limiter.allow("BAW123") is True


def test_limiter_is_per_requester_and_case_insensitive():
    now = [1000.0]
    limiter = ReplyRateLimiter(1, 60.0, clock=lambda: now[0])
    assert limiter.allow("BAW123") is True
    assert limiter.allow("baw123") is False
    assert limiter.allow("EZY456") is True


def test_limiter_bounds_tracked_requesters():
    now = [1000.0]
    limiter = ReplyRateLimiter(1, 60.0, clock=lambda: now[0], max_tracked=8)
    for i in range(64):
        now[0] += 0.001
        assert limiter.allow(f"CS{i:03d}") is True
    assert len(limiter._hits) <= 8


@pytest.mark.asyncio
async def test_reply_atis_stops_after_limit(tmp_path):
    runtime = make_runtime(tmp_path, limit=2)
    for _ in range(5):
        await runtime._reply_atis("BAW123")
    # V line + one T per ATIS line + E line, twice — and nothing after that.
    packets_per_reply = len(runtime.state.lines) + 2
    assert len(runtime.client.sent) == 2 * packets_per_reply


@pytest.mark.asyncio
async def test_reply_atis_budget_is_per_requester(tmp_path):
    runtime = make_runtime(tmp_path, limit=1)
    packets_per_reply = len(runtime.state.lines) + 2
    await runtime._reply_atis("BAW123")
    await runtime._reply_atis("BAW123")
    assert len(runtime.client.sent) == packets_per_reply
    await runtime._reply_atis("EZY456")
    assert len(runtime.client.sent) == 2 * packets_per_reply


@pytest.mark.asyncio
async def test_incoming_query_flood_is_rate_limited(tmp_path):
    runtime = make_runtime(tmp_path, limit=2)
    for _ in range(20):
        await runtime._on_packet("$CQBAW123:EGLL_ATIS:ATIS")
    packets_per_reply = len(runtime.state.lines) + 2
    assert len(runtime.client.sent) == 2 * packets_per_reply
