"""Кубики. Кидает только скрипт — ни мастер, ни игроки не кидают никогда.

Генератор с фиксированным зерном: зерно и порядковый номер броска пишутся в лог,
поэтому прогон воспроизводится строка в строку.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, asdict, replace

# Таблица сложности WFRP 4e. Ключи нормализованы: нижний регистр, «ё» → «е».
#
# Ключи русские нарочно и переводу не подлежат: мастер называет сложность
# словом в своей реплике («ПРОВЕРКА: Курт, Сила, трудно»), и мы ищем это слово
# здесь. Русский игровой протокол описан в protocol.py.
DIFFICULTIES: dict[str, int] = {
    "очень легко": 60, "очень легкая": 60, "очень легкое": 60, "очень легкий": 60,
    "легко": 40, "легкая": 40, "легкое": 40, "легкий": 40,
    "средне": 20, "средняя": 20, "среднее": 20, "средний": 20,
    "испытание": 0, "испытующая": 0, "обычная": 0, "обычно": 0, "без модификатора": 0,
    "трудно": -10, "трудная": -10, "трудное": -10, "трудный": -10,
    "тяжело": -20, "тяжелая": -20, "тяжелое": -20, "тяжелый": -20,
    "очень тяжело": -30, "очень тяжелая": -30, "очень тяжелое": -30, "очень тяжелый": -30,
}


def normalise(s: str) -> str:
    """Приводит название навыка или сложности к ключу словаря."""
    s = s.strip().lower().replace("ё", "е")
    s = re.sub(r"[«»\"'(),.;:!?\[\]]", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def modifier_difficulties(difficulty: str) -> tuple[int, bool]:
    """Возвращает модификатор и признак того, что сложность распознана."""
    key = normalise(difficulty)
    if key in DIFFICULTIES:
        return DIFFICULTIES[key], True
    # «трудная проверка», «тяжёлая (−20)» и прочие хвосты
    for name, mod in sorted(DIFFICULTIES.items(), key=lambda p: -len(p[0])):
        if key.startswith(name):
            return mod, True
    explicit = re.search(r"([+\-−]\s*\d{1,2})", key)
    if explicit:
        return int(explicit.group(1).replace("−", "-").replace(" ", "")), True
    return 0, False


@dataclass
class Roll:
    character: str
    skill: str
    characteristic: str
    difficulty: str
    base: int
    advances: int
    modifier: int
    target: int
    rolled: int
    success: bool
    success_levels: int
    auto: str | None
    index: int
    seed: int
    tags: list[str]
    doubles: bool = False

    def as_dict(self) -> dict:
        return asdict(self)

    def short(self) -> str:
        total = "успех" if self.success else "провал"
        auto = f", {self.auto}" if self.auto else ""
        doubles = ""
        if self.doubles:
            doubles = (", ДУБЛЬ — выдающийся успех, опиши дополнительный эффект"
                     if self.success else ", ДУБЛЬ — фумбл, опиши осложнение")
        return (
            f"выпало {self.rolled}, цель {self.target}, {total}, "
            f"уровней успеха {self.success_levels:+d}{auto}{doubles}"
        )

    def as_line(self) -> str:
        return f"{self.character}, {self.skill}, {self.difficulty} — {self.short()}"


class Dice:
    """Сотня с фиксируемым зерном плюс разбор целевого числа по карточке персонажа."""

    DOUBLES = (11, 22, 33, 44, 55, 66, 77, 88, 99, 100)

    def __init__(self, config: dict, seed: int, doubles_share: float = 0.0):
        self.seed = seed
        self._rng = random.Random(seed)
        self._index = 0
        # Подкрутка — только для проверочных прогонов: она ломает честность
        # бросков, ради которой всё и затевалось. Каждый подкрученный бросок
        # помечается в логе, чтобы такой прогон нельзя было выдать за настоящий.
        self.doubles_share = max(0.0, min(float(doubles_share), 1.0))
        self.rigged = 0
        # Игроки и персонажи мира в одном справочнике: мастер вправе кидать
        # за Гретель и патруль, и это должны быть их числа, а не заглушка.
        self.characters = {**config["characters"], **config.get("npcs", {})}
        self.map_skills = {
            normalise(k): v for k, v in config["skill_to_characteristic"].items()
        }
        self.advances = config.get("trained_skill_advances", 10)
        self.default_base = config.get("default_base", 30)

    def _base(
        self, character: str, skill: str, explicit: int | None = None
    ) -> tuple[int, str, int, list[str]]:
        """База, название характеристики, надбавка за обученность, метки."""
        tags: list[str] = []
        card = self.characters.get(character)
        if card is None:
            if explicit is not None:
                # Мастер назвал целевое число NPC сам — это характеристика,
                # а не бросок, и запрет на числа её не касается.
                return explicit, "названа мастером", 0, ["база_от_мастера"]
            return self.default_base, "?", 0, ["нпс_без_карточки"]

        characteristics = card["characteristics"]
        key = normalise(skill)

        # Явное значение навыка в карточке старше расчёта: в документе у
        # персонажей мира навыки заданы числом, а не характеристикой с надбавкой.
        for name, value in card.get("skills", {}).items():
            if normalise(name) == key:
                return value, f"навык {name}", 0, tags

        # Проверка прямо по характеристике.
        for name, value in characteristics.items():
            if normalise(name) == key:
                return value, name, 0, tags

        # Проверка по навыку: база — характеристика, на которой навык стоит.
        characteristic = self.map_skills.get(key)
        if characteristic is None:
            tags.append("неизвестный_навык")
            return self.default_base, "?", 0, tags

        trained = any(
            normalise(n) == key for n in card.get("trained_skills", [])
        )
        advances = self.advances if trained else 0
        return characteristics[characteristic], characteristic, advances, tags

    def substitute(self, result: Roll, value: int, tag: str) -> Roll:
        """Тот же бросок, но с выбранным значением вместо выпавшего.

        Нужно для Судьбы: игрок в безнадёжном положении тратит её и называет
        число сам. Пересчитываем всё, что от него зависит — успех, уровни,
        автоматику, дубль, — иначе в лог уйдёт красивая единица с чужим
        исходом. Исходный бросок при этом никуда не девается: он уже записан,
        и сравнение «выпало бы — взял» пойдёт в разбор.
        """
        value = max(1, min(100, int(value)))
        success = value <= result.target
        levels = (result.target // 10) - (value // 10)
        auto = None
        if value <= 5:
            auto, success, levels = "автоуспех", True, max(levels, 0)
        elif value >= 96:
            auto, success, levels = "автопровал", False, min(levels, -1)
        doubles = value == 100 or (value % 11 == 0 and value > 0)

        substituted = replace(
            result, rolled=value, success=success, success_levels=levels,
            auto=auto, doubles=doubles,
            tags=[mk for mk in result.tags if mk != "подкручено"] + [tag],
        )
        return substituted

    def roll(
        self, character: str, skill: str, difficulty: str, explicit_base: int | None = None
    ) -> Roll:
        base, characteristic, advances, tags = self._base(character, skill, explicit_base)
        modifier, recognised = modifier_difficulties(difficulty)
        if not recognised:
            tags.append("неизвестная_сложность")

        target = base + advances + modifier
        self._index += 1
        rolled, is_rigged = self._hundred()
        if is_rigged:
            tags.append("подкручено")

        auto = None
        success = rolled <= target
        levels = (target // 10) - (rolled // 10)

        # Автоматика WFRP: 01–05 успех всегда, 96–100 провал всегда.
        if rolled <= 5:
            auto = "автоуспех"
            success = True
            levels = max(levels, 0)
        elif rolled >= 96:
            auto = "автопровал"
            success = False
            levels = min(levels, -1)

        # Дубль на обычной проверке — тоже событие: выдающийся успех или фумбл.
        has_doubles = rolled == 100 or (rolled % 11 == 0 and rolled > 0)
        if has_doubles:
            tags.append("дубль_успех" if success else "фумбл")

        return Roll(
            character=character, skill=skill, characteristic=characteristic,
            difficulty=difficulty, base=base, advances=advances, modifier=modifier,
            target=target, rolled=rolled, success=success, success_levels=levels, auto=auto,
            index=self._index, seed=self.seed, tags=tags, doubles=has_doubles,
        )

    @property
    def rolls(self) -> int:
        return self._index

    def _hundred(self) -> tuple[int, bool]:
        """Сотня. При подкрутке с заданной долей выдаёт дубль."""
        if self.doubles_share and self._rng.random() < self.doubles_share:
            self.rigged += 1
            return self._rng.choice(self.DOUBLES), True
        return self._rng.randint(1, 100), False

    def fake(self) -> int:
        """Настоящий бросок взамен числа, которое модель назвала сама."""
        self._index += 1
        return self._hundred()[0]
