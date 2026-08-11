#!/usr/bin/env python3
"""Судья: три независимых прохода по слепому логу.

    python3 scoring/judge.py --log runs/X/лог.jsonl --out runs/X/судья.json
    python3 scoring/judge.py --log ... --rounds-file runs/X/выборка/круги.json \
                             --out runs/X/судья-выборка.json

Каждый проход — отдельная сессия: судья не должен видеть своих прошлых оценок,
иначе три прохода превращаются в один с косметическими правками.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parent
sys.path.insert(0, str(КОРЕНЬ))
sys.path.insert(0, str(КОРЕНЬ.parent / "orchestrator"))

import common  # noqa: E402

from src.providers import base as провайдеры  # noqa: E402


async def один_проход(провайдер, модель: str, промпт: str, транскрипт: str, номер: int) -> dict:
    сессия = await провайдер.открыть(f"Судья-{номер}", промпт, модель)
    try:
        ответ = await сессия.отправить(
            транскрипт + "\n\nОцени всех четверых по рубрике. Верни только JSON."
        )
    finally:
        await сессия.закрыть()
    разбор = common.вынуть_json(ответ.текст)
    разбор["_проход"] = номер
    разбор["_модель"] = ответ.модель
    разбор["_латентность_мс"] = ответ.латентность_мс
    разбор["_стоимость"] = ответ.стоимость
    return разбор


def сверить_цитаты(проход: dict, события: list[dict]) -> dict:
    """Помечает каждый балл: цитата на месте, цитата не в том круге, цитаты нет.

    Балл без подтверждённой цитаты в медиану не пойдёт — так требует рубрика.
    """
    for имя, данные in проход.get("персонажи", {}).items():
        for критерий, оценка in данные.get("критерии", {}).items():
            статус = common.проверить_цитату(
                события, оценка.get("круг"), оценка.get("цитата", "")
            )
            оценка["_цитата"] = статус
            оценка["_засчитан"] = статус != "не найдена" and isinstance(
                оценка.get("балл"), int
            )
        for штраф in данные.get("штрафы", []):
            штраф["_цитата"] = common.проверить_цитату(
                события, штраф.get("круг"), штраф.get("цитата", "")
            )
            штраф["_засчитан"] = штраф["_цитата"] != "не найдена"
    return проход


async def судить(аргументы) -> dict:
    события = common.прочитать_лог(аргументы.log)

    только = None
    if аргументы.rounds_file:
        только = json.loads(Path(аргументы.rounds_file).read_text(encoding="utf-8"))
    elif аргументы.sample:
        только = common.выбрать_круги(события, аргументы.sample)

    текст = common.транскрипт(события, слепой=True, только_круги=только)
    промпт = Path(аргументы.rubric).read_text(encoding="utf-8")

    провайдер = провайдеры.создать(аргументы.provider)
    проходы = []
    try:
        for номер in range(1, аргументы.passes + 1):
            print(f"— проход {номер} из {аргументы.passes}…", flush=True)
            проход = await один_проход(провайдер, аргументы.model, промпт, текст, номер)
            проходы.append(сверить_цитаты(проход, события))
    finally:
        await провайдер.завершить()

    результат = {
        "лог": str(аргументы.log),
        "круги": только,
        "проходов": len(проходы),
        "модель_судьи": аргументы.model,
        "рубрика": Path(аргументы.rubric).name,
        "проходы": проходы,
    }
    Path(аргументы.out).parent.mkdir(parents=True, exist_ok=True)
    Path(аргументы.out).write_text(
        json.dumps(результат, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return результат


def main() -> int:
    разбор = argparse.ArgumentParser(description="Слепой судья по логу прогона")
    разбор.add_argument("--log", required=True, nargs="+",
                        help="один лог или несколько подряд (продолженный прогон)")
    разбор.add_argument("--out", required=True)
    разбор.add_argument("--passes", type=int, default=3)
    разбор.add_argument("--provider", default="claude")
    разбор.add_argument("--model", default="opus")
    разбор.add_argument("--sample", type=int, default=None,
                        help="судить только выборку из N кругов")
    разбор.add_argument("--rounds-file", default=None,
                        help="JSON со списком кругов выборки")
    разбор.add_argument("--rubric", default=str(КОРЕНЬ / "rubric.md"),
                        help="rubric.md для игроков или rubric-gm.md для мастера")
    аргументы = разбор.parse_args()

    результат = asyncio.run(судить(аргументы))
    засчитано = сумма = 0
    for проход in результат["проходы"]:
        for данные in проход.get("персонажи", {}).values():
            for оценка in данные.get("критерии", {}).values():
                сумма += 1
                засчитано += bool(оценка.get("_засчитан"))
    print(f"баллов с подтверждённой цитатой: {засчитано} из {сумма}")
    print(f"результат: {аргументы.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
