#!/usr/bin/env python3
"""Судьба, Удача, Стойкость, Решимость: python3 orchestrator/tests/test_ресурсы.py

Главное, что здесь проверяется, — разница между «сказал, что тратит» и
«потратил». На её отсутствии сгорели два прошлых прогона: Удачи в коде не
было вовсе, игроки её «тратили», а судья засчитывал это за работу с
механикой и ставил высший балл.

Второе по важности — правило связи потолков: запас Удачи равен текущей
Судьбе, запас Решимости равен текущей Стойкости. Если реализация даёт
другое, это ошибка, и бриф говорит об этом прямо.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.combat import Combat  # noqa: E402
from src.dice import Dice  # noqa: E402
from src.parse import (  # noqa: E402
    chosen_value, awards_resolve, corruption_language, spends,
)

CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
NAMES = list(CONFIG["base_order"])


def combat() -> Combat:
    return Combat(CONFIG, Dice(CONFIG, 1), ROOT / "crits.json")


def test_starting_values_and_linked_ceilings():
    c = combat()
    for name in NAMES:
        rp = c.resources(name)
        assert (rp["fate"], rp["fortune"]) == (2, 2), (name, rp)
        # Решимость пуста при потолке один: в третьем прогоне три начисления
        # мастера подряд сгорели, потому что копилка была полна с первого круга.
        assert (rp["resilience"], rp["resolve"]) == (1, 0), (name, rp)
        # Потолок восполняемого равен постоянному — это и есть правило связи.
        assert rp["fortune_max"] == rp["fate"], name
        assert rp["resolve_max"] == rp["resilience"], name


def test_first_resolve_award_applies():
    """Ради этого стартовую Решимость и обнулили."""
    c = combat()
    total = c.award_resolve("Ханна", 1)
    assert total["success"] and total["added"] == 1, total
    assert c.resources("Ханна")["resolve"] == 1


def test_fate_lets_the_player_choose_the_roll():
    """Второе применение Судьбы: в безнадёжном положении игрок берёт число сам."""
    c = combat()
    dice = Dice(CONFIG, 7)
    roll = dice.roll("Курт", "Ловкость", "трудно")
    taken = dice.substitute(roll, 1, "выбрано_за_судьбу")

    assert taken.rolled == 1
    assert taken.success, "01 обязан быть успехом"
    assert taken.auto == "автоуспех"
    assert "выбрано_за_судьбу" in taken.tags
    # Всё остальное от исходного броска сохранилось: цель, навык, номер.
    assert (taken.target, taken.skill) == (roll.target, roll.skill)

    total = c.spend_fate("Курт")
    assert total["success"]
    rp = c.resources("Курт")
    assert rp["fate"] == 1, "Судьба не списалась"
    assert rp["fortune_max"] == 1, "потолок Удачи не упал вслед за Судьбой"
    assert rp["fortune"] <= 1, "Удача осталась выше нового потолка"
    # Беспамятства быть не должно: это применение не про смерть.
    assert not c.sheet("Курт").unconscious


def test_substitution_recomputes_the_outcome():
    """Иначе в лог уйдёт красивая единица с чужим успехом."""
    dice = Dice(CONFIG, 11)
    roll = dice.roll("Лизель", "Обман", "трудно")
    for value, expect_success in ((1, True), (100, False), (96, False)):
        taken = dice.substitute(roll, value, "проба")
        assert taken.success is expect_success, (value, taken.success)
    assert dice.substitute(roll, 100, "проба").doubles, "100 обязан быть дублем"


def test_spending_fate_lowers_the_fortune_ceiling():
    c = combat()
    c.apply_fate("Курт", True)
    rp = c.resources("Курт")
    assert rp["fate"] == 1
    assert rp["fortune_max"] == 1, "потолок Удачи не опустился вслед за Судьбой"
    assert rp["fortune"] <= 1, "текущая Удача осталась выше нового потолка"


def test_spending_resilience_lowers_the_resolve_ceiling():
    c = combat()
    total = c.spend_resilience("Ансельм")
    assert total["success"]
    rp = c.resources("Ансельм")
    assert rp["resilience"] == 0
    assert rp["resolve_max"] == 0
    assert rp["resolve"] == 0, "Решимость осталась выше обнулённого потолка"


def test_cannot_spend_more_than_held():
    c = combat()
    # Решимость на старте пуста, поэтому сначала её надо заслужить.
    c.award_resolve("Ханна", 1)
    assert c.spend("Ханна", "resolve")["success"], "первая трата должна пройти"
    second = c.spend("Ханна", "resolve")
    assert not second["success"], "потрачено больше, чем было"
    assert "кончилась" in second["reason"]
    # Ресурс не ушёл в минус: отрицательный остаток врал бы в строке состояния.
    assert c.resources("Ханна")["resolve"] == 0


def test_resolve_never_rises_above_its_ceiling():
    c = combat()
    first = c.award_resolve("Лизель", 5)
    assert first["added"] == 1, "начислено больше, чем помещается в потолок"
    assert c.resources("Лизель")["resolve"] == 1

    full = c.award_resolve("Лизель", 1)
    assert not full["success"] and full["added"] == 0, "начислили сверх потолка"


def test_resolve_clears_everything_for_one_round():
    """Страх, боль, раны, штрафы — на круг не держит ничто.

    Раньше Решимость умела убирать только «сломлен» и «оглушён», и в третьем
    прогоне Курт потратил её впустую: снимать было нечего, ресурс списался
    за нулевой эффект.
    """
    c = combat()
    combatant = c.sheet("Курт")
    combatant.conditions = ["сломлен", "ослеплён", "кровотечение"]
    combatant.penalty = -30
    with_penalty = c._penalty_conditions(combatant)
    assert with_penalty < 0, "без иммунитета штрафы должны кусаться"

    c.give_immunity("Курт")
    assert c._penalty_conditions(combatant) == 0, "иммунитет не снял штрафы"
    # Состояния остаются: рука не срослась, её просто перестали замечать.
    assert set(combatant.conditions) == {"сломлен", "ослеплён", "кровотечение"}

    c.fresh_round()
    assert c._penalty_conditions(combatant) == with_penalty, "иммунитет не погас через круг"


def test_resolve_is_spent_even_with_nothing_to_clear():
    """Трата не должна пропадать впустую — это и был баг третьего прогона."""
    c = combat()
    assert not c.sheet("Ансельм").conditions
    c.award_resolve("Ансельм", 1)
    total = c.spend("Ансельм", "resolve")
    assert total["success"], "трата отклонена, хотя Решимость была"
    c.give_immunity("Ансельм")
    assert c.sheet("Ансельм").immunity_rounds == 1


def test_claim_is_recognised_in_any_case_form():
    assert spends("ТРАЧУ УДАЧУ и бью снова") == ["fortune"]
    assert spends("Трачу Решимость, стискиваю зубы") == ["resolve"]
    assert spends("трачу стойкость — иначе конец") == ["resilience"]
    # Дубли не множатся, порядок сохраняется.
    assert spends("Трачу удачу. Трачу решимость. Трачу удачу.") == ["fortune", "resolve"]


def test_talking_about_fortune_is_not_a_claim():
    """Иначе любое «повезло» спишет ресурс, которого игрок не тратил."""
    for text in (
        "Курту сегодня не хватает удачи",
        "«Судьба к нам недобра», — говорит Ансельм",
        "Он полагается на удачу, а не на молот",
    ):
        assert spends(text) == [], text


def test_resolve_tag_from_the_gm():
    entries = awards_resolve(
        "Сцена. [РЕШИМОСТЬ: Ханна, +1, вступилась за Йорга против патруля] Дальше.",
        NAMES)
    assert len(entries) == 1
    assert entries[0]["name"] == "Ханна"
    assert entries[0]["amount"] == 1
    assert "Йорг" in entries[0]["reason"]


def test_chosen_number_is_read_from_the_claim():
    assert chosen_value("ТРАЧУ СУДЬБУ, беру 01 — рука не дрогнет.") == 1
    assert chosen_value("Трачу судьбу и выбираю 5") == 5
    # Не назвал — берём лучшее: он и так заплатил постоянным ресурсом.
    assert chosen_value("ТРАЧУ СУДЬБУ. Курт делает невозможное.") == 1
    # «200» на сотне не бывает: число не распознаётся вовсе, и заявка падает
    # в умолчание. Трактовать молчание в пользу игрока честнее, чем угадывать.
    assert chosen_value("Трачу судьбу, беру 200") == 1


def test_language_corruption_catches_substituted_letters():
    """Латинская буква внутри русского слова — глазом не видно, а это порча."""
    clean = corruption_language("Ханна кивает и уходит в погреб.")
    assert not clean["corrupted"], clean

    substitution = corruption_language("Ханна кивaет и уходит в пoгреб.")
    assert substitution["corrupted"]
    assert len(substitution["mixed_words"]) == 2, substitution["mixed_words"]

    foreign = corruption_language("Ханна sparingly and quietly continues resting by the road.")
    assert foreign["corrupted"] and foreign["wholly_not_russian"]

    # Ложных срабатываний быть не должно: аббревиатура и одинокая латинская
    # буква — это не порча, а обычная запись.
    for harmless in ("Он показал пункт a в бумаге.", "Курт смотрит на HP."):
        assert not corruption_language(harmless)["corrupted"], harmless


def test_the_sergeant_was_given_fate():
    """Бриф: чтобы он мог пережить то, чего не должен был."""
    assert CONFIG["npcs"]["Сержант"]["fate"] == 1


def test_every_player_has_a_motivation():
    """На ней держится возврат Решимости — без неё начислять не за что."""
    c = combat()
    for name in NAMES:
        assert c.sheet(name).motivation, f"у {name} нет мотивации"


if __name__ == "__main__":
    failures = 0
    for name, func in sorted(globals().items()):
        if name.startswith("test_"):
            try:
                func()
                print(f"  ok   {name}")
            except AssertionError as error:
                failures += 1
                print(f"  ПЛОХО {name}: {error}")
    print("ресурсы в порядке" if not failures else f"провалов: {failures}")
    sys.exit(1 if failures else 0)
