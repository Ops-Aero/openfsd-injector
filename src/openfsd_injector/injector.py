from __future__ import annotations

import asyncio
import logging
import signal

from .config import AppConfig
from .plugins.atis import AtisPlugin
from .plugins.base import Plugin

log = logging.getLogger(__name__)


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

        while not stop.is_set():
            try:
                await self._start_all()
                await stop.wait()
            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("injector session failed")
                await self._stop_all()
                if stop.is_set():
                    break
                delay = self.cfg.injector.reconnect_seconds
                log.info("reconnecting in %.1fs", delay)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=delay)
                except TimeoutError:
                    continue
        await self._stop_all()

    async def _start_all(self) -> None:
        log.info("starting %d plugin(s)", len(self.plugins))
        await asyncio.gather(*(p.start() for p in self.plugins))

    async def _stop_all(self) -> None:
        await asyncio.gather(*(p.stop() for p in self.plugins), return_exceptions=True)
