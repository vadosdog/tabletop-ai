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
import rubric_schema


def main() -> int:
    parsed = argparse.ArgumentParser(description="Выборка кругов для человека")
    parsed.add_argument("--log", required=True, nargs="+",
                        help="один лог или несколько подряд (продолженный прогон)")
    parsed.add_argument("--out", required=True)
    parsed.add_argument("--rounds", type=int, default=15)
    parsed.add_argument("--from-round", type=int, default=None,
                        help="с какого круга начать выборку (по умолчанию с середины)")
    args = parsed.parse_args()

    events = common.read_log(args.log)
    rounds = common.choose_rounds(events, args.rounds, args.from_round)
    names = common.characters(events)

    folder = Path(args.out)
    folder.mkdir(parents=True, exist_ok=True)

    (folder / "круги.json").write_text(
        json.dumps(rounds, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (folder / "транскрипт.md").write_text(
        common.transcript(events, blind=True, only_rounds=rounds), encoding="utf-8"
    )

    table = folder / "оценка.csv"
    with table.open("w", encoding="utf-8", newline="") as fn:
        writer = csv.writer(fn)
        writer.writerow(rubric_schema.CSV_COLUMNS)
        for name in names:
            for criterion in common.CRITERIA:
                writer.writerow([name, criterion, "", "", ""])

    print(f"круги выборки: {rounds}")
    print(f"транскрипт: {folder / 'транскрипт.md'}")
    print(f"заполни баллы в: {table}")
    print("судью по той же выборке: judge.py --rounds-file "
          f"{folder / 'круги.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
