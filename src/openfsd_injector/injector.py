from __future__ import annotations

import asyncio
import logging
import signal

from .config import AppConfig, ConfigError
from .plugins.atis import AtisPlugin
from .plugins.base import Plugin

log = logging.getLogger(__name__)

# Session backoff ceiling, as a multiple of injector.reconnect_seconds.
MAX_BACKOFF_MULTIPLIER = 12


class Injector:
    """Owns plugin lifecycles and reconnects the whole set if the server dies."""

    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg
        self.plugins: list[Plugin] = []
        if cfg.atis.enabled:
            self.plugins.append(AtisPlugin(cfg))

    async def run(self) -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:
                pass

        base_delay = self.cfg.injector.reconnect_seconds
        delay = base_delay
        while not stop.is_set():
            try:
                await self._start_all()
                delay = base_delay
                await self._supervise(stop)
                break
            except asyncio.CancelledError:
                break
            except ConfigError:
                # Configuration/credential problems are fatal, not retryable.
                log.error("configuration error — refusing to retry", exc_info=True)
                await self._stop_all()
                raise
            except Exception:
                log.exception("injector session failed")
                await self._stop_all()
                if stop.is_set():
                    break
                log.info("reconnecting in %.1fs", delay)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=delay)
                except TimeoutError:
                    delay = min(delay * 2, base_delay * MAX_BACKOFF_MULTIPLIER)
                    continue
        await self._stop_all()

    async def _supervise(self, stop: asyncio.Event) -> None:
        """Return when stopped; raise the first plugin failure instead if one dies."""
        stop_task = asyncio.create_task(stop.wait(), name="injector-stop")
        plugin_tasks = {
            asyncio.create_task(p.wait(), name=f"plugin-{p.name}"): p for p in self.plugins
        }
        watched = [stop_task, *plugin_tasks]
        try:
            done, _pending = await asyncio.wait(watched, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in watched:
                task.cancel()
            await asyncio.gather(*watched, return_exceptions=True)
        for task in done:
            if task is stop_task:
                continue
            exc = task.exception()
            if exc is not None:
                raise exc
            raise ConnectionError(f"plugin {plugin_tasks[task].name} stopped unexpectedly")

    async def _start_all(self) -> None:
        log.info("starting %d plugin(s)", len(self.plugins))
        await asyncio.gather(*(p.start() for p in self.plugins))

    async def _stop_all(self) -> None:
        await asyncio.gather(*(p.stop() for p in self.plugins), return_exceptions=True)
