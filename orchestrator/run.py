#!/usr/bin/env python3
"""Запуск прогона.

    python3 orchestrator/run.py --provider stub          # сухой прогон, без токенов
    python3 orchestrator/run.py --draw-control           # жребий: кто на слабой модели
    python3 orchestrator/run.py                          # живой прогон

Живой прогон идёт на подписке. Запускать из чистого шелла: ANTHROPIC_API_KEY
перебивает подписку и жжёт платные кредиты, ANTHROPIC_BASE_URL уводит запросы
не туда. Скрипт вычищает их сам, если не передан --keep-env.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parent
sys.path.insert(0, str(КОРЕНЬ))

from src.dice import Кубики  # noqa: E402
from src.logbook import Журнал  # noqa: E402
from src.combat import Боевка  # noqa: E402
from src.prompts import Промпты  # noqa: E402
from src.restore import восстановить  # noqa: E402
from src.providers import base as провайдеры  # noqa: E402
from src.session import Прогон  # noqa: E402

ОПАСНОЕ_ОКРУЖЕНИЕ = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")
МСК = timezone(timedelta(hours=3))


def метка_времени() -> str:
    """Дата и время старта по Москве — чтобы папки сортировались по порядку."""
    return datetime.now(МСК).strftime("%Y-%m-%d_%H:%M")


def почистить_окружение(оставить: bool) -> list[str]:
    вычищено = []
    for ключ in ОПАСНОЕ_ОКРУЖЕНИЕ:
        if os.environ.get(ключ):
            if оставить:
                print(f"! {ключ} оставлен в окружении по --keep-env")
            else:
                os.environ.pop(ключ)
                вычищено.append(ключ)
    if вычищено:
        print(f"— вычищено из окружения: {', '.join(вычищено)} (идём на подписке)")
    return вычищено


def жребий(конфиг: dict, путь: Path) -> str:
    """Кто сядет на заведомо слабую модель. Выбирает генератор, не автор."""
    rng = random.Random(конфиг["зерно"])
    выбранный = rng.choice(list(конфиг["базовый_порядок"]))
    конфиг["контрольный_игрок"] = выбранный
    конфиг["игроки"][выбранный]["модель"] = конфиг["контрольная_модель"]
    путь.write_text(json.dumps(конфиг, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"— жребий (зерно {конфиг['зерно']}): контрольный игрок {выбранный}, "
          f"модель {конфиг['контрольная_модель']}")
    return выбранный


async def прогнать(конфиг: dict, аргументы) -> Прогон:
    папка = Path(аргументы.out)
    папка.mkdir(parents=True, exist_ok=True)
    (папка / "config.json").write_text(
        json.dumps(конфиг, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    документ = (КОРЕНЬ / конфиг["документ"]).resolve()
    промпты = Промпты(документ, КОРЕНЬ)
    журнал = Журнал(папка / "лог.jsonl")
    кубики = Кубики(конфиг, конфиг["зерно"], конфиг.get("доля_дублей", 0.0))

    имена = {конфиг["мастер"]["провайдер"]} | {
        н["провайдер"] for н in конфиг["игроки"].values()
    }
    подключённые = {
        имя: провайдеры.создать(
            имя, предел_стоимости=конфиг.get("предел_стоимости_usd_на_агента")
        )
        for имя in имена
    }

    боевка = Боевка(конфиг, кубики, КОРЕНЬ / "криты.json")
    прогон = Прогон(конфиг, промпты, журнал, кубики, подключённые, боевка)
    try:
        await прогон.подготовить()
        if аргументы.указание:
            прогон.доставить(
                "Мастер", f"СЛУЖЕБНОЕ. {аргументы.указание}", "скрипт"
            )

        if аргументы.continue_from:
            прошлое = [json.loads(с) for с in
                       Path(аргументы.continue_from).read_text(encoding="utf-8").splitlines() if с.strip()]
            история, состояние = восстановить(прошлое, прогон.имена)
            состояние.источник = str(аргументы.continue_from)
            прогон.продолжить(история, состояние)
            print(f"— продолжаю с круга {состояние.последний_круг + 1}, режим "
                  f"{состояние.режим}, подано "
                  f"{sum(len(т) for т in история.values()) // 1000} тыс. символов истории")
            await прогон.играть(с_круга_ноль=False)
        elif аргументы.round_zero_only:
            try:
                await прогон.круг_ноль()
                прогон.остановка = "только круг ноль"
            finally:
                прогон._итоги()
        else:
            await прогон.играть()
    finally:
        await прогон.завершить()
        журнал.закрыть()
    return прогон


def main() -> int:
    разбор = argparse.ArgumentParser(description="Оркестратор прогона НРИ")
    разбор.add_argument("--config", default=str(КОРЕНЬ / "config.json"))
    разбор.add_argument("--out", default=None, help="папка прогона")
    разбор.add_argument("--provider", default=None,
                        help="перебить провайдера у всех агентов, например stub")
    разбор.add_argument("--model", default=None, help="перебить модель у всех агентов")
    разбор.add_argument("--rounds", type=int, default=None)
    разбор.add_argument("--seed", type=int, default=None)
    разбор.add_argument("--round-zero-only", action="store_true",
                        help="остановиться после круга ноль и вводной сцены")
    разбор.add_argument("--continue-from", default=None,
                        help="лог прерванного прогона: продолжить с последнего круга")
    разбор.add_argument("--final-from", type=int, default=None,
                        help="с какого круга подталкивать мастера к развязке")
    разбор.add_argument("--hard-final-from", type=int, default=None,
                        help="с какого круга требовать развязку и тег [ФИНАЛ]")
    разбор.add_argument("--дубли", type=float, default=0.0,
                        help="ПРОВЕРОЧНЫЙ РЕЖИМ: доля подкрученных дублей, 0..1")
    разбор.add_argument("--указание", default=None,
                        help="разовое служебное указание мастеру в начале прогона")
    разбор.add_argument("--draw-control", action="store_true",
                        help="бросить жребий, записать контрольного игрока в конфиг и выйти")
    разбор.add_argument("--keep-env", action="store_true",
                        help="не вычищать ANTHROPIC_* из окружения")
    аргументы = разбор.parse_args()

    путь_конфига = Path(аргументы.config)
    конфиг = json.loads(путь_конфига.read_text(encoding="utf-8"))

    if аргументы.seed is not None:
        конфиг["зерно"] = аргументы.seed
    if аргументы.дубли:
        конфиг["доля_дублей"] = аргументы.дубли
        print(f"! ПОДКРУЧЕННЫЙ ГЕНЕРАТОР: дубли в {аргументы.дубли:.0%} бросков. "
              "Прогон проверочный, за настоящий его выдавать нельзя.")
    if аргументы.draw_control:
        жребий(конфиг, путь_конфига)
        return 0
    if аргументы.rounds is not None:
        конфиг["предел_кругов"] = аргументы.rounds
    if аргументы.final_from is not None:
        конфиг["финал_с_круга"] = аргументы.final_from
    if аргументы.hard_final_from is not None:
        конфиг["жёсткий_финал_с_круга"] = аргументы.hard_final_from
    if аргументы.provider:
        конфиг["мастер"]["провайдер"] = аргументы.provider
        for настройки in конфиг["игроки"].values():
            настройки["провайдер"] = аргументы.provider
    if аргументы.model:
        конфиг["мастер"]["модель"] = аргументы.model
        for настройки in конфиг["игроки"].values():
            настройки["модель"] = аргументы.model

    сухой = (аргументы.provider or "").startswith(("stub", "заглуш"))
    if not сухой:
        почистить_окружение(аргументы.keep_env)
        if конфиг.get("контрольный_игрок") is None:
            print("! контрольный игрок не назначен — сначала "
                  "`python3 orchestrator/run.py --draw-control`")
            return 2

    if аргументы.out is None:
        аргументы.out = str(КОРЕНЬ.parent / "runs" /
                            f"{метка_времени()}_{'сухой' if сухой else 'прогон'}")
    else:
        # Имя дали руками — всё равно ставим дату и время впереди.
        путь = Path(аргументы.out)
        if not re.match(r"\d{4}-\d{2}-\d{2}_\d{2}:\d{2}_", путь.name):
            аргументы.out = str(путь.with_name(f"{метка_времени()}_{путь.name}"))

    прогон = asyncio.run(прогнать(конфиг, аргументы))

    print()
    print(f"кругов: {прогон.круг}, остановка: {прогон.остановка}")
    print(f"бросков: {прогон.кубики.бросков}, зерно: {прогон.кубики.зерно}"
          + (f", подкручено {прогон.кубики.подкручено}"
             if прогон.кубики.доля_дублей else ""))
    print(f"токенов: {прогон.токенов}, стоимость: ${прогон.стоимость:.4f}")
    for имя, данные in прогон.счётчики.items():
        print(f"  {имя:8} ходов {данные['ходов']:3}  тайных {данные['тайных']:2}  "
              f"самобросков {данные['самобросков']:2}")
    print(f"лог: {Path(аргументы.out) / 'лог.jsonl'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
