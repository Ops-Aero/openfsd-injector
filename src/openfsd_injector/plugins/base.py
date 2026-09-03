from __future__ import annotations

from abc import ABC, abstractmethod

from ..config import AppConfig


class Plugin(ABC):
    name: str = "plugin"

    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg

    @abstractmethod
    async def start(self) -> None: ...

    async def stop(self) -> None:
        return None
