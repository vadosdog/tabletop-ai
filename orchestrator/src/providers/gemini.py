"""Google, gemini-3.1-pro-preview через Interactions API.

Два места, где документация молчит, а гадать нельзя: точная форма многоходовой
истории в поле `input` и точные имена полей в usage. Поэтому разбор написан
терпимым — понимает и snake_case, и camelCase, — а если не нашёл текста вообще,
падает и показывает сырой ответ целиком. Молча вернуть пустую строку хуже: она
уйдёт в круг как «персонаж промолчал», и мы этого не заметим.

Форма истории проверяется живым запросом (проверка.py). Если массив не принят,
адаптер сам переходит на склейку истории в одну строку и пишет это в
расхождения: склейка ломает попадание в кэш, и об этом надо знать заранее, а не
разбираться потом, почему у Gemini кэш пустой.

Размышление здесь выключить нельзя, нижняя ступень — «low».
"""

from __future__ import annotations

from .base import зарегистрировать
from .настройки import ключ
from .общий import ОшибкаПровайдера, ПровайдерHTTP, Разбор, СессияHTTP, Токены


def _поле(источник: dict, *имена, умолчание=None):
    """Достаёт поле, как бы вендор его ни назвал: thoughts_token_count или ...Count."""
    for имя in имена:
        if имя in источник and источник[имя] is not None:
            return источник[имя]
    return умолчание


def _собрать_текст(узел) -> list[str]:
    """Обходит ответ и собирает всё, что похоже на текст модели.

    Мысли (`thought: true`) в реплику не берём: это внутреннее рассуждение, оно
    учитывается токенами, но за столом его не слышно.
    """
    куски: list[str] = []
    if isinstance(узел, dict):
        if узел.get("thought") is True:
            return куски
        if isinstance(узел.get("text"), str) and узел["text"].strip():
            куски.append(узел["text"])
            return куски
        for ключ_поля in ("output", "candidates", "content", "contents", "parts", "message"):
            if ключ_поля in узел:
                куски += _собрать_текст(узел[ключ_поля])
    elif isinstance(узел, list):
        for элемент in узел:
            куски += _собрать_текст(элемент)
    return куски


def _найти_причину(данные: dict) -> str | None:
    """finish_reason добираемся хоть из корня, хоть из первого кандидата."""
    прямая = _поле(данные, "finish_reason", "finishReason")
    if прямая:
        return str(прямая)
    for элемент in (данные.get("candidates") or данные.get("output") or []):
        if isinstance(элемент, dict):
            причина = _поле(элемент, "finish_reason", "finishReason")
            if причина:
                return str(причина)
    блок = данные.get("prompt_feedback") or данные.get("promptFeedback") or {}
    if isinstance(блок, dict) and _поле(блок, "block_reason", "blockReason"):
        return "blocked"
    return None


class ПровайдерGemini(ПровайдерHTTP):
    имя = "gemini"
    базовый_url = "https://generativelanguage.googleapis.com/v1beta/interactions"

    СНИМАЕМЫЕ = ("temperature", "top_p", "thinking_level")

    def __init__(self, *доводы, **именованные):
        super().__init__(*доводы, **именованные)
        self.склеивать_историю = False

    def _url(self) -> str:
        return self.базовый_url

    def _заголовки(self, сессия: СессияHTTP) -> dict[str, str]:
        return {
            "x-goog-api-key": ключ(self.имя),
            "Content-Type": "application/json",
        }

    def _тело(self, сессия: СессияHTTP) -> dict:
        if self.склеивать_историю:
            вход = "\n\n".join(
                f"{'ВЕДУЩИЙ' if шаг['роль'] == 'user' else 'ТЫ'}: {шаг['текст']}"
                for шаг in сессия.история
            )
        else:
            вход = [
                {
                    "role": "user" if шаг["роль"] == "user" else "model",
                    "parts": [{"text": шаг["текст"]}],
                }
                for шаг in сессия.история
            ]

        настройки: dict = {"max_output_tokens": self.параметры.предел_ответа}
        уровень = self.параметры.уровень(self.имя)
        if уровень and "thinking_level" not in self.отброшенные:
            настройки["thinking_level"] = уровень
        if self.параметры.температура is not None and "temperature" not in self.отброшенные:
            настройки["temperature"] = self.параметры.температура
        if self.параметры.top_p is not None and "top_p" not in self.отброшенные:
            настройки["top_p"] = self.параметры.top_p

        return {
            "model": сессия.модель,
            "input": вход,
            "system_instruction": сессия.системный_промпт,
            "generation_config": настройки,
        }

    def _снять_отвергнутое(self, ответ: str) -> str | None:
        """Сначала пробуем спасти форму истории, потом уже снимать параметры."""
        низ = ответ.lower()
        if not self.склеивать_историю and "input" in низ and any(
            с in низ for с in ("invalid", "expected", "unsupported", "type")
        ):
            self.склеивать_историю = True
            self.добытые_расхождения.append(
                "массив ходов в поле input не принят — история склеивается в одну "
                "строку. Кэш по префиксу при этом работает хуже, доля попаданий "
                "ниже, чем у остальных троих (видно в таблице кэша)."
            )
            return "input"
        return super()._снять_отвергнутое(ответ)

    def _разобрать(self, данные: dict) -> Разбор:
        куски = _собрать_текст(данные)
        причина = _найти_причину(данные)

        usage = данные.get("usage_metadata") or данные.get("usageMetadata") or \
            данные.get("usage") or {}
        кэш = int(_поле(usage, "cached_content_token_count", "cachedContentTokenCount",
                        "cached_token_count", "cachedTokenCount", умолчание=0) or 0)
        вход = int(_поле(usage, "prompt_token_count", "promptTokenCount",
                         "input_token_count", "inputTokenCount", умолчание=0) or 0)
        выход = int(_поле(usage, "candidates_token_count", "candidatesTokenCount",
                          "output_token_count", "outputTokenCount", умолчание=0) or 0)
        мысли = int(_поле(usage, "thoughts_token_count", "thoughtsTokenCount",
                          умолчание=0) or 0)

        токены = Токены(
            # prompt_token_count у Google включает кэшированное: не вычесть —
            # вход задваивается и доля кэша выходит бессмысленной.
            вход=max(0, вход - кэш),
            # Мысли Google считает отдельно от candidates, а берёт за них цену
            # выхода. Приводим к общему виду: выход = видимое плюс мысли.
            выход=выход + мысли,
            размышление=мысли,
            кэш_чтение=кэш,
            сырое=usage,
        )

        текст = "\n\n".join(к.strip() for к in куски if к.strip()).strip()
        сигнал = причина if причина in (
            "SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "IMAGE_SAFETY", "blocked"
        ) else None

        if not текст and сигнал is None and причина not in ("MAX_TOKENS", "length"):
            # Текста нет, и вендор не объяснил почему. Это не «персонаж
            # промолчал», это мы не поняли ответ — надо увидеть его целиком.
            raise ОшибкаПровайдера(
                f"gemini: в ответе не найдено текста, причина {причина!r}. "
                f"Сырой ответ: {str(данные)[:1200]}"
            )

        return Разбор(
            текст=текст,
            токены=токены,
            модель_факт=данные.get("model") or данные.get("model_version")
            or данные.get("modelVersion"),
            сигнал_отказа=сигнал,
            оборван=причина in ("MAX_TOKENS", "length"),
        )


зарегистрировать("gemini", ПровайдерGemini)
