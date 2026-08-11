"""Лог прогона. JSONL, одна строка на событие, запись сразу на диск.

Из этого файла собираются без ручной работы: транскрипт для статьи, таблица
скоринга, список помеченных моментов для монтажа. Поэтому пишем сырьё, а не
уже сведённые цифры.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class Журнал:
    def __init__(self, путь: str | Path):
        self.путь = Path(путь)
        self.путь.parent.mkdir(parents=True, exist_ok=True)
        self._файл = self.путь.open("a", encoding="utf-8")
        self._н = 0
        self._старт = time.monotonic()

    def записать(self, тип_события: str, **поля: Any) -> dict:
        self._н += 1
        событие = {
            "н": self._н,
            "время": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "секунд_от_старта": round(time.monotonic() - self._старт, 1),
            "тип_события": тип_события,
        }
        событие.update({к: з for к, з in поля.items() if з is not None})
        self._файл.write(json.dumps(событие, ensure_ascii=False) + "\n")
        self._файл.flush()
        return событие

    def ход(
        self,
        *,
        круг: int,
        режим: str,
        говорящий: str,
        порядок_в_круге: int | None,
        видимость: str,
        текст: str,
        бросок: dict | None = None,
        метки: list[str] | None = None,
        провайдер: str | None = None,
        модель: str | None = None,
        латентность_мс: int | None = None,
        токены: dict | None = None,
        стоимость: float | None = None,
        ответов_в_сцене: int | None = None,
        контекст_символов: int | None = None,
    ) -> dict:
        return self.записать(
            "ход",
            ответов_в_сцене=ответов_в_сцене,
            контекст_символов=контекст_символов,
            круг=круг,
            режим=режим,
            говорящий=говорящий,
            порядок_в_круге=порядок_в_круге,
            видимость=видимость,
            текст=текст,
            бросок=бросок,
            метки=метки or [],
            провайдер=провайдер,
            модель=модель,
            латентность_мс=латентность_мс,
            токены=токены,
            стоимость=стоимость,
        )

    def доставка(self, *, круг: int, от_кого: str, кому: str, символов: int) -> dict:
        return self.записать(
            "доставка", круг=круг, от_кого=от_кого, кому=кому, символов=символов
        )

    def бросок(self, *, круг: int, бросок: dict, метки: list[str] | None = None) -> dict:
        return self.записать(
            "бросок", круг=круг, говорящий="скрипт", видимость="всем",
            бросок=бросок, метки=метки or [],
        )

    def аномалия(self, *, круг: int, кто: str, метка: str, подробности: str = "") -> dict:
        return self.записать(
            "аномалия", круг=круг, говорящий=кто, метки=[метка], текст=подробности
        )

    def закрыть(self) -> None:
        self._файл.close()

    @property
    def прошло_секунд(self) -> float:
        return time.monotonic() - self._старт


def прочитать(путь: str | Path) -> list[dict]:
    """Читает JSONL целиком. Битые строки пропускает, но не молча."""
    события: list[dict] = []
    with Path(путь).open(encoding="utf-8") as ф:
        for номер, строка in enumerate(ф, 1):
            строка = строка.strip()
            if not строка:
                continue
            try:
                события.append(json.loads(строка))
            except json.JSONDecodeError as ошибка:
                print(f"! строка {номер} лога не разобрана: {ошибка}")
    return события
