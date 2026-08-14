"""Слой провайдера. Оркестратор не знает, чья модель за персонажем.

Интерфейс узкий нарочно: обмен репликами и ничего больше. Второй прогон, где
за столом окажутся GPT, Gemini и Grok, требует ровно двух методов — открыть
сессию и отправить в неё текст.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass
class Reply:
    text: str
    provider: str
    model: str
    latency_ms: int
    tokens: dict | None = None
    cost: float | None = None
    model_actual: str | None = None   # что реально ответило, а не алиас из конфига
    tags: list[str] = field(default_factory=list)


@runtime_checkable
class Session(Protocol):
    """Один агент со своей историей. Историю держит либо провайдер, либо адаптер."""

    async def send(self, text: str) -> Reply: ...

    async def close(self) -> None: ...


@runtime_checkable
class Provider(Protocol):
    name: str

    async def open(self, agent: str, system_prompt: str, model: str) -> Session: ...

    async def shutdown(self) -> None: ...


_REGISTRY: dict[str, type] = {}


def register(name: str, cls: type) -> None:
    _REGISTRY[name] = cls


def create(name: str, **params) -> Provider:
    if name not in _REGISTRY:
        # Ленивая загрузка: claude тянет за собой SDK, сторонние — httpx,
        # заглушка — ничего. Сухой прогон не должен требовать ни того ни другого.
        if name == "claude":
            from . import claude as _  # noqa: F401
        elif name in ("stub", "заглушка"):
            from . import stub as _  # noqa: F401
        elif name == "openai":
            from . import openai as _  # noqa: F401
        elif name == "gemini":
            from . import gemini as _  # noqa: F401
        elif name == "xai":
            from . import xai as _  # noqa: F401
        elif name == "openrouter":
            from . import openrouter as _  # noqa: F401
    if name not in _REGISTRY:
        known = ", ".join(sorted(_REGISTRY)) or "ничего"
        raise ValueError(f"неизвестный провайдер {name!r}; зарегистрированы: {known}")
    return _REGISTRY[name](**params)
