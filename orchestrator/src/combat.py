"""Раны, урон и травмы. Считает скрипт, модели описывают.

Кубики у моделей уже отняты — модель кидает подозрительно удачно. Раны отняты
по той же причине: модель списывает их подозрительно драматично. Здесь вся
арифметика боя, и ни строчки её нет в промптах.

Цифры оружия, брони и таблица критов — наши, написаны под этот модуль. Из книги
не переносилось ничего.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path

from . import protocol
from .dice import Dice, normalise

# Названия локаций и размеров русские нарочно, переводу не подлежат: локация
# уходит в лог и в сцену мастера, а размер приходит ключом из карточки в
# config.json. Русский игровой протокол описан в protocol.py.
LOCATIONS = (
    (9, "голова"), (24, "левая рука"), (44, "правая рука"),
    (79, "корпус"), (89, "левая нога"), (100, "правая нога"),
)

FORMULAS_WOUNDS = {
    "средний": lambda sb, tb, wpb: sb + 2 * tb + wpb,
    "мелкий": lambda sb, tb, wpb: 2 * tb + wpb,
    "крошечный": lambda sb, tb, wpb: tb,
    "крохотный": lambda sb, tb, wpb: 1,
    "большой": lambda sb, tb, wpb: (sb + 2 * tb + wpb) * 2,
    "огромный": lambda sb, tb, wpb: (sb + 2 * tb + wpb) * 4,
    "чудовищный": lambda sb, tb, wpb: (sb + 2 * tb + wpb) * 8,
}


def bonus(value: int) -> int:
    """Бонус характеристики — её первая цифра."""
    return value // 10


def location(roll: int) -> str:
    """Место попадания: цифры броска атаки переворачиваются. 43 → 34."""
    reversed = int(f"{roll % 100:02d}"[::-1]) or 100
    for threshold, name in LOCATIONS:
        if reversed <= threshold:
            return name
    return "корпус"


def doubles(roll: int) -> bool:
    """11, 22, … 99 и 100. Сто — всегда провал, значит всегда фумбл."""
    if roll == 100:
        return True
    return roll % 11 == 0 and roll > 0


@dataclass
class Combatant:
    name: str
    characteristics: dict
    size: str = "средний"
    wounds_max: int = 0
    wounds: int = 0
    armour: dict = field(default_factory=dict)
    weapons: list = field(default_factory=list)
    conditions: list = field(default_factory=list)
    injuries: list = field(default_factory=list)
    crits_by_locations: dict = field(default_factory=dict)
    penalty: int = 0
    # Четыре ресурса, две пары. В каждой постоянный и восполняемый, и запас
    # восполняемого равен текущему значению постоянного: потратил Судьбу —
    # навсегда упал потолок Удачи. Потолки держим отдельными полями, потому что
    # текущее значение может быть ниже, а вот выше потолка — никогда.
    fate: int = 0
    fortune: int = 0
    fortune_max: int = 0
    resilience: int = 0
    resolve: int = 0
    resolve_max: int = 0
    motivation: str = ""
    # Круги, на которые Решимость сняла все эффекты разом. Пока счётчик не ноль,
    # состояния остаются в списке, но не кусаются: страх, боль, раны — ничего
    # не мешает. Что именно это оправдывает в сцене, решает мастер.
    immunity_rounds: int = 0
    unconscious: bool = False
    dead: bool = False
    lost_limbs: list = field(default_factory=list)

    @property
    def tb(self) -> int:
        return bonus(self.characteristics.get("Выносливость", 30))

    @property
    def sb(self) -> int:
        return bonus(self.characteristics.get("Сила", 30))

    def mapping(self) -> dict:
        return asdict(self)


@dataclass
class AttackResult:
    attacker: str
    target: str
    weapons: str
    roll_attacks: int = 0
    target_attacks: int = 0
    sl_attacks: int = 0
    roll_defence: int = 0
    target_defence: int = 0
    sl_defence: int = 0
    margin: int = 0
    hit: bool = False
    location: str = ""
    damage_before_armour: int = 0
    defence: int = 0
    wounds_dealt: int = 0
    wounds_left: int = 0
    doubles: bool = False
    crit: dict | None = None
    conditions: list = field(default_factory=list)
    injury: str | None = None
    death: bool = False
    awaiting_fate: bool = False
    tags: list = field(default_factory=list)

    def mapping(self) -> dict:
        return asdict(self)

    def as_line(self) -> str:
        """Сухой результат для мастера: он превращает его в сцену."""
        if not self.hit:
            reason = "промах" if self.sl_attacks < 0 else "защита выдержала"
            return f"{self.attacker} бьёт {self.target} ({self.weapons}) — {reason}."

        chunks = [f"{self.attacker} попал: {self.target}, {self.location}"]
        if self.wounds_dealt:
            chunks.append(f"{self.wounds_dealt} ран, осталось {self.wounds_left} "
                         f"из {self.wounds_max_targets}")
        else:
            chunks.append("броня и выносливость погасили весь урон")
        if self.crit:
            chunks.append(f"КРИТИЧЕСКОЕ РАНЕНИЕ: {self.crit['text']}")
        if self.conditions:
            chunks.append("состояния: " + ", ".join(self.conditions))
        if self.injury:
            chunks.append(f"травма: {self.injury}")
        if self.death:
            chunks.append("СМЕРТЬ" + (" — игрок решает про Судьбу" if self.awaiting_fate else ""))
        return ". ".join(chunks) + "."

    wounds_max_targets: int = 0


class Combat:
    """Листы всех участников и вся арифметика боя."""

    def __init__(self, config: dict, dice: Dice, path_crits: str | Path):
        self.config = config
        self.dice = dice
        table = json.loads(Path(path_crits).read_text(encoding="utf-8"))
        self.crits = table["locations"]
        self.healing = table["healing"]
        self.weapons = {normalise(i): dt for i, dt in config.get("weapons", {}).items()}
        self.conditions_reference = {
            normalise(i): dt for i, dt in config.get("conditions", {}).items()
        }
        self.combatants: dict[str, Combatant] = {}
        self.counters = {
            "attacks": 0, "hits": 0, "crits": 0, "fumbles": 0,
            "doubles": 0, "deaths": 0, "fate_spent": 0,
        }
        self.wounds_lost: dict[str, int] = {}
        self.reached_to_zero: set[str] = set()

        cards = {**config.get("characters", {}), **config.get("npcs", {})}
        for name in cards:
            self.sheet(name)

    # --- листы ------------------------------------------------------------

    def sheet(self, name: str) -> Combatant:
        """Лист бойца. Безымянному персонажу мира выдаётся заготовка."""
        if name in self.combatants:
            return self.combatants[name]

        cards = {**self.config.get("characters", {}),
                    **self.config.get("npcs", {})}
        card = cards.get(name)
        if card is None:
            base = self.config.get("default_base", 30)
            card = {
                "characteristics": {d: base for d in
                                   ("Боевые навыки", "Сила", "Выносливость",
                                    "Сила воли", "Ловкость", "Инициатива")},
                "size": "средний", "weapons": ["кулаки"], "armour": {},
            }

        characteristics = card["characteristics"]
        combatant = Combatant(
            name=name,
            characteristics=characteristics,
            size=card.get("size", "средний"),
            armour=dict(card.get("armour", {})),
            weapons=list(card.get("weapons", ["кулаки"])),
            fate=int(card.get("fate", 0)),
            resilience=int(card.get("resilience", 0)),
            motivation=card.get("motivation", ""),
        )
        # Потолки берутся от постоянных ресурсов, а не из карточки: если в
        # карточке написано иначе, это ошибка данных, и лучше её не размножать.
        combatant.fortune_max = combatant.fate
        combatant.fortune = min(int(card.get("fortune", combatant.fate)), combatant.fortune_max)
        combatant.resolve_max = combatant.resilience
        combatant.resolve = min(int(card.get("resolve", combatant.resilience)),
                             combatant.resolve_max)
        combatant.wounds_max = self.wounds_by_formula(characteristics, combatant.size,
                                              card.get("talents", {}))
        combatant.wounds = combatant.wounds_max
        self.combatants[name] = combatant
        return combatant

    def wounds_by_formula(self, characteristics: dict, size: str, talents: dict) -> int:
        """Раны считаются, а не задаются: при росте характеристик пересчитаются."""
        sb = bonus(characteristics.get("Сила", 30))
        tb = bonus(characteristics.get("Выносливость", 30))
        wpb = bonus(characteristics.get("Сила воли", 30))
        formula = FORMULAS_WOUNDS.get(normalise(size), FORMULAS_WOUNDS["средний"])
        total = formula(sb, tb, wpb)
        total += tb * int(talents.get("выносливый", 0))
        return max(total, 1)

    # --- проверки ---------------------------------------------------------

    def _penalty_conditions(self, combatant: Combatant) -> int:
        # Решимость снимает всё разом на круг: и штрафы от состояний, и общий
        # штраф от критов. Персонаж стиснул зубы — сегодня его ничто не держит.
        if combatant.immunity_rounds > 0:
            return 0
        penalty = combatant.penalty
        for state in combatant.conditions:
            help = self.conditions_reference.get(normalise(state), {})
            penalty += int(help.get("penalty", 0))
        return penalty

    def _roll(self, combatant: Combatant, skill: str) -> tuple[int, int, int]:
        """Бросок против характеристики со штрафами. Возвращает цель, бросок, УУ."""
        value = combatant.characteristics.get(skill)
        if value is None:
            value = combatant.characteristics.get("Боевые навыки", 30)
        target = max(value + self._penalty_conditions(combatant), 1)
        roll = self.dice.fake()
        sl = (target // 10) - (roll // 10)
        if roll <= 5:
            sl = max(sl, 0)
        elif roll >= 96:
            sl = min(sl, -1)
        return target, roll, sl

    # --- атака ------------------------------------------------------------

    def attack(self, who: str, target_name: str, weapon: str,
              defence: str | None = None) -> AttackResult:
        attacker, target = self.sheet(who), self.sheet(target_name)
        weapons = self.weapons.get(normalise(weapon), {"kind": "ближний", "рейтинг": 2,
                                                      "qualities": []})
        total = AttackResult(attacker=who, target=target_name, weapons=weapon)
        total.wounds_max_targets = target.wounds_max
        self.counters["attacks"] += 1

        skill_attacks = ("Стрелковые навыки"
                       if weapons["kind"] in ("механический", "лук")
                       and "Стрелковые навыки" in attacker.characteristics
                       else "Боевые навыки")
        total.target_attacks, total.roll_attacks, total.sl_attacks = self._roll(
            attacker, skill_attacks
        )
        skill_defence = "Ловкость" if normalise(defence or "") == "уклонение" \
            else "Боевые навыки"
        total.target_defence, total.roll_defence, total.sl_defence = self._roll(
            target, skill_defence
        )
        total.margin = total.sl_attacks - total.sl_defence

        total.doubles = doubles(total.roll_attacks)
        success_attacks = total.roll_attacks <= total.target_attacks and total.roll_attacks < 96
        if total.roll_attacks <= 5:
            success_attacks = True
        total.hit = success_attacks and total.margin > 0

        if total.doubles:
            self.counters["doubles"] += 1
            if not success_attacks:
                total.tags.append("фумбл")
                self.counters["fumbles"] += 1
            elif total.hit:
                total.tags.append("дубль_крит")
            else:
                # Бросок удался, но защита оказалась лучше: атака не прошла,
                # значит и критического ранения нет — калечить некого.
                total.tags.append("дубль_без_последствий")

        if not total.hit:
            return total
        self.counters["hits"] += 1

        # Урон: у луков и арбалетов рейтинг фиксированный, без бонуса Силы.
        gain = total.margin
        ones, tens = total.roll_attacks % 10, total.roll_attacks // 10
        qualities = [normalise(d) for d in weapons.get("qualities", [])]
        if "ранящее" in qualities:
            gain = max(gain, ones)
        if "пробивающий" in qualities:
            gain = max(gain, tens)

        base = weapons["рейтинг"] + (0 if weapons["kind"] in ("механический", "лук")
                                    else attacker.sb)
        total.damage_before_armour = base + gain
        total.location = location(total.roll_attacks)
        armour = int(target.armour.get(total.location, 0))
        total.defence = target.tb + armour
        total.wounds_dealt = max(total.damage_before_armour - total.defence, 0)

        if "рубящий" in qualities and armour > 0:
            target.armour[total.location] = armour - 1
            total.tags.append("броня_повреждена")

        # Ноль ран — не смерть. Но любой урон сверх нуля идёт в криты.
        before = target.wounds
        target.wounds = max(target.wounds - total.wounds_dealt, 0)
        total.wounds_left = target.wounds
        self.wounds_lost[target_name] = self.wounds_lost.get(target_name, 0) + (before - target.wounds)
        if target.wounds == 0 and before > 0:
            self.reached_to_zero.add(target_name)

        needs_crit = total.doubles and total.hit
        if before == 0 and total.wounds_dealt > 0:
            needs_crit = True
        if needs_crit:
            self._critical(total, target)
            if "колющий" in qualities:
                # Пробивает насквозь: добавляет урон единицами броска атаки.
                extra = ones
                target.wounds = max(target.wounds - extra, 0)
                total.wounds_dealt += extra
                total.wounds_left = target.wounds
                total.tags.append("колющий_насквозь")
                self.wounds_lost[target_name] = self.wounds_lost.get(target_name, 0) + extra
        return total

    # --- критические ранения ----------------------------------------------

    def _critical(self, total: AttackResult, target: Combatant) -> None:
        roll = self.dice.fake()
        table = self.crits.get(total.location, self.crits["корпус"])
        entry = next((zn for zn in table if roll <= zn["threshold"]), table[-1])

        self.counters["crits"] += 1
        total.crit = {"roll": roll, "text": entry["text"],
                     "location": total.location}
        target.crits_by_locations[total.location] = (
            target.crits_by_locations.get(total.location, 0) + 1
        )

        for state in entry.get("conditions", []):
            if state not in target.conditions:
                target.conditions.append(state)
                total.conditions.append(state)
        if entry.get("injury"):
            target.injuries.append({"kind": entry["injury"], "location": total.location,
                                "text": entry["text"], "healed": False})
            total.injury = entry["injury"]
        target.penalty += int(entry.get("penalty", 0))
        if entry.get("unconscious"):
            target.unconscious = True
            total.tags.append("без_сознания")
        if entry.get("потеря_конечности"):
            target.lost_limbs.append(total.location)
            total.tags.append("потеря_конечности")

        death = bool(entry.get("death"))
        # Счётчик критов по локации: перебор над бонусом Выносливости калечит.
        if target.crits_by_locations[total.location] > target.tb:
            if total.location in ("голова", "корпус"):
                death = True
            elif total.location not in target.lost_limbs:
                target.lost_limbs.append(total.location)
                target.injuries.append({"kind": "ампутация", "location": total.location,
                                    "text": f"{total.location} потеряна", "healed": False})
                total.injury = "ампутация"
                total.tags.append("потеря_конечности_по_счётчику")

        if death:
            total.death = True
            # Судьбу тратит только игрок и только своим словом.
            total.awaiting_fate = target.fate > 0

    def spend_fate(self, name: str) -> dict:
        """Списывает Судьбу навсегда и роняет потолок Удачи.

        Общая часть двух применений: спасения от смерти и выбора значения на
        кубах в безнадёжном положении. Беспамятство сюда не входит — оно
        относится только к первому.
        """
        combatant = self.sheet(name)
        before = self.resources(name)
        if combatant.fate <= 0:
            return {"success": False, "reason": "Судьба кончилась",
                    "before": before, "after": before}
        combatant.fate -= 1
        # Потолок Удачи падает вместе с Судьбой — навсегда. Если текущая Удача
        # была под потолок, она тоже съезжает: держать больше, чем можно
        # накопить, нельзя.
        combatant.fortune_max = combatant.fate
        combatant.fortune = min(combatant.fortune, combatant.fortune_max)
        self.counters["fate_spent"] += 1
        return {"success": True, "reason": "", "before": before,
                "after": self.resources(name)}

    def apply_fate(self, name: str, spends: bool) -> str:
        """Игрок сказал своё слово о смерти. Скрипт за него не решает."""
        combatant = self.sheet(name)
        if spends:
            total = self.spend_fate(name)
            if total["success"]:
                combatant.unconscious = True
                return (f"{name} тратит Судьбу: смерть заменена на беспамятство, "
                        f"осталось Судьбы {combatant.fate}, потолок Удачи теперь "
                        f"{combatant.fortune_max}")
        combatant.dead = True
        self.counters["deaths"] += 1
        return f"{name} умирает"

    # --- четыре ресурса ----------------------------------------------------

    def resources(self, name: str) -> dict:
        c = self.sheet(name)
        return {
            "fate": c.fate, "fortune": c.fortune, "fortune_max": c.fortune_max,
            "resilience": c.resilience, "resolve": c.resolve,
            "resolve_max": c.resolve_max,
        }

    def spend(self, name: str, resource: str) -> dict:
        """Списывает восполняемый ресурс. Не хватило — не списывает и говорит.

        Ход при этом не отклоняется: игрок мог просто ошибиться в счёте, и
        ронять из-за этого круг незачем. Но и эффекта не будет, а попытка
        уйдёт в лог и в сводку мастеру — иначе «трачу Удачу» без Удачи
        засчитается судьёй как настоящая трата, что уже однажды и вышло.
        """
        combatant = self.sheet(name)
        before = self.resources(name)
        field = resource if resource in ("fortune", "resolve") else None
        if field is None:
            return {"success": False, "reason": f"неизвестный ресурс {resource!r}",
                    "before": before, "after": before}

        remainder = getattr(combatant, field)
        if remainder <= 0:
            return {"success": False,
                    "reason": f"{protocol.RESOURCE_NAMES.get(resource, resource)} кончилась",
                    "before": before, "after": before}

        setattr(combatant, field, remainder - 1)
        self.counters[f"{resource}_spent"] = \
            self.counters.get(f"{resource}_spent", 0) + 1
        return {"success": True, "reason": "", "before": before,
                "after": self.resources(name)}

    def spend_resilience(self, name: str) -> dict:
        """Постоянный ресурс: тратится навсегда и роняет потолок Решимости."""
        combatant = self.sheet(name)
        before = self.resources(name)
        if combatant.resilience <= 0:
            return {"success": False, "reason": "Стойкость кончилась",
                    "before": before, "after": before}
        combatant.resilience -= 1
        combatant.resolve_max = combatant.resilience
        combatant.resolve = min(combatant.resolve, combatant.resolve_max)
        self.counters["resilience_spent"] = \
            self.counters.get("resilience_spent", 0) + 1
        return {"success": True, "reason": "", "before": before,
                "after": self.resources(name)}

    def award_resolve(self, name: str, amount: int = 1) -> dict:
        """Начисляет мастер за игру по Мотивации. Выше потолка не поднимает."""
        combatant = self.sheet(name)
        before = self.resources(name)
        fresh = min(combatant.resolve + amount, combatant.resolve_max)
        added = fresh - combatant.resolve
        combatant.resolve = fresh
        self.counters["resolve_awarded"] = \
            self.counters.get("resolve_awarded", 0) + added
        return {"success": added > 0, "added": added,
                "reason": "" if added else "Решимость уже под потолок",
                "before": before, "after": self.resources(name)}

    # --- состояния и кровотечение -----------------------------------------

    def give_immunity(self, name: str, rounds: int = 1) -> list[str]:
        """Решимость: на круг персонажа не держит ничто.

        Состояния из списка не убираем — они остались, просто перестали
        мешать. Так честнее для сцены: сломанная рука никуда не делась,
        её обладатель просто перестал её замечать.
        """
        combatant = self.sheet(name)
        combatant.immunity_rounds = max(combatant.immunity_rounds, rounds)
        return list(combatant.conditions)

    def fresh_round(self) -> None:
        """Гасит иммунитеты. Зовётся в начале круга, до любых бросков."""
        for combatant in self.combatants.values():
            if combatant.immunity_rounds > 0:
                combatant.immunity_rounds -= 1

    def clear_conditions(self, name: str, which: tuple[str, ...]) -> list[str]:
        """Снимает названные состояния. Штраф пересчитывать не надо.

        Он и так считается на лету в _штраф_состояний по текущему списку:
        убрали состояние — штраф ушёл вместе с ним.
        """
        combatant = self.sheet(name)
        targets = {normalise(s) for s in which}
        dealt = [s for s in combatant.conditions if normalise(s) in targets]
        combatant.conditions = [s for s in combatant.conditions if normalise(s) not in targets]
        return dealt

    def bleeding(self) -> list[str]:
        """Кто истекает кровью на нуле ран — этих проверяем каждый раунд."""
        return [name for name, c in self.combatants.items()
                if c.wounds == 0 and not c.dead
                and any(normalise(s) == "кровотечение" for s in c.conditions)]

    def check_bleeding(self, name: str) -> dict:
        combatant = self.sheet(name)
        target, roll, sl = self._roll(combatant, "Выносливость")
        success = roll <= target
        total = {"character": name, "target": target, "rolled": roll, "success": success}
        if not success:
            combatant.unconscious = True
            total["consequence"] = "потерял сознание, без помощи умрёт"
        else:
            total["consequence"] = "держится"
        return total

    def restore(self, name: str, amount: int = 1) -> tuple[int, str]:
        """Естественное восстановление ран. Невылеченный крит его запрещает."""
        combatant = self.sheet(name)
        unhealed = [tx for tx in combatant.injuries if not tx["healed"]]
        if unhealed:
            kinds = ", ".join(tx["kind"] for tx in unhealed)
            return 0, f"{name} не восстанавливается: невылеченное — {kinds}"
        before = combatant.wounds
        combatant.wounds = min(combatant.wounds + amount, combatant.wounds_max)
        return combatant.wounds - before, f"{name}: раны {combatant.wounds} из {combatant.wounds_max}"

    def heal(self, name: str, kind: str, success: bool) -> str:
        """Лечение по проверке Лечения. Тяжёлое требует хирургии и времени."""
        combatant = self.sheet(name)
        injury = next((tx for tx in combatant.injuries
                       if normalise(tx["kind"]) == normalise(kind)
                       and not tx["healed"]), None)
        if injury is None:
            return f"у {name} нет невылеченной травмы «{kind}»"
        help = self.healing.get(normalise(kind), {})
        if help.get("необратимо"):
            return f"{kind} необратима, лечить нечего"
        if not success:
            return f"{name}: лечение не помогло, {kind} остаётся"
        injury["healed"] = True
        tail = " (нужна хирургия и покой)" if help.get("хирургия") else ""
        return f"{name}: {kind} вылечена{tail}, дней покоя {help.get('дней', 0)}"

    def clear_state(self, name: str, state: str) -> bool:
        combatant = self.sheet(name)
        before = len(combatant.conditions)
        combatant.conditions = [s for s in combatant.conditions
                          if normalise(s) != normalise(state)]
        return len(combatant.conditions) != before

    # --- сводки -----------------------------------------------------------

    def summary_resources(self, names: list[str]) -> str:
        """Мастеру — остатки всех четверых каждый круг.

        Ему это нужно не для счёта, а чтобы видеть, кому пора начислить
        Решимость и у кого уже нечего тратить.
        """
        known = [i for i in names if i in self.combatants]
        if not known:
            return ""
        lines = ["РЕСУРСЫ ИГРОКОВ (считает скрипт, ты только начисляешь Решимость):"]
        for name in known:
            lines.append(f"- {name}: {self.line_conditions(name)}")
        return "\n".join(lines)

    def summary_gm(self, participants: list[str] | None = None) -> str:
        """Раны, состояния и травмы всех, кто в сцене. Мастеру — каждый круг."""
        names = participants or [i for i, c in self.combatants.items()
                              if c.wounds < c.wounds_max or c.conditions or c.injuries]
        if not names:
            return ""
        lines = ["СОСТОЯНИЕ УЧАСТНИКОВ (считает скрипт, ты только описываешь):"]
        for name in names:
            c = self.sheet(name)
            chunks = [f"{name}: раны {c.wounds} из {c.wounds_max}"]
            if c.conditions:
                chunks.append("состояния: " + ", ".join(c.conditions))
            if c.injuries:
                chunks.append("травмы: " + ", ".join(
                    tx["kind"] + (" (вылечена)" if tx["healed"] else "")
                    for tx in c.injuries))
            if c.penalty:
                chunks.append(f"общий штраф {c.penalty}")
            if c.unconscious:
                chunks.append("без сознания")
            if c.dead:
                chunks.append("мёртв")
            lines.append("- " + "; ".join(chunks))
        return "\n".join(lines)

    def line_conditions(self, name: str) -> str:
        """Короткая строка остатка — перед каждым ходом, без исключений.

        Ресурс, упомянутый один раз в карточке на нулевом круге, к двадцатому
        не существует: модель про него не помнит. Это свойство подачи, а не
        модели, и лечится не уговорами в промпте, а вот этой строкой. Поэтому
        она приходит всегда, даже когда всё цело и сказать вроде бы нечего.
        """
        c = self.sheet(name)
        conditions = ", ".join(c.conditions) if c.conditions else "нет"
        chunks = [
            f"Раны {c.wounds} из {c.wounds_max}",
            f"Судьба {c.fate}, Удача {c.fortune} из {c.fortune_max}",
            f"Стойкость {c.resilience}, Решимость {c.resolve} из {c.resolve_max}",
            f"Состояний {conditions}" if conditions == "нет" else f"Состояния: {conditions}",
        ]
        active_injuries = [tx["kind"] for tx in c.injuries if not tx["healed"]]
        if active_injuries:
            chunks.append("травмы: " + ", ".join(active_injuries))
        return ". ".join(chunks) + "."

    def summary_player(self, name: str) -> str:
        """Свои цифры — каждый круг. Про чужие игрок узнаёт только со стороны."""
        line = self.line_conditions(name)
        if self.sheet(name).wounds == 0:
            line += " Ты на пределе: следующий удар пойдёт по критическим ранениям."
        return "ТВОЁ СОСТОЯНИЕ: " + line

    def totals(self) -> dict:
        return {
            **self.counters,
            "wounds_lost": dict(self.wounds_lost),
            "reached_to_zero": sorted(self.reached_to_zero),
            "injuries": {name: [tx["kind"] for tx in c.injuries]
                       for name, c in self.combatants.items() if c.injuries},
            "conditions_at_end": {name: c.conditions
                                   for name, c in self.combatants.items() if c.conditions},
        }
