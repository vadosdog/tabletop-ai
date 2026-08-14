#!/usr/bin/env python3
"""Тесты боевки: python3 orchestrator/tests/test_combat.py

Броски здесь подставные и заданы по шагам — иначе половина веток (крит, ноль
ран, смерть) выпадала бы раз в сотню прогонов и не проверялась бы никогда.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.combat import Combat, bonus, doubles, location  # noqa: E402
from src.dice import Dice  # noqa: E402

CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
CRITS = ROOT / "crits.json"


class ScriptedDice(Dice):
    """Кидает то, что велено. Список кончился — дальше единицы."""

    def __init__(self, config, rolls):
        super().__init__(config, seed=0)
        self.queue = list(rolls)

    def fake(self) -> int:
        self._index += 1
        return self.queue.pop(0) if self.queue else 1


def combat(rolls) -> Combat:
    return Combat(CONFIG, ScriptedDice(CONFIG, rolls), CRITS)


# --- листы и формула ------------------------------------------------------

def тест_формула_ран():
    c = combat([])
    # Контрольные цифры из брифа: если не сходится — ошибка в реализации.
    assert [c.sheet(i).wounds_max for i in ("Ханна", "Ансельм", "Курт", "Лизель")] \
        == [12, 13, 15, 11]


def тест_размеры_и_талант():
    c = combat([])
    characteristics = {"Сила": 40, "Выносливость": 40, "Сила воли": 30}   # 4, 4, 3
    assert c.wounds_by_formula(characteristics, "средний", {}) == 15
    assert c.wounds_by_formula(characteristics, "мелкий", {}) == 11
    assert c.wounds_by_formula(characteristics, "крошечный", {}) == 4
    assert c.wounds_by_formula(characteristics, "крохотный", {}) == 1
    assert c.wounds_by_formula(characteristics, "большой", {}) == 30
    assert c.wounds_by_formula(characteristics, "огромный", {}) == 60
    assert c.wounds_by_formula(characteristics, "чудовищный", {}) == 120
    # Выносливый добавляет ТБ за уровень.
    assert c.wounds_by_formula(characteristics, "средний", {"выносливый": 2}) == 15 + 8


def тест_бонус_первая_цифра():
    assert (bonus(45), bonus(40), bonus(9), bonus(100)) == (4, 4, 0, 10)


def тест_локация_переворотом():
    assert location(43) == "правая рука"      # 43 → 34
    assert location(10) == "голова"           # 10 → 01
    assert location(9) == "правая нога"       # 09 → 90
    assert location(55) == "корпус"


def тест_дубли():
    assert [c for c in range(1, 101) if doubles(c)] == \
        [11, 22, 33, 44, 55, 66, 77, 88, 99, 100]


# --- атака ----------------------------------------------------------------

def тест_попадание_и_урон():
    # Курт (БН 45, СБ 4, меч 4) бьёт Тварь (БН 40, ТБ 4, броня 1 везде).
    # Атака 12 → УУ +3; защита 38 → УУ +1; разница +2.
    c = combat([12, 38])
    total = c.attack("Курт", "Тварь", "меч")
    assert total.hit and total.margin == 2
    assert total.location == "левая рука"          # 12 → 21
    assert total.damage_before_armour == 4 + 4 + 2        # СБ + рейтинг + разница
    assert total.defence == 4 + 1                   # ТБ цели + броня локации
    assert total.wounds_dealt == 5
    assert c.sheet("Тварь").wounds == 15 - 5


def тест_промах_при_равной_разнице():
    # Оба выбросили одинаково: разница ноль — атака не проходит.
    c = combat([32, 32])
    total = c.attack("Курт", "Тварь", "меч")
    assert not total.hit and total.margin == 0
    assert c.sheet("Тварь").wounds == c.sheet("Тварь").wounds_max


def тест_броня_гасит_урон():
    # Лизель (СБ 2, нож 2) по Сержанту (ТБ 4, броня 1 на руке).
    # Разница +1 → урон 5, защита 5 → ноль ран. Броски не дубли, иначе крит.
    c = combat([21, 50])
    total = c.attack("Лизель", "Сержант", "нож")
    assert total.hit and total.wounds_dealt == 0
    assert c.sheet("Сержант").wounds == c.sheet("Сержант").wounds_max


def тест_арбалет_без_бонуса_силы():
    c = combat([12, 90])
    total = c.attack("Курт", "Тварь", "арбалет")
    # Рейтинг 9 фиксированный, бонус Силы не прибавляется.
    assert total.damage_before_armour == 9 + total.margin


def тест_пробивающий_берёт_десятки():
    # Молот Ансельма (БН 36): вместо уровней успеха берётся цифра десятков.
    c = combat([30, 50])
    total = c.attack("Ансельм", "Тварь", "молот")
    assert total.hit and total.margin == 1
    assert total.damage_before_armour == 3 + 4 + 3        # СБ + рейтинг + десятки броска
    assert total.margin < 3, "проверка бессмысленна, если разница и так больше"


def тест_рубящий_портит_броню():
    before = None
    c = combat([12, 90])
    before = c.sheet("Курт").armour["левая рука"]
    total = c.attack("Тварь", "Курт", "когти")     # когти рубящие
    assert total.location == "левая рука"
    assert c.sheet("Курт").armour["левая рука"] == before - 1


# --- дубли, криты, ноль ран ------------------------------------------------

def тест_дубль_даёт_крит_при_полных_ранах():
    # Атака 22 — дубль и успех. Крит независимо от того, сколько ран у цели.
    c = combat([22, 90, 50])                      # третий бросок — по таблице критов
    total = c.attack("Курт", "Тварь", "меч")
    assert total.doubles and "дубль_крит" in total.tags
    assert total.crit and total.crit["location"] == "левая рука"   # 22 → 22
    assert c.sheet("Тварь").wounds_max > c.sheet("Тварь").wounds


def тест_дубль_без_попадания_не_даёт_крит():
    # Бросок удался, но защита была лучше: атака не прошла, калечить некого.
    c = combat([22, 11])
    total = c.attack("Ансельм", "Тварь", "молот")
    assert total.doubles and not total.hit
    assert total.crit is None and "дубль_без_последствий" in total.tags
    assert c.counters["crits"] == 0


def тест_проваленный_дубль_это_фумбл():
    c = combat([99, 10])
    total = c.attack("Лизель", "Тварь", "нож")
    assert total.doubles and "фумбл" in total.tags and not total.hit
    assert c.counters["fumbles"] == 1


def тест_сто_всегда_фумбл():
    c = combat([100, 90])
    total = c.attack("Курт", "Тварь", "меч")
    assert total.doubles and "фумбл" in total.tags and not total.hit


def тест_ноль_ран_не_смерть_а_крит():
    c = combat([12, 90, 30])
    target = c.sheet("Лизель")
    target.wounds = 0                                  # уже на пределе, но жива
    total = c.attack("Тварь", "Лизель", "когти")
    assert total.hit and total.crit, "урон по нулю ран обязан дать крит"
    assert not target.dead, "ноль ран — не смерть"


def тест_смерть_ждёт_решения_игрока():
    # Крит в голову с броском 100 по таблице — смерть, но Судьбу тратит игрок.
    c = combat([10, 90, 100])
    target = c.sheet("Лизель")
    target.wounds = 0
    total = c.attack("Тварь", "Лизель", "когти")
    assert total.location == "голова" and total.death
    assert total.awaiting_fate, "скрипт не смеет решать за игрока"
    assert not target.dead, "до ответа игрока персонаж не мёртв"

    message = c.apply_fate("Лизель", True)
    assert "Судьбу" in message and target.fate == 1
    assert target.unconscious and not target.dead


def тест_отказ_от_судьбы_убивает():
    c = combat([10, 90, 100])
    c.sheet("Лизель").wounds = 0
    c.attack("Тварь", "Лизель", "когти")
    c.apply_fate("Лизель", False)
    assert c.sheet("Лизель").dead and c.counters["deaths"] == 1


def тест_счётчик_критов_отрывает_конечность():
    c = combat([])
    target = c.sheet("Лизель")
    tb = target.tb
    # Перебор критов в одну руку сверх бонуса Выносливости — рука потеряна.
    for _ in range(tb + 1):
        c.dice.queue.extend([12, 99, 30])
        target.wounds = 0
        c.attack("Тварь", "Лизель", "когти")
    assert "левая рука" in target.lost_limbs


def тест_колющий_добивает_при_крите():
    CONFIG["weapons"]["пика"] = {"kind": "ближний", "рейтинг": 3, "qualities": ["колющий"]}
    c = combat([22, 90, 50])          # 22 — дубль, значит крит
    total = c.attack("Курт", "Тварь", "пика")
    assert total.crit and "колющий_насквозь" in total.tags
    CONFIG["weapons"].pop("пика")


# --- состояния и кровотечение ---------------------------------------------

def тест_кровотечение_на_нуле():
    c = combat([50])
    combatant = c.sheet("Ханна")
    combatant.wounds = 0
    combatant.conditions.append("кровотечение")
    assert c.bleeding() == ["Ханна"]
    total = c.check_bleeding("Ханна")
    assert total["success"] is (total["rolled"] <= total["target"])


def тест_провал_кровотечения_валит_без_сознания():
    c = combat([99])
    combatant = c.sheet("Ханна")
    combatant.wounds = 0
    combatant.conditions.append("кровотечение")
    c.check_bleeding("Ханна")
    assert combatant.unconscious


def тест_состояние_штрафует_проверку():
    c = combat([50, 50])
    target = c.sheet("Курт")
    without_conditions = c._roll(target, "Боевые навыки")[0]
    target.conditions.append("ослеплён")
    with_condition = c._roll(target, "Боевые навыки")[0]
    assert with_condition == without_conditions - 30


# --- видимость -------------------------------------------------------------

def тест_игрок_видит_только_своё():
    c = combat([12, 90])
    c.attack("Тварь", "Курт", "когти")
    own = c.summary_player("Курт")
    assert "Раны" in own, own
    assert "Тварь" not in own, "игрок увидел, кто его ударил"
    gm = c.summary_gm()
    assert "Курт" in gm


def тест_строка_состояния_показывает_все_четыре_ресурса():
    """Она приходит каждый круг — на ней держится вся правка про ресурсы."""
    c = combat([12, 90])
    line = c.line_conditions("Курт")
    for word in ("Раны", "Судьба", "Удача", "Стойкость", "Решимость", "Состояний"):
        assert word in line, f"в строке состояния нет «{word}»: {line}"


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
    print("боевка в порядке" if not failed else f"провалено: {failed}")
    sys.exit(1 if failed else 0)
