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


class Logbook:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("a", encoding="utf-8")
        self._n = 0
        self._start = time.monotonic()

    def write(self, event_type: str, **fields: Any) -> dict:
        self._n += 1
        event = {
            "n": self._n,
            "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "seconds_from_start": round(time.monotonic() - self._start, 1),
            "event_type": event_type,
        }
        event.update({d: zn for d, zn in fields.items() if zn is not None})
        self._file.write(json.dumps(event, ensure_ascii=False) + "\n")
        self._file.flush()
        return event

    def turn(
        self,
        *,
        round: int,
        mode: str,
        speaker: str,
        order_in_round: int | None,
        visibility: str,
        text: str,
        roll: dict | None = None,
        tags: list[str] | None = None,
        provider: str | None = None,
        model: str | None = None,
        latency_ms: int | None = None,
        tokens: dict | None = None,
        cost: float | None = None,
        replies_v_scene: int | None = None,
        context_chars: int | None = None,
    ) -> dict:
        return self.write(
            "ход",
            replies_v_scene=replies_v_scene,
            context_chars=context_chars,
            round=round,
            mode=mode,
            speaker=speaker,
            order_in_round=order_in_round,
            visibility=visibility,
            text=text,
            roll=roll,
            tags=tags or [],
            provider=provider,
            model=model,
            latency_ms=latency_ms,
            tokens=tokens,
            cost=cost,
        )

    def delivery(self, *, round: int, sender: str, to: str, chars: int) -> dict:
        return self.write(
            "доставка", round=round, sender=sender, to=to, chars=chars
        )

    def roll(self, *, round: int, roll: dict, tags: list[str] | None = None) -> dict:
        return self.write(
            "бросок", round=round, speaker="скрипт", visibility="всем",
            roll=roll, tags=tags or [],
        )

    def anomaly(self, *, round: int, who: str, tag: str, details: str = "") -> dict:
        return self.write(
            "аномалия", round=round, speaker=who, tags=[tag], text=details
        )

    def close(self) -> None:
        self._file.close()

    @property
    def elapsed_seconds(self) -> float:
        return time.monotonic() - self._start


def read(path: str | Path) -> list[dict]:
    """Читает JSONL целиком. Битые строки пропускает, но не молча."""
    events: list[dict] = []
    with Path(path).open(encoding="utf-8") as fn:
        for index, line in enumerate(fn, 1):
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError as error:
                print(f"! строка {index} лога не разобрана: {error}")
    return events
