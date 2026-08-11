"""Слой провайдера. Оркестратор не знает, чья модель за персонажем.

Интерфейс узкий нарочно: обмен репликами и ничего больше. Второй прогон, где
за столом окажутся GPT, Gemini и Grok, требует ровно двух методов — открыть
сессию и отправить в неё текст.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass
class Ответ:
    текст: str
    провайдер: str
    модель: str
    латентность_мс: int
    токены: dict | None = None
    стоимость: float | None = None
    модель_факт: str | None = None   # что реально ответило, а не алиас из конфига
    метки: list[str] = field(default_factory=list)


@runtime_checkable
class Сессия(Protocol):
    """Один агент со своей историей. Историю держит либо провайдер, либо адаптер."""

    async def отправить(self, текст: str) -> Ответ: ...

    async def закрыть(self) -> None: ...


@runtime_checkable
class Провайдер(Protocol):
    имя: str

    async def открыть(self, агент: str, системный_промпт: str, модель: str) -> Сессия: ...

    async def завершить(self) -> None: ...


_РЕЕСТР: dict[str, type] = {}


def зарегистрировать(имя: str, класс: type) -> None:
    _РЕЕСТР[имя] = класс


def создать(имя: str, **параметры) -> Провайдер:
    if имя not in _РЕЕСТР:
        # Ленивая загрузка: claude тянет за собой SDK, заглушка — нет.
        if имя == "claude":
            from . import claude as _  # noqa: F401
        elif имя in ("stub", "заглушка"):
            from . import stub as _  # noqa: F401
    if имя not in _РЕЕСТР:
        известные = ", ".join(sorted(_РЕЕСТР)) or "ничего"
        raise ValueError(f"неизвестный провайдер {имя!r}; зарегистрированы: {известные}")
    return _РЕЕСТР[имя](**параметры)
