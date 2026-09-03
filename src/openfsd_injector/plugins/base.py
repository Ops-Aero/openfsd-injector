from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod

from ..config import AppConfig


class Plugin(ABC):
    name: str = "plugin"

    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg

    @abstractmethod
    async def start(self) -> None: ...

    async def wait(self) -> None:
        """Block until the plugin fails, raising that failure.

        The default implementation has no background work to supervise, so it
        blocks until cancelled.
        """
        await asyncio.Event().wait()

    async def stop(self) -> None:
        return None
