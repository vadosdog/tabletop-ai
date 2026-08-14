"""Разбор реплик. Скрипт ничего не редактирует — только читает служебные метки.

Единственное исключение из «не редактируем» — служебные теги режима и блоки
«ТОЛЬКО ДЛЯ», которые вырезаются из текста для игроков: это канал управления,
а не реплика мастера.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import protocol

# Русские слова протокола — из protocol.py. Здесь только то, как они обёрнуты
# в разметку: скобки, двоеточия, звёздочки markdown, которыми модель их метит.
MODES = protocol.MODES

_TAG_MODE = re.compile(
    rf"\[\s*{protocol.TAG_MODE}\s*:\s*({'|'.join(protocol.MODES)})\s*\]", re.IGNORECASE
)
_TAG_FINALE = re.compile(rf"\[\s*{protocol.TAG_FINALE}\s*\]", re.IGNORECASE)
_TAG_DOWN = re.compile(
    rf"\[\s*{protocol.TAG_DOWN}\s*:\s*(?P<имя>[^,\]]+?)\s*(?:,\s*(?P<причина>[^\]]+?))?\s*\]",
    re.IGNORECASE,
)
_TAG_BACK = re.compile(
    rf"\[\s*{protocol.TAG_BACK}\s*:\s*(?P<имя>[^,\]]+?)\s*(?:,[^\]]*)?\]", re.IGNORECASE
)
_LINE_CHECKS = re.compile(
    rf"^[ \t>*_]*{protocol.LINE_CHECK}[ \t]*[*_]*[ \t]*:[ \t]*(?P<тело>.+?)[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)
_HEADING_PRIVATE = re.compile(
    r"^[ \t>*_#]*" + r"[ \t]+".join(protocol.HEADER_PRIVATE.split())
    + r"[ \t]*:?[ \t]*\[?[ \t]*(?P<имя>[А-ЯЁа-яё]+)[^\n]*$",
    re.IGNORECASE | re.MULTILINE,
)
_SECRET = re.compile(rf"^[ \t>*_#]*{protocol.MARK_SECRET}\b[ \t]*:?", re.IGNORECASE)
_LINE_ATTACKS = re.compile(
    rf"^[ \t>*_]*{protocol.LINE_ATTACK}[ \t]*[*_]*[ \t]*:[ \t]*(?P<тело>.+?)[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)
_ARROW = re.compile(r"\s*(?:→|->|–>|=>|➔)\s*")

# Модель, назвавшая число броска сама. Ловим цифру рядом с «бросковым» словом
# в пределах одного предложения: в прозе этого мира цифры почти не встречаются.
_ROLL_WORDS = (
    r"бросок|броск\w+|выпал\w*|выпад\w*|кида\w+|кину\w+|кинул\w*|"
    r"результат\w*|сотн\w+|д100|d100|цель\w*|целев\w+"
)
_SELF_ROLL = [
    re.compile(rf"(?:{_ROLL_WORDS})[^.!?\n]{{0,25}}?\b(\d{{1,3}})\b", re.IGNORECASE),
    re.compile(rf"\b(\d{{1,3}})\b[^.!?\n]{{0,25}}?(?:{_ROLL_WORDS})", re.IGNORECASE),
    re.compile(r"уровн\w+\s+успеха\s*[:—–-]?\s*([+-]?\d{1,2})", re.IGNORECASE),
]


@dataclass
class Attack:
    who: str
    target_name: str
    weapon: str
    defence: str | None = None
    line: str = ""
    tags: list[str] = field(default_factory=list)


@dataclass
class SkillCheck:
    character: str
    skill: str
    difficulty: str
    line: str
    tags: list[str] = field(default_factory=list)
    base: int | None = None   # мастер задал целевое число NPC прямо в строке


def mode(text: str, default: str = protocol.TALK) -> tuple[str, bool]:
    """Последний тег режима в реплике. Второе значение — был ли тег вообще."""
    found = _TAG_MODE.findall(text)
    if not found:
        return default, False
    return found[-1].upper(), True


def without_control_tags(text: str) -> str:
    """Убирает [РЕЖИМ: …], [ФИНАЛ] и [РЕШИМОСТЬ: …] — это канал управления.

    Теги выбытия и возвращения остаются в тексте: для игроков это событие
    сцены, а не служебная разметка. «Курта увели» партия должна прочитать.
    А вот начисление Решимости за столом не звучит: игрок узнаёт о нём
    отдельной служебной строкой, вместе с новым остатком.
    """
    clean = _TAG_RESOLVE.sub("", _TAG_FINALE.sub("", _TAG_MODE.sub("", text)))
    return re.sub(r"\n{3,}", "\n\n", clean).strip()


def declared_finale(text: str) -> bool:
    return bool(_TAG_FINALE.search(text))


def _clean_name(raw: str, known: list[str]) -> tuple[str, int | None, list[str]]:
    """Имя, явно указанная база и метки.

    Мастер вправе кидать за персонажей мира. Для них он либо опирается на
    карточку из конфига, либо пишет целевое число прямо в строке:
    «ПРОВЕРКА: Сержант (45), Обаяние, трудно». Число — это характеристика NPC,
    а не бросок, называть её мастеру не запрещено.
    """
    name = re.sub(r"[\[\]*_«»\"'.]", "", raw).strip()
    base = None
    explicit = re.search(r"\((\d{1,3})\)", name)
    if explicit:
        base = int(explicit.group(1))
        name = name[: explicit.start()].strip()

    first = name.split()[0] if name.split() else name
    for candidate in known:
        if first.lower().startswith(candidate.lower()[:4]):
            return candidate, base, []
    return name, base, ["проверка_за_нпс"]


def checks(text: str, known: list[str]) -> tuple[list[SkillCheck], list[str]]:
    """Все строки «ПРОВЕРКА: …». Вторым значением — метки аномалий."""
    found: list[SkillCheck] = []
    anomalies: list[str] = []

    for matched in _LINE_CHECKS.finditer(text):
        body = matched.group("тело").strip().rstrip("*_ ")
        parts = [pt.strip() for pt in body.split(",") if pt.strip()]
        line = matched.group(0).strip()

        if len(parts) < 2:
            anomalies.append("непарсимая_проверка")
            continue

        name, base, tags = _clean_name(parts[0], known)
        skill = re.sub(r"[\[\]*_]", "", parts[1]).strip()
        if len(parts) >= 3:
            difficulty = re.sub(r"[\[\]*_]", "", parts[2]).strip()
        else:
            difficulty = "испытание"
            tags.append("сложность_не_указана")
        if len(parts) > 3:
            tags.append("лишние_поля_в_проверке")

        anomalies.extend(tags)
        found.append(SkillCheck(name, skill, difficulty, line, tags, base))

    return found, anomalies


def attacks(text: str, known: list[str]) -> tuple[list[Attack], list[str]]:
    """Строки «АТАКА: кто → по кому, чем [, защита]»."""
    found: list[Attack] = []
    anomalies: list[str] = []

    for matched in _LINE_ATTACKS.finditer(text):
        body = matched.group("тело").strip().rstrip("*_ ")
        sides = _ARROW.split(body, maxsplit=1)
        if len(sides) != 2:
            anomalies.append("непарсимая_атака")
            continue

        who, _, tags_who = _clean_name(sides[0], known)
        parts = [pt.strip() for pt in sides[1].split(",") if pt.strip()]
        if not parts:
            anomalies.append("непарсимая_атака")
            continue

        target_name, _, tags_targets = _clean_name(parts[0], known)
        weapon = re.sub(r"[\[\]*_]", "", parts[1]).strip() if len(parts) > 1 else "кулаки"
        defence = parts[2] if len(parts) > 2 else None
        if len(parts) > 3:
            anomalies.append("лишние_поля_в_атаке")

        tags = [mk for mk in tags_who + tags_targets if mk != "проверка_за_нпс"]
        found.append(Attack(who, target_name, weapon, defence,
                               matched.group(0).strip(), tags))
        anomalies.extend(tags)

    return found, anomalies


def annotate_attacks(text: str, totals: dict[str, str]) -> str:
    """Дописывает исход к строке атаки. Только для лога и мастера."""
    def replacement(matched: re.Match) -> str:
        line = matched.group(0).rstrip()
        total = totals.get(line.strip())
        return f"{line} → {total}" if total else line

    return _LINE_ATTACKS.sub(replacement, text)


def without_lines_attacks(text: str) -> str:
    """Вырезает строки атак из текста для игроков.

    Игрок видит свои раны цифрами и чужие — только со стороны. Строка атаки с
    подписанным исходом выдала бы всем точный остаток ран каждого, а это ровно
    то, чего быть не должно. Само действие мастер описывает словами в сцене.
    """
    clean = _LINE_ATTACKS.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", clean).strip()


def split_private(text: str) -> tuple[str, dict[str, str]]:
    """Делит реплику мастера на публичную часть и блоки «ТОЛЬКО ДЛЯ имя»."""
    headings = list(_HEADING_PRIVATE.finditer(text))
    if not headings:
        return text.strip(), {}

    public = text[: headings[0].start()].strip()
    private: dict[str, str] = {}
    for i, heading in enumerate(headings):
        end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
        chunk = text[heading.end(): end].strip()
        name = heading.group("имя").capitalize()
        private[name] = (private.get(name, "") + "\n\n" + chunk).strip() if name in private else chunk
    return public, private


# «ТРАЧУ УДАЧУ» в ходе игрока. Допускаем падежи и разделители: модель пишет
# то «ТРАЧУ УДАЧУ», то «Трачу Удачу,», то «трачу решимость —».
_SPEND = re.compile(
    rf"\bтрач[уy]\s+(?P<ресурс>{protocol.alternation(protocol.RESOURCE_STEMS)})",
    re.IGNORECASE,
)

# «[РЕШИМОСТЬ: Ханна, +1, вступилась за Йорга против патруля]»
_TAG_RESOLVE = re.compile(
    rf"\[\s*{protocol.TAG_RESOLVE}\s*:\s*(?P<имя>[^,\]]+)\s*,\s*\+?(?P<сколько>\d+)\s*"
    r"(?:,\s*(?P<причина>[^\]]*))?\]",
    re.IGNORECASE,
)

_RESOURCE_D_NAME = protocol.RESOURCE_STEMS


# «ТРАЧУ СУДЬБУ, беру 01» — игрок в безнадёжном положении называет значение сам.
_CHOSEN_NUMBER = re.compile(
    rf"трач[уy]\s+{protocol.STEM_FATE}\w*[^.\n]{{0,40}}?\b(?P<число>100|\d{{1,2}})\b",
    re.IGNORECASE)


# Слово, в котором кириллица перемешана с латиницей. Самая коварная порча
# текста: «сcылка» с латинской c выглядит как обычное слово, но это уже не
# русский. Такие подмены глазом не ловятся, а на экране остаются навсегда.
_MIXED_WORD = re.compile(r"\b(?=\w*[а-яёА-ЯЁ])(?=\w*[a-zA-Z])\w+\b")
_CYRILLIC_V_WORD = re.compile(r"[а-яёА-ЯЁ]")
_LATIN_V_WORD = re.compile(r"[a-zA-Z]")


def corruption_language(text: str) -> dict:
    """Чем испорчен русский текст: чужими словами и подменёнными буквами.

    Считается по каждой реплике и уходит судье как минус модели. Две разные
    беды: целиком нерусская реплика — это одно, а латинская «c» посреди
    русского слова — другое, и второе опаснее, потому что незаметно.
    """
    mixed = [s for s in _MIXED_WORD.findall(text)
                 # Слово из одной латинской буквы среди кириллицы — не порча,
                 # а обычная скобка вроде «пункт a»; порча — это буква ВНУТРИ.
                 if len(_LATIN_V_WORD.findall(s)) < len(s)
                 and len(_CYRILLIC_V_WORD.findall(s)) < len(s)]
    cyrillic_count = len(_CYRILLIC_V_WORD.findall(text))
    latin_count = len(_LATIN_V_WORD.findall(text))
    wholly_foreign = latin_count > 20 and latin_count > cyrillic_count * 3
    return {
        "mixed_words": sort_unique(mixed),
        "wholly_not_russian": wholly_foreign,
        "corrupted": bool(mixed) or wholly_foreign,
    }


def sort_unique(words: list[str]) -> list[str]:
    seen, total = set(), []
    for word in words:
        key = word.lower()
        if key not in seen:
            seen.add(key)
            total.append(word)
    return total


def chosen_value(text: str, default: int = 1) -> int:
    """Какое число игрок взял себе за Судьбу. Не назвал — берём лучшее.

    Лучшее это 01: автоуспех по правилам. Молчание игрока разумнее трактовать
    в его пользу, чем гадать: он и так заплатил постоянным ресурсом.
    """
    matched = _CHOSEN_NUMBER.search(text)
    if not matched:
        return default
    return max(1, min(100, int(matched.group("число"))))


def spends(text: str) -> list[str]:
    """Какие ресурсы игрок заявил к трате. Порядок сохраняется, дубли — нет.

    Заявка — это ещё не трата: списывает её скрипт и только если есть чем.
    Именно на этом различии сгорел прошлый прогон, где судья зачёл за трату
    слова «Трачу Удачу», не подкреплённые ничем.
    """
    found: list[str] = []
    for matched in _SPEND.finditer(text):
        raw = matched.group("ресурс").lower()
        for root, name in _RESOURCE_D_NAME.items():
            if raw.startswith(root) and name not in found:
                found.append(name)
                break
    return found


def awards_resolve(text: str, known: list[str]) -> list[dict]:
    """Теги [РЕШИМОСТЬ: имя, +1, причина] из сцены мастера."""
    found = []
    for matched in _TAG_RESOLVE.finditer(text):
        name, _, tags = _clean_name(matched.group("имя"), known)
        found.append({
            "name": name,
            "amount": int(matched.group("сколько")),
            "reason": (matched.group("причина") or "").strip() or "не указана",
            "tags": tags,
            "line": matched.group(0),
        })
    return found


def down_events(text: str, known: list[str]) -> list[tuple[str, str]]:
    """Кого мастер вывел из игры и почему. Имя опознаётся по списку игроков."""
    found = []
    for matched in _TAG_DOWN.finditer(text):
        name, _, tags = _clean_name(matched.group("имя"), known)
        reason = (matched.group("причина") or "не указана").strip()
        if tags:
            reason += " (имя не опознано)"
        found.append((name, reason))
    return found


def comebacks(text: str, known: list[str]) -> list[str]:
    names = []
    for matched in _TAG_BACK.finditer(text):
        name, _, _ = _clean_name(matched.group("имя"), known)
        names.append(name)
    return names


def secret_turn(text: str) -> bool:
    return bool(_SECRET.match(text.lstrip()))


def self_rolls(text: str, known: set[int] | None = None) -> list[str]:
    """Фрагменты, в которых модель назвала число броска сама.

    `известные` — числа, которые скрипт уже выдал сам (выпавшие и целевые).
    Их пересказ не самоброс: мастер вправе назвать результат, который получил.
    """
    known = known or set()
    found: list[str] = []
    for pattern in _SELF_ROLL:
        for matched in pattern.finditer(text):
            try:
                number = int(matched.group(1))
            except (IndexError, ValueError):
                number = None
            if number is not None and number in known:
                continue
            fragment = matched.group(0).strip()
            if fragment not in found:
                found.append(fragment)
    return found


def annotate_checks(text: str, rolls: dict[str, str]) -> str:
    """Дописывает результат броска прямо к строке проверки — для глаз игроков."""
    def replacement(matched: re.Match) -> str:
        line = matched.group(0).rstrip()
        total = rolls.get(line.strip())
        return f"{line} → {total}" if total else line

    return _LINE_CHECKS.sub(replacement, text)
