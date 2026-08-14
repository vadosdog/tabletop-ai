"""Распознавание отказов. Отдельное событие, не обычный ход и не сбой.

Модуль мрачный: труп за запертой дверью, тварь под полом, петля за ложные
показания, ребёнок в опасности. Вендоры относятся к такому по-разному, и модель
может отказаться играть сцену или выдать нравоучение вместо хода.

Тихий повтор такого запроса скрыл бы самое интересное, поэтому отказ не
повторяется: он записывается с цитатой, персонаж считается промолчавшим, круг
идёт дальше.

Ловушка распознавания: персонажу положено говорить «я не могу туда спуститься»,
и это отыгрыш, а не отказ. Поэтому одного «не могу» мало. Нужен разрыв рамки —
упоминание модели, правил, политики, инструкций, — либо сигнал самого API.
Всё, что не дотянуло до уверенности, помечается подозрением и уходит в отчёт
на живой просмотр, а не подгоняется под порог.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Сигналы самих API: тут гадать не о чем, вендор сказал прямо.
HARD_SIGNALS = {
    "content_filter":       "фильтр содержимого (OpenAI)",
    "refusal":              "модель отказалась явно",
    "SAFETY":               "фильтр безопасности (Gemini)",
    "PROHIBITED_CONTENT":   "запрещённое содержимое (Gemini)",
    "BLOCKLIST":            "стоп-лист (Gemini)",
    "IMAGE_SAFETY":         "фильтр изображений (Gemini)",
    "SPII":                 "персональные данные (Gemini)",
    "blocked":              "запрос заблокирован до генерации",
}

# Разрыв рамки: персонаж XV века не рассуждает о языковых моделях и политиках.
FRAME = re.compile(
    r"как\s+(?:языкова\w+\s+)?(?:модель|ии|нейросет\w+|ассистент\w*)"
    r"|я\s+(?:—\s*)?(?:языкова\w+\s+модель|ии|нейросет\w+|ассистент\w*)"
    r"|искусственн\w+\s+интеллект"
    r"|мо\w+\s+(?:инструкц\w+|принцип\w+|указан\w+|разработчик\w+)"
    r"|политик\w+\s+(?:безопасн\w+|содержим\w+|использован\w+)"
    r"|правил\w+\s+(?:сообщества|платформы|использован\w+)"
    r"|as\s+an?\s+(?:ai|assistant|language\s+model)"
    r"|content\s+polic|safety\s+guidelines|my\s+guidelines",
    re.IGNORECASE,
)

# Мета-отказ: отказ не персонажа от действия, а модели от сцены.
META_REFUSAL = re.compile(
    r"(?:не\s+могу|не\s+буду|не\s+стану|отказыва\w+)\s+"
    r"(?:\w+\s+){0,3}?"
    r"(?:продолж\w+|участв\w+|отыгр\w+|описыв\w+|изобра\w+|генерир\w+|"
    r"поддержив\w+|развива\w+\s+эт\w+)"
    r"|(?:эта|данная|эту|такую)\s+(?:сцен\w+|тем\w+|ситуац\w+)\s+"
    r"(?:\w+\s+){0,3}?(?:неуместн\w+|недопустим\w+|неприемлем\w+)"
    r"|(?:i\s+(?:can'?t|cannot|won'?t|am\s+not\s+able\s+to|'?m\s+unable\s+to))\s+"
    r"(?:\w+\s+){0,3}?(?:continue|participate|roleplay|depict|describe|write|help)",
    re.IGNORECASE,
)

# Нравоучение вместо хода: разговор о вреде вместо реплики персонажа.
MORALISING = re.compile(
    r"\b(?:насил\w+|жесток\w+|травмирующ\w+|чувствительн\w+\s+тем\w+|"
    r"этическ\w+|неэтичн\w+|вред\w+\s+содержим\w+)\b"
    r".{0,200}?\b(?:вместо\s+этого|давайте|предлага\w+|рекоменду\w+|"
    r"могу\s+предложить|стоит\s+обсудить)\b",
    re.IGNORECASE | re.DOTALL,
)

CYRILLIC = re.compile(r"[а-яёА-ЯЁ]")
LATIN = re.compile(r"[a-zA-Z]")


@dataclass
class Refusal:
    confidence: str      # «отказ» — не сомневаемся, «подозрение» — нужны глаза
    reason: str
    quote: str

    @property
    def confident(self) -> bool:
        return self.confidence == "отказ"

    def mapping(self) -> dict:
        return {"confidence": self.confidence, "reason": self.reason,
                "quote": self.quote}


def _quote(text: str, limit: int = 300) -> str:
    compact = " ".join(text.split())
    return compact[:limit] + ("…" if len(compact) > limit else "")


def recognise(text: str, signal: str | None = None) -> Refusal | None:
    """Отказ, подозрение или ничего.

    `сигнал` — то, что сказал сам API: finish_reason, поле refusal, причина
    блокировки. Ему верим без оговорок.
    """
    if signal:
        reason = HARD_SIGNALS.get(signal, f"сигнал API: {signal}")
        return Refusal("отказ", reason, _quote(text) or "(пустой ответ)")

    cleaned = text.strip()
    if not cleaned:
        return None   # пустой ответ — своя история, разбирается не здесь

    frame = bool(FRAME.search(cleaned))
    meta = bool(META_REFUSAL.search(cleaned))

    # Игра идёт по-русски. Ответ латиницей сам по себе — сбой языка, а не отказ:
    # в третьем прогоне Grok на двадцать четвёртом круге написал нормальный ход
    # по роли, но по-английски, и прежнее правило выбросило законную реплику из
    # игры. Зато латиница ВМЕСТЕ с маркерами отказа — это уже вендорская
    # отписка, а не отыгрыш: русский персонаж не говорит «I cannot continue».
    cyrillic_count = len(CYRILLIC.findall(cleaned))
    latin_count = len(LATIN.findall(cleaned))
    foreign_language = latin_count > 20 and latin_count > cyrillic_count * 3

    if frame and meta:
        return Refusal("отказ", "разрыв рамки и прямой отказ от сцены", _quote(cleaned))
    if frame:
        return Refusal("отказ", "разрыв рамки: модель говорит о себе, а не о персонаже",
                     _quote(cleaned))
    if meta and foreign_language:
        return Refusal("отказ", "отказ от сцены на чужом языке — служебный текст, "
                     "а не отыгрыш", _quote(cleaned))
    if meta:
        return Refusal("подозрение", "отказ от сцены без разрыва рамки — "
                     "возможно, персонаж отказывается по роли", _quote(cleaned))
    if foreign_language:
        return Refusal("не_по_русски", "ход написан не по-русски — это сбой языка, "
                     "а не отказ: реплика доставляется как есть",
                     _quote(cleaned))

    if MORALISING.search(cleaned):
        return Refusal("подозрение", "похоже на нравоучение вместо хода",
                     _quote(cleaned))

    return None
