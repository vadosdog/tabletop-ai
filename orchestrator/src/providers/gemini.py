"""Google, gemini-3.1-pro-preview через Interactions API.

Два места, где документация молчит, а гадать нельзя: точная форма многоходовой
истории в поле `input` и точные имена полей в usage. Поэтому разбор написан
терпимым — понимает и snake_case, и camelCase, — а если не нашёл текста вообще,
падает и показывает сырой ответ целиком. Молча вернуть пустую строку хуже: она
уйдёт в круг как «персонаж промолчал», и мы этого не заметим.

Форма истории проверяется живым запросом (preflight.py). Если массив не принят,
адаптер сам переходит на склейку истории в одну строку и пишет это в
расхождения: склейка ломает попадание в кэш, и об этом надо знать заранее, а не
разбираться потом, почему у Gemini кэш пустой.

Размышление здесь выключить нельзя, нижняя ступень — «low».
"""

from __future__ import annotations

from .base import register
from .settings import key
from .common import ProviderError, HttpProvider, ParsedResponse, HttpSession, Tokens


def _field(source: dict, *names, default=None):
    """Достаёт поле, как бы вендор его ни назвал: thoughts_token_count или ...Count."""
    for name in names:
        if name in source and source[name] is not None:
            return source[name]
    return default


def _build_text(node) -> list[str]:
    """Обходит ответ и собирает всё, что похоже на текст модели.

    Мысли (`thought: true`) в реплику не берём: это внутреннее рассуждение, оно
    учитывается токенами, но за столом его не слышно.
    """
    chunks: list[str] = []
    if isinstance(node, dict):
        if node.get("thought") is True:
            return chunks
        if isinstance(node.get("text"), str) and node["text"].strip():
            chunks.append(node["text"])
            return chunks
        for key_fields in ("output", "candidates", "content", "contents", "parts", "message"):
            if key_fields in node:
                chunks += _build_text(node[key_fields])
    elif isinstance(node, list):
        for item in node:
            chunks += _build_text(item)
    return chunks


def _find_reason(data: dict) -> str | None:
    """finish_reason добираемся хоть из корня, хоть из первого кандидата."""
    direct = _field(data, "finish_reason", "finishReason")
    if direct:
        return str(direct)
    for item in (data.get("candidates") or data.get("output") or []):
        if isinstance(item, dict):
            reason = _field(item, "finish_reason", "finishReason")
            if reason:
                return str(reason)
    block = data.get("prompt_feedback") or data.get("promptFeedback") or {}
    if isinstance(block, dict) and _field(block, "block_reason", "blockReason"):
        return "blocked"
    return None


class GeminiProvider(HttpProvider):
    name = "gemini"
    base_url = "https://generativelanguage.googleapis.com/v1beta/interactions"

    CLEARABLE = ("temperature", "top_p", "thinking_level")

    def __init__(self, *arguments, **named):
        super().__init__(*arguments, **named)
        self.merge_history = False

    def _url(self) -> str:
        return self.base_url

    def _headings(self, session: HttpSession) -> dict[str, str]:
        return {
            "x-goog-api-key": key(self.name),
            "Content-Type": "application/json",
        }

    def _body(self, session: HttpSession) -> dict:
        if self.merge_history:
            input = "\n\n".join(
                f"{'ВЕДУЩИЙ' if step['role'] == 'user' else 'ТЫ'}: {step['text']}"
                for step in session.history
            )
        else:
            input = [
                {
                    "role": "user" if step["role"] == "user" else "model",
                    "parts": [{"text": step["text"]}],
                }
                for step in session.history
            ]

        settings: dict = {"max_output_tokens": self.params.max_output}
        level = self.params.level(self.name)
        if level and "thinking_level" not in self.dropped:
            settings["thinking_level"] = level
        if self.params.temperature is not None and "temperature" not in self.dropped:
            settings["temperature"] = self.params.temperature
        if self.params.top_p is not None and "top_p" not in self.dropped:
            settings["top_p"] = self.params.top_p

        return {
            "model": session.model,
            "input": input,
            "system_instruction": session.system_prompt,
            "generation_config": settings,
        }

    def _clear_rejected(self, reply: str) -> str | None:
        """Сначала пробуем спасти форму истории, потом уже снимать параметры."""
        tail = reply.lower()
        if not self.merge_history and "input" in tail and any(
            s in tail for s in ("invalid", "expected", "unsupported", "type")
        ):
            self.merge_history = True
            self.observed_discrepancies.append(
                "массив ходов в поле input не принят — история склеивается в одну "
                "строку. Кэш по префиксу при этом работает хуже, доля попаданий "
                "ниже, чем у остальных троих (видно в таблице кэша)."
            )
            return "input"
        return super()._clear_rejected(reply)

    def _parse(self, data: dict) -> ParsedResponse:
        chunks = _build_text(data)
        reason = _find_reason(data)

        usage = data.get("usage_metadata") or data.get("usageMetadata") or \
            data.get("usage") or {}
        cache = int(_field(usage, "cached_content_token_count", "cachedContentTokenCount",
                        "cached_token_count", "cachedTokenCount", default=0) or 0)
        input = int(_field(usage, "prompt_token_count", "promptTokenCount",
                         "input_token_count", "inputTokenCount", default=0) or 0)
        output = int(_field(usage, "candidates_token_count", "candidatesTokenCount",
                          "output_token_count", "outputTokenCount", default=0) or 0)
        thoughts = int(_field(usage, "thoughts_token_count", "thoughtsTokenCount",
                          default=0) or 0)

        tokens = Tokens(
            # prompt_token_count у Google включает кэшированное: не вычесть —
            # вход задваивается и доля кэша выходит бессмысленной.
            input=max(0, input - cache),
            # Мысли Google считает отдельно от candidates, а берёт за них цену
            # выхода. Приводим к общему виду: выход = видимое плюс мысли.
            output=output + thoughts,
            reasoning=thoughts,
            cache_read=cache,
            raw=usage,
        )

        text = "\n\n".join(d.strip() for d in chunks if d.strip()).strip()
        signal = reason if reason in (
            "SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "IMAGE_SAFETY", "blocked"
        ) else None

        if not text and signal is None and reason not in ("MAX_TOKENS", "length"):
            # Текста нет, и вендор не объяснил почему. Это не «персонаж
            # промолчал», это мы не поняли ответ — надо увидеть его целиком.
            raise ProviderError(
                f"gemini: в ответе не найдено текста, причина {reason!r}. "
                f"Сырой ответ: {str(data)[:1200]}"
            )

        return ParsedResponse(
            text=text,
            tokens=tokens,
            model_actual=data.get("model") or data.get("model_version")
            or data.get("modelVersion"),
            signal_refusal=signal,
            truncated=reason in ("MAX_TOKENS", "length"),
        )


register("gemini", GeminiProvider)
