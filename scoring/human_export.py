#!/usr/bin/env python3
"""Выборка для ручной оценки.

    python3 scoring/human_export.py --log runs/X/лог.jsonl --rounds 15 --out runs/X/выборка

Кладёт три файла: транскрипт выборки, список кругов (чтобы судья мог оценить
ровно те же круги) и пустую таблицу для оценок. Заполненную таблицу потом
скармливаем report.py.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common  # noqa: E402


def main() -> int:
    разбор = argparse.ArgumentParser(description="Выборка кругов для человека")
    разбор.add_argument("--log", required=True, nargs="+",
                        help="один лог или несколько подряд (продолженный прогон)")
    разбор.add_argument("--out", required=True)
    разбор.add_argument("--rounds", type=int, default=15)
    разбор.add_argument("--from-round", type=int, default=None,
                        help="с какого круга начать выборку (по умолчанию с середины)")
    аргументы = разбор.parse_args()

    события = common.прочитать_лог(аргументы.log)
    круги = common.выбрать_круги(события, аргументы.rounds, аргументы.from_round)
    имена = common.персонажи(события)

    папка = Path(аргументы.out)
    папка.mkdir(parents=True, exist_ok=True)

    (папка / "круги.json").write_text(
        json.dumps(круги, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (папка / "транскрипт.md").write_text(
        common.транскрипт(события, слепой=True, только_круги=круги), encoding="utf-8"
    )

    таблица = папка / "оценка.csv"
    with таблица.open("w", encoding="utf-8", newline="") as ф:
        писарь = csv.writer(ф)
        писарь.writerow(["персонаж", "критерий", "балл", "цитата", "заметка"])
        for имя in имена:
            for критерий in common.КРИТЕРИИ:
                писарь.writerow([имя, критерий, "", "", ""])

    print(f"круги выборки: {круги}")
    print(f"транскрипт: {папка / 'транскрипт.md'}")
    print(f"заполни баллы в: {таблица}")
    print("судью по той же выборке: judge.py --rounds-file "
          f"{папка / 'круги.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
