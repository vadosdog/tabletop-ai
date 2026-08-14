#!/usr/bin/env python3
"""Короткая сводка по логу прогона — для отправки в телеграм.

    python3 scripts/сводка-прогона.py runs/X/лог.jsonl | ./scripts/tg.sh

Пишет только то, что решает: сорвалось или нет, кого обрезало, кто отказался,
кто сколько стоил. Подробности остаются в логе и отчёте.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def summary(path: Path) -> str:
    events = [json.loads(s) for s in
               path.read_text(encoding="utf-8").splitlines() if s.strip()]
    total = next((ev for ev in events if ev["event_type"] == "итог"), None)
    turns = [ev for ev in events if ev["event_type"] == "ход"]
    lines: list[str] = []

    if total is None:
        lines.append("⚠️ ПРОГОН ОБОРВАЛСЯ: итога в логе нет")
        lines.append(f"успело пройти ходов: {len(turns)}")
        return "\n".join(lines)

    stop = total.get("stop") or "?"
    failed = stop.startswith("сбой")
    lines.append(f"{'❌' if failed else '✅'} {path.parent.name}")
    lines.append(f"кругов: {total.get('rounds')}, остановка: {stop}")
    lines.append(f"минут: {total.get('minutes')}, бросков: {total.get('rolls')}")

    truncations = {i: dt.get("truncations_by_limit") for i, dt in
              (total.get("provider_totals") or {}).items() if dt.get("truncations_by_limit")}
    refusals = total.get("refusals") or []

    lines.append("")
    if truncations:
        lines.append(f"⚠️ обрезаны по лимиту: {truncations}")
    else:
        lines.append("обрывов по лимиту нет")
    if refusals:
        lines.append(f"⚠️ отказов: {len(refusals)}")
        for about in refusals[:3]:
            lines.append(f"  круг {about.get('round')} {about.get('who')} "
                          f"({about.get('provider')}): {about.get('quote','')[:80]}")
    else:
        lines.append("отказов нет")

    lines.append("")
    lines.append("по провайдерам:")
    for name, dt in sorted((total.get("provider_totals") or {}).items()):
        cache = dt.get("share_cache")
        reas = dt.get("share_reasoning")
        lines.append(
            f"  {name}: ${dt.get('cost_usd', 0):.4f}, "
            f"кэш {'—' if cache is None else f'{cache:.0%}'}, "
            f"размышление {'—' if reas is None else f'{reas:.0%}'}, "
            f"повторов {dt.get('retries', 0)}"
        )
    lines.append(f"итого: ${total.get('cost_usd')}")

    # Апстрим прослойки: сменился посреди прогона — это важнее всего остального.
    marks = [ev for ev in events if ev["event_type"] == "апстрим"]
    if len(marks) > 1:
        lines.append("")
        lines.append("⚠️ АПСТРИМ МЕНЯЛСЯ: "
                      + " → ".join(str(about.get("upstream")) for about in marks))
    elif marks:
        lines.append(f"апстрим: {marks[0].get('upstream')} (не менялся)")

    return "\n".join(lines)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("нужен путь к лог.jsonl")
    print(summary(Path(sys.argv[1])))
