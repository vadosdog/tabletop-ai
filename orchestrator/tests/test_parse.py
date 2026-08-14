#!/usr/bin/env python3
"""Тесты парсера: python3 orchestrator/tests/test_parse.py"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src import parse  # noqa: E402

NAMES = ["Курт", "Ансельм", "Ханна", "Лизель"]


def test_mode_tag():
    assert parse.mode("текст\n\n[РЕЖИМ: РАЗГОВОР]") == ("РАЗГОВОР", True)
    assert parse.mode("текст\n[режим: действие]") == ("ДЕЙСТВИЕ", True)
    # Тега нет — умолчание РАЗГОВОР и признак пропуска: молчание мастера
    # толкуется в пользу диалога, на нём держится вся затея.
    assert parse.mode("просто сцена") == ("РАЗГОВОР", False)
    assert parse.mode("просто сцена", "ДЕЙСТВИЕ") == ("ДЕЙСТВИЕ", False)
    # Берётся последний тег, если мастер поставил два.
    assert parse.mode("[РЕЖИМ: ДЕЙСТВИЕ] ... [РЕЖИМ: РАЗГОВОР]")[0] == "РАЗГОВОР"


def test_control_tags_are_stripped():
    clean = parse.without_control_tags("Сцена.\n\n[РЕЖИМ: РАЗГОВОР]\n\n[ФИНАЛ]")
    assert clean == "Сцена."


def test_finale_tag():
    assert parse.declared_finale("конец\n[ФИНАЛ]")
    assert not parse.declared_finale("это ещё не финал")


def test_check_lines():
    text = (
        "Ханна щурится в темноту.\n"
        "ПРОВЕРКА: Ханна, Восприятие, испытание\n"
        "**ПРОВЕРКА:** Курт, Сила воли, трудно"
    )
    checks, anomalies = parse.checks(text, NAMES)
    assert len(checks) == 2 and not anomalies
    assert (checks[0].character, checks[0].skill) == ("Ханна", "Восприятие")
    assert checks[1].difficulty == "трудно"


def test_check_without_difficulty():
    checks, anomalies = parse.checks("ПРОВЕРКА: Лизель, Обман", NAMES)
    assert checks[0].difficulty == "испытание"
    assert "сложность_не_указана" in anomalies


def test_unparsable_check():
    checks, anomalies = parse.checks("ПРОВЕРКА: тут всё непонятно", NAMES)
    assert not checks and "непарсимая_проверка" in anomalies


def test_name_with_surname_is_recognised():
    checks, _ = parse.checks("ПРОВЕРКА: Ханна Фогель, Скрытность, легко", NAMES)
    assert checks[0].character == "Ханна"


def test_going_down_and_comeback():
    tx = "Курт валится навзничь.\n[ВЫБЫЛ: Курт, без сознания]"
    assert parse.down_events(tx, NAMES) == [("Курт", "без сознания")]
    # Причина необязательна, фамилия в имени не мешает.
    assert parse.down_events("[ВЫБЫЛ: Ханна Фогель]", NAMES) == [("Ханна", "не указана")]
    assert parse.comebacks("[ВЕРНУЛСЯ: Курт]", NAMES) == ["Курт"]
    assert parse.down_events("никто не выбыл", NAMES) == []


def test_down_tags_stay_in_the_text():
    # Для игроков это событие сцены, а не служебная разметка.
    text = "Курта уводят.\n[ВЫБЫЛ: Курт, арест]\n\n[РЕЖИМ: РАЗГОВОР]"
    clean = parse.without_control_tags(text)
    assert "[ВЫБЫЛ: Курт, арест]" in clean and "[РЕЖИМ" not in clean


def test_attack_lines_never_reach_players():
    # Иначе все увидят точный остаток ран друг друга, а это запрещено.
    text = ("Курт бьёт с разворота.\n"
             "АТАКА: Курт → Тварь, меч → попал, 4 ран, осталось 11 из 15.\n"
             "Тварь отшатывается.")
    clean = parse.without_lines_attacks(text)
    assert "АТАКА" not in clean and "осталось 11" not in clean
    assert "Курт бьёт с разворота." in clean and "Тварь отшатывается." in clean


def test_check_rolled_for_an_npc():
    # У персонажа мира нет карточки игрока — это не ошибка разбора.
    checks, anomalies = parse.checks("ПРОВЕРКА: Гретель, Обман, испытание", NAMES)
    assert checks[0].character == "Гретель" and checks[0].base is None
    assert anomalies == ["проверка_за_нпс"]


def test_npc_with_explicit_target_number():
    checks, _ = parse.checks("ПРОВЕРКА: Хозяин мельницы (45), Обман, средне", NAMES)
    assert checks[0].character == "Хозяин мельницы" and checks[0].base == 45


def test_private_blocks():
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


def test_without_private_blocks():
    public, private = parse.split_private("Обычная сцена.")
    assert public == "Обычная сцена." and private == {}


def test_secret_turn():
    assert parse.secret_turn("ТАЙНО Лизель проверяет грамоту.")
    assert parse.secret_turn("  тайно: она отходит к двери")
    assert not parse.secret_turn("Лизель тайно проверяет грамоту.")


def test_self_rolls():
    assert parse.self_rolls("Курт кидает и выпало 42.")
    assert parse.self_rolls("Бросок вышел 77, она отводит глаза.")
    assert parse.self_rolls("Результат: 13, успех.")
    assert parse.self_rolls("уровни успеха 3")
    # Проза без чисел броска — чисто.
    assert not parse.self_rolls("Шесть человек на мосту, сержант нетерпелив.")
    assert not parse.self_rolls("Курт кидает мешок на телегу и садится рядом.")


def test_self_roll_ignores_numbers_we_issued():
    # Мастер вправе пересказать результат, который выдал скрипт.
    assert not parse.self_rolls("Выпало 37, грязь под ногтями", known={37})
    assert parse.self_rolls("Выпало 38, грязь под ногтями", known={37})


def test_checks_are_annotated():
    text = "ПРОВЕРКА: Ханна, Восприятие, испытание\nОна щурится."
    annotated = parse.annotate_checks(
        text, {"ПРОВЕРКА: Ханна, Восприятие, испытание": "выпало 12, цель 52, успех"}
    )
    assert "→ выпало 12, цель 52, успех" in annotated
    assert "Она щурится." in annotated


if __name__ == "__main__":
    failed = 0
    for name, func in sorted(globals().items()):
        if name.startswith("test_"):
            try:
                func()
                print(f"  ok   {name}")
            except AssertionError as error:
                failed += 1
                print(f"  ПРОВАЛ {name}: {error}")
    print("все тесты парсера прошли" if not failed else f"провалено: {failed}")
    sys.exit(1 if failed else 0)
