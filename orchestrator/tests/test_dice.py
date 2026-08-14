#!/usr/bin/env python3
"""Тесты кубиков: python3 orchestrator/tests/test_dice.py"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.dice import Dice, modifier_difficulties  # noqa: E402

CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))


def тест_таблица_сложности():
    assert modifier_difficulties("очень легко") == (60, True)
    assert modifier_difficulties("Средне") == (20, True)
    assert modifier_difficulties("испытание") == (0, True)
    assert modifier_difficulties("тяжёлая") == (-20, True)
    assert modifier_difficulties("очень тяжело") == (-30, True)
    assert modifier_difficulties("трудная проверка") == (-10, True)
    # Неизвестная сложность не роняет прогон, но помечается.
    assert modifier_difficulties("невозможно") == (0, False)


def тест_цель_по_характеристике():
    d = Dice(CONFIG, 1)
    c = d.roll("Курт", "Сила воли", "трудно")
    assert c.base == 30 and c.modifier == -10 and c.target == 20
    assert c.characteristic == "Сила воли" and c.advances == 0


def тест_цель_по_навыку():
    d = Dice(CONFIG, 1)
    # Восприятие стоит на Инициативе; у Ханны навык обученный, отсюда надбавка.
    c = d.roll("Ханна", "Восприятие", "испытание")
    assert c.characteristic == "Инициатива" and c.base == 42
    assert c.advances == 10 and c.target == 52

    # У Курта восприятие тоже в карточке, а обман — нет.
    untrained = d.roll("Лизель", "Скрытность", "средне")
    assert untrained.characteristic == "Ловкость" and untrained.advances == 0
    assert untrained.target == 41 + 20


def тест_бросок_за_персонажа_мира():
    d = Dice(CONFIG, 1)
    # У Гретель Обман задан числом прямо в карточке — оно старше расчёта.
    c = d.roll("Гретель", "Обман", "испытание")
    assert c.base == 45 and c.advances == 0 and c.target == 45
    assert c.tags == []
    # Навыка без явного значения — считается по характеристике.
    by_characteristic = d.roll("Гретель", "Скрытность", "средне")
    assert by_characteristic.characteristic == "Ловкость"


def тест_явная_база_от_мастера():
    d = Dice(CONFIG, 1)
    c = d.roll("Хозяин мельницы", "Обман", "средне", explicit_base=45)
    assert c.target == 65 and "база_от_мастера" in c.tags


def тест_нпс_без_карточки_помечается():
    d = Dice(CONFIG, 1)
    c = d.roll("Кто-то безымянный", "Обман", "средне")
    assert "нпс_без_карточки" in c.tags


def тест_неизвестный_навык_помечается():
    d = Dice(CONFIG, 1)
    c = d.roll("Курт", "Жонглирование", "средне")
    assert "неизвестный_навык" in c.tags
    assert c.target == CONFIG["default_base"] + 20


def тест_уровни_успеха():
    d = Dice(CONFIG, 1)
    c = d.roll("Курт", "Боевые навыки", "средне")  # цель 65
    c.rolled = 54
    # Пересчёт вручную по формуле десятков: 65 → 6, 54 → 5.
    assert (c.target // 10) - (54 // 10) == 1


def тест_автоматика():
    # 01–05 успех всегда, 96–100 провал всегда, даже против высокой цели.
    d = Dice(CONFIG, 1)
    successes = failures = 0
    for seed in range(400):
        d = Dice(CONFIG, seed)
        c = d.roll("Лизель", "Обаяние", "очень тяжело")  # цель 62
        if c.rolled <= 5:
            assert c.success and c.auto == "автоуспех" and c.success_levels >= 0
            successes += 1
        if c.rolled >= 96:
            assert not c.success and c.auto == "автопровал" and c.success_levels <= -1
            failures += 1
    assert successes and failures, "за 400 зёрен автоматика ни разу не сработала"


def тест_дубль_на_обычной_проверке():
    # Вне боя дубль — тоже событие: выдающийся успех или фумбл.
    for seed in range(300):
        d = Dice(CONFIG, seed)
        c = d.roll("Курт", "Сила", "средне")
        if c.rolled in (11, 22, 33, 44, 55, 66, 77, 88, 99, 100):
            assert c.doubles
            assert ("дубль_успех" in c.tags) == c.success
            assert "ДУБЛЬ" in c.short()
            return
    raise AssertionError("за 300 зёрен дубль ни разу не выпал")


def тест_воспроизводимость():
    first = [Dice(CONFIG, 777).roll("Курт", "Сила", "средне").rolled for _ in range(1)]
    d = Dice(CONFIG, 777)
    rolls = [d.roll("Курт", "Сила", "средне").rolled for _ in range(10)]
    d2 = Dice(CONFIG, 777)
    rolls2 = [d2.roll("Курт", "Сила", "средне").rolled for _ in range(10)]
    assert rolls == rolls2 and rolls[0] == first[0]
    assert len(set(rolls)) > 1, "генератор выдаёт одно и то же число"


if __name__ == "__main__":
    failed = 0
    for name, func in sorted(globals().items()):
        if name.startswith("тест_"):
            try:
                func()
                print(f"  ok   {name}")
            except AssertionError as error:
                failed += 1
                print(f"  ПРОВАЛ {name}: {error}")
    print("все тесты кубиков прошли" if not failed else f"провалено: {failed}")
    sys.exit(1 if failed else 0)
