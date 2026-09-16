"""Source protocol and registry."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Type

from ainews.config import Config, SourceConfig
from ainews.http import Fetcher
from ainews.models import Item
from ainews.store import Store

log = logging.getLogger(__name__)


@dataclass
class Context:
    """Shared services handed to every source."""

    config: Config
    fetcher: Fetcher
    store: Store


class Source(ABC):
    type_name: str = ""
    #: set on subclasses that need secrets, checked by `ainews check`
    required_options: tuple[str, ...] = ()

    def __init__(self, name: str, options: dict[str, Any], ctx: Context) -> None:
        self.name = name
        self.options = options
        self.ctx = ctx

    @abstractmethod
    def fetch(self, since: datetime) -> list[Item]:
        """Return candidate items published at or after `since`."""

    def missing_options(self) -> list[str]:
        return [k for k in self.required_options if not self.options.get(k)]

    # -- helpers for subclasses ----------------------------------------------

    @property
    def limit(self) -> int:
        return int(self.options.get("max_items", self.ctx.config.max_items_per_source))

    def log_result(self, items: list[Item]) -> list[Item]:
        log.info("%s [%s]: %d items", self.name, self.type_name, len(items))
        return items


_REGISTRY: dict[str, Type[Source]] = {}


def register(cls: Type[Source]) -> Type[Source]:
    if not cls.type_name:
        raise ValueError(f"{cls.__name__} must set type_name")
    _REGISTRY[cls.type_name] = cls
    return cls


def available_types() -> list[str]:
    return sorted(_REGISTRY)


def build_source(cfg: SourceConfig, ctx: Context) -> Source:
    try:
        cls = _REGISTRY[cfg.type]
    except KeyError:
        raise ValueError(
            f"Unknown source type {cfg.type!r}. Available: {', '.join(available_types())}"
        ) from None
    return cls(cfg.name, cfg.options, ctx)
