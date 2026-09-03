"""Background station tasks must be supervised, not fire-and-forget."""

import asyncio

import pytest

from openfsd_injector.config import (
    AppConfig,
    AtisPluginConfig,
    AudioHttpConfig,
    AuthConfig,
    InjectorConfig,
    MissingCredentialError,
    StationConfig,
    VoiceConfig,
)
from openfsd_injector.injector import Injector
from openfsd_injector.plugins import atis as atis_module
from openfsd_injector.plugins.atis import AtisPlugin, StationRuntime, StationSupervisor
from openfsd_injector.plugins.base import Plugin

FAKE_PASSWORD = "fake-password-not-a-real-credential"


def make_app(tmp_path, reconnect: float = 0.01, stations: int = 1) -> AppConfig:
    return AppConfig(
        auth=AuthConfig(cid=999999, password=FAKE_PASSWORD),
        injector=InjectorConfig(reconnect_seconds=reconnect, position_interval_seconds=0.01),
        atis=AtisPluginConfig(
            refresh_seconds=3600,
            voice=VoiceConfig(cache_dir=str(tmp_path / "cache")),
            audio_http=AudioHttpConfig(enabled=False),
            stations=[
                StationConfig(
                    icao=f"EG{i:02d}",
                    callsign=f"EG{i:02d}_ATIS",
                    frequency=128.080,
                    lat=51.0,
                    lon=-0.5,
                )
                for i in range(stations)
            ],
        ),
    )


def station_of(app: AppConfig) -> StationConfig:
    return app.atis.stations[0]


class FakeRuntime:
    """Stands in for StationRuntime: records lifecycle, fails on demand."""

    started = 0
    stopped = 0
    failure: BaseException | None = ConnectionError("server closed connection")
    start_failure: BaseException | None = None
    start_event: asyncio.Event | None = None

    def __init__(self, app: AppConfig, station: StationConfig, catalog=None, **_kwargs) -> None:
        self.app = app
        self.station = station
        self.catalog = catalog

    async def start(self) -> None:
        type(self).started += 1
        if type(self).start_event is not None:
            type(self).start_event.set()
        if type(self).start_failure is not None:
            raise type(self).start_failure

    async def wait(self) -> None:
        if type(self).failure is not None:
            raise type(self).failure
        await asyncio.Event().wait()

    async def stop(self) -> None:
        type(self).stopped += 1


@pytest.fixture
def fake_runtime(monkeypatch):
    class Runtime(FakeRuntime):
        started = 0
        stopped = 0
        failure: BaseException | None = ConnectionError("server closed connection")
        start_failure: BaseException | None = None
        start_event = None

    monkeypatch.setattr(atis_module, "StationRuntime", Runtime)
    return Runtime


@pytest.mark.asyncio
async def test_station_runtime_wait_raises_task_failure(tmp_path):
    """A dead read/position/ATIS task must surface, not be swallowed."""
    app = make_app(tmp_path)
    runtime = StationRuntime(app, station_of(app))

    async def boom() -> None:
        raise ConnectionError("server closed connection")

    runtime._tasks = [asyncio.create_task(boom(), name="EG00_ATIS-read")]
    with pytest.raises(ConnectionError, match="server closed connection"):
        await runtime.wait()
    await runtime.stop()


@pytest.mark.asyncio
async def test_station_runtime_wait_reports_clean_exit(tmp_path):
    app = make_app(tmp_path)
    runtime = StationRuntime(app, station_of(app))

    async def finishes() -> None:
        return None

    runtime._tasks = [asyncio.create_task(finishes(), name="EG00_ATIS-pos")]
    with pytest.raises(ConnectionError, match="exited unexpectedly"):
        await runtime.wait()
    await runtime.stop()


@pytest.mark.asyncio
async def test_supervisor_restarts_station_after_drop(tmp_path, fake_runtime):
    app = make_app(tmp_path)
    supervisor = StationSupervisor(app, station_of(app))
    supervisor.start()
    try:
        async with asyncio.timeout(5):
            while supervisor.restarts < 3:
                await asyncio.sleep(0.01)
    finally:
        await supervisor.stop()
    assert fake_runtime.started >= 3
    assert fake_runtime.stopped >= 3
    assert supervisor.task is None


@pytest.mark.asyncio
async def test_supervisor_backoff_grows_and_is_capped(tmp_path, fake_runtime):
    """Restart delay must back off instead of hammering a dead server."""
    delays: list[float] = []

    async def recording_sleep(delay: float) -> None:
        delays.append(delay)
        await asyncio.sleep(0)

    app = make_app(tmp_path, reconnect=1.0)
    supervisor = StationSupervisor(app, station_of(app), sleep=recording_sleep)
    supervisor.start()
    try:
        async with asyncio.timeout(5):
            while len(delays) < 8:
                await asyncio.sleep(0)
    finally:
        await supervisor.stop()
    assert delays[:4] == [1.0, 2.0, 4.0, 8.0]
    assert max(delays) == 1.0 * atis_module.MAX_BACKOFF_MULTIPLIER


@pytest.mark.asyncio
async def test_supervisor_does_not_retry_credential_errors(tmp_path, fake_runtime):
    fake_runtime.start_failure = MissingCredentialError("no injector credential is configured")
    app = make_app(tmp_path)
    supervisor = StationSupervisor(app, station_of(app))
    supervisor.start()
    with pytest.raises(MissingCredentialError):
        async with asyncio.timeout(5):
            await supervisor.wait()
    assert fake_runtime.started == 1
    await supervisor.stop()


@pytest.mark.asyncio
async def test_plugin_wait_propagates_station_failure(tmp_path, fake_runtime):
    fake_runtime.start_failure = MissingCredentialError("no injector credential is configured")
    app = make_app(tmp_path, stations=2)
    plugin = AtisPlugin(app)
    await plugin.start()
    with pytest.raises(MissingCredentialError):
        async with asyncio.timeout(5):
            await plugin.wait()
    await plugin.stop()


@pytest.mark.asyncio
async def test_plugin_start_fails_fast_without_credential(tmp_path, fake_runtime):
    app = make_app(tmp_path)
    app.auth.password = ""
    app.auth.token = ""
    plugin = AtisPlugin(app)
    with pytest.raises(MissingCredentialError):
        await plugin.start()
    assert fake_runtime.started == 0


class ExplodingPlugin(Plugin):
    name = "exploding"

    def __init__(self, cfg: AppConfig) -> None:
        super().__init__(cfg)
        self.stopped = False

    async def start(self) -> None:
        return None

    async def wait(self) -> None:
        raise ConnectionError("all stations dead")

    async def stop(self) -> None:
        self.stopped = True


class IdlePlugin(Plugin):
    name = "idle"

    def __init__(self, cfg: AppConfig) -> None:
        super().__init__(cfg)
        self.stopped = False

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        self.stopped = True


@pytest.mark.asyncio
async def test_injector_supervise_propagates_plugin_failure(tmp_path):
    app = make_app(tmp_path)
    app.atis.enabled = False
    injector = Injector(app)
    plugin = ExplodingPlugin(app)
    injector.plugins = [plugin]
    with pytest.raises(ConnectionError, match="all stations dead"):
        async with asyncio.timeout(5):
            await injector._supervise(asyncio.Event())


@pytest.mark.asyncio
async def test_injector_supervise_returns_when_stopped(tmp_path):
    app = make_app(tmp_path)
    app.atis.enabled = False
    injector = Injector(app)
    injector.plugins = [IdlePlugin(app)]
    stop = asyncio.Event()
    task = asyncio.create_task(injector._supervise(stop))
    await asyncio.sleep(0)
    stop.set()
    async with asyncio.timeout(5):
        assert await task is None


@pytest.mark.asyncio
async def test_injector_run_stops_on_fatal_config_error(tmp_path, fake_runtime):
    """A missing credential must not be retried forever."""
    fake_runtime.start_failure = MissingCredentialError("no injector credential is configured")
    app = make_app(tmp_path)
    injector = Injector(app)
    with pytest.raises(MissingCredentialError):
        async with asyncio.timeout(5):
            await injector.run()
