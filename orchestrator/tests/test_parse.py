#!/usr/bin/env python3
"""Тесты парсера: python3 orchestrator/tests/test_parse.py"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src import parse  # noqa: E402

NAMES = ["Курт", "Ансельм", "Ханна", "Лизель"]


def тест_режим():
    assert parse.mode("текст\n\n[РЕЖИМ: РАЗГОВОР]") == ("РАЗГОВОР", True)
    assert parse.mode("текст\n[режим: действие]") == ("ДЕЙСТВИЕ", True)
    # Тега нет — умолчание РАЗГОВОР и признак пропуска: молчание мастера
    # толкуется в пользу диалога, на нём держится вся затея.
    assert parse.mode("просто сцена") == ("РАЗГОВОР", False)
    assert parse.mode("просто сцена", "ДЕЙСТВИЕ") == ("ДЕЙСТВИЕ", False)
    # Берётся последний тег, если мастер поставил два.
    assert parse.mode("[РЕЖИМ: ДЕЙСТВИЕ] ... [РЕЖИМ: РАЗГОВОР]")[0] == "РАЗГОВОР"


def тест_служебные_теги_вырезаются():
    clean = parse.without_control_tags("Сцена.\n\n[РЕЖИМ: РАЗГОВОР]\n\n[ФИНАЛ]")
    assert clean == "Сцена."


def тест_финал():
    assert parse.declared_finale("конец\n[ФИНАЛ]")
    assert not parse.declared_finale("это ещё не финал")


def тест_проверки():
    text = (
        "Ханна щурится в темноту.\n"
        "ПРОВЕРКА: Ханна, Восприятие, испытание\n"
        "**ПРОВЕРКА:** Курт, Сила воли, трудно"
    )
    checks, anomalies = parse.checks(text, NAMES)
    assert len(checks) == 2 and not anomalies
    assert (checks[0].character, checks[0].skill) == ("Ханна", "Восприятие")
    assert checks[1].difficulty == "трудно"


def тест_проверка_без_сложности():
    checks, anomalies = parse.checks("ПРОВЕРКА: Лизель, Обман", NAMES)
    assert checks[0].difficulty == "испытание"
    assert "сложность_не_указана" in anomalies


def тест_непарсимая_проверка():
    checks, anomalies = parse.checks("ПРОВЕРКА: тут всё непонятно", NAMES)
    assert not checks and "непарсимая_проверка" in anomalies


def тест_имя_с_фамилией_опознаётся():
    checks, _ = parse.checks("ПРОВЕРКА: Ханна Фогель, Скрытность, легко", NAMES)
    assert checks[0].character == "Ханна"


def тест_выбытие_и_возвращение():
    tx = "Курт валится навзничь.\n[ВЫБЫЛ: Курт, без сознания]"
    assert parse.down_events(tx, NAMES) == [("Курт", "без сознания")]
    # Причина необязательна, фамилия в имени не мешает.
    assert parse.down_events("[ВЫБЫЛ: Ханна Фогель]", NAMES) == [("Ханна", "не указана")]
    assert parse.comebacks("[ВЕРНУЛСЯ: Курт]", NAMES) == ["Курт"]
    assert parse.down_events("никто не выбыл", NAMES) == []


def тест_теги_выбытия_остаются_в_тексте():
    # Для игроков это событие сцены, а не служебная разметка.
    text = "Курта уводят.\n[ВЫБЫЛ: Курт, арест]\n\n[РЕЖИМ: РАЗГОВОР]"
    clean = parse.without_control_tags(text)
    assert "[ВЫБЫЛ: Курт, арест]" in clean and "[РЕЖИМ" not in clean


def тест_строки_атак_не_уходят_игрокам():
    # Иначе все увидят точный остаток ран друг друга, а это запрещено.
    text = ("Курт бьёт с разворота.\n"
             "АТАКА: Курт → Тварь, меч → попал, 4 ран, осталось 11 из 15.\n"
             "Тварь отшатывается.")
    clean = parse.without_lines_attacks(text)
    assert "АТАКА" not in clean and "осталось 11" not in clean
    assert "Курт бьёт с разворота." in clean and "Тварь отшатывается." in clean


def тест_проверка_за_нпс():
    # У персонажа мира нет карточки игрока — это не ошибка разбора.
    checks, anomalies = parse.checks("ПРОВЕРКА: Гретель, Обман, испытание", NAMES)
    assert checks[0].character == "Гретель" and checks[0].base is None
    assert anomalies == ["проверка_за_нпс"]


def тест_нпс_с_явным_числом():
    checks, _ = parse.checks("ПРОВЕРКА: Хозяин мельницы (45), Обман, средне", NAMES)
    assert checks[0].character == "Хозяин мельницы" and checks[0].base == 45


def тест_личные_блоки():
    text = (
        "Общая сцена, её видят все.\n\n"
        "ТОЛЬКО ДЛЯ Лизель\n"
        "Сержант сверяет бумаги по списку.\n\n"
        "ТОЛЬКО ДЛЯ Ханна\n"
        "Пёс скулит и пятится от лаза."
    )
    public, private = parse.split_private(text)
    assert public == "Общая сцена, её видят все."
    assert set(private) == {"Лизель", "Ханна"}
    assert "бумаги" in private["Лизель"] and "Пёс" in private["Ханна"]


def тест_без_личных_блоков():
    public, private = parse.split_private("Обычная сцена.")
    assert public == "Обычная сцена." and private == {}


def тест_тайный_ход():
    assert parse.secret_turn("ТАЙНО Лизель проверяет грамоту.")
    assert parse.secret_turn("  тайно: она отходит к двери")
    assert not parse.secret_turn("Лизель тайно проверяет грамоту.")


def тест_самоброски():
    assert parse.self_rolls("Курт кидает и выпало 42.")
    assert parse.self_rolls("Бросок вышел 77, она отводит глаза.")
    assert parse.self_rolls("Результат: 13, успех.")
    assert parse.self_rolls("уровни успеха 3")
    # Проза без чисел броска — чисто.
    assert not parse.self_rolls("Шесть человек на мосту, сержант нетерпелив.")
    assert not parse.self_rolls("Курт кидает мешок на телегу и садится рядом.")


def тест_самоброс_не_считает_выданные_числа():
    # Мастер вправе пересказать результат, который выдал скрипт.
    assert not parse.self_rolls("Выпало 37, грязь под ногтями", known={37})
    assert parse.self_rolls("Выпало 38, грязь под ногтями", known={37})


def тест_подпись_проверок():
    text = "ПРОВЕРКА: Ханна, Восприятие, испытание\nОна щурится."
    annotated = parse.annotate_checks(
        text, {"ПРОВЕРКА: Ханна, Восприятие, испытание": "выпало 12, цель 52, успех"}
    )
    assert "→ выпало 12, цель 52, успех" in annotated
    assert "Она щурится." in annotated


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
    print("все тесты парсера прошли" if not failed else f"провалено: {failed}")
    sys.exit(1 if failed else 0)
