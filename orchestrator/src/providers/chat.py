"""Основа для всех, кто говорит на OpenAI-совместимом /chat/completions.

Этим языком говорят xAI напрямую и OpenRouter как прослойка. Разница между ними
— адрес, заголовки и несколько дополнительных полей в теле, а форма сообщений,
разбор ответа и вытаскивание usage у них общие и лежат здесь.

Отдельно от openai.py нарочно: тот работает через Responses API, у него другая
форма и запроса, и ответа, и usage. Общего у них меньше, чем кажется по имени.
"""

from __future__ import annotations

from .common import HttpProvider, ParsedResponse, HttpSession, Tokens


class ChatProvider(HttpProvider):
    """Один запрос — вся история заново. Историю держит адаптер."""

    # Имя поля с уровнем размышления: у разных прослоек оно разное.
    field_reasoning = "reasoning_effort"

    # Входит ли размышление в completion_tokens. Общего правила нет, и ошибка
    # здесь стоит дорого: не сложить — выход и деньги занижены, сложить лишний
    # раз — завышены. Проверяется арифметикой самого вендора, см. _токены.
    reasoning_separately = False

    def _url(self) -> str:
        return self.base_url

    def _messages(self, session: HttpSession) -> list[dict]:
        messages = [{"role": "system", "content": session.system_prompt}]
        messages += [
            {"role": step["role"], "content": step["text"]} for step in session.history
        ]
        return messages

    def _body(self, session: HttpSession) -> dict:
        body: dict = {
            "model": session.model,
            "messages": self._messages(session),
            "max_tokens": self.params.max_output,
        }
        level = self.params.level(self.name)
        if level and self.field_reasoning not in self.dropped:
            body[self.field_reasoning] = level
        if self.params.temperature is not None and "temperature" not in self.dropped:
            body["temperature"] = self.params.temperature
        if self.params.top_p is not None and "top_p" not in self.dropped:
            body["top_p"] = self.params.top_p
        return body

    def _reasoning_outside_output(self, usage: dict) -> bool:
        """Спрашиваем у самого вендора, а не гадаем: сходится ли total.

        Если prompt + completion + reasoning == total, значит размышление лежит
        рядом с выходом, а не внутри него. Проверка по сумме надёжнее любого
        умолчания: вендор вправе поменять формат, и тогда мы это заметим сразу,
        а не через тридцать кругов в таблице расходов.
        """
        total_of = usage.get("total_tokens")
        input = usage.get("prompt_tokens")
        output = usage.get("completion_tokens")
        reas = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
        if None in (total_of, input, output) or not reas:
            return self.reasoning_separately
        if input + output + reas == total_of:
            return True
        if input + output == total_of:
            return False
        return self.reasoning_separately

    def _tokens(self, usage: dict) -> Tokens:
        reasoning = int(
            (usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0
        )
        output = int(usage.get("completion_tokens") or 0)
        if reasoning and self._reasoning_outside_output(usage):
            # Приводим к общему виду: выход всегда включает размышление, потому
            # что вендор берёт за него цену выхода.
            output += reasoning

        tokens = Tokens(
            input=int(usage.get("prompt_tokens") or 0),
            output=output,
            reasoning=reasoning,
            cache_read=int(
                (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
            ),
            cache_write=int(
                (usage.get("prompt_tokens_details") or {}).get("cache_write_tokens") or 0
            ),
            raw=usage,
        )
        # prompt_tokens считает кэшированные вместе с обычными: без вычета вход
        # задваивается, и доля кэша выходит бессмысленной.
        tokens.input = max(0, tokens.input - tokens.cache_read)
        return tokens

    def _parse(self, data: dict) -> ParsedResponse:
        choices = data.get("choices") or []
        first = choices[0] if choices else {}
        message = first.get("message") or {}

        text = (message.get("content") or "").strip()
        signal = None
        if message.get("refusal"):
            signal = "refusal"
            text = text or str(message["refusal"])

        reason = first.get("finish_reason")
        if reason == "content_filter":
            signal = signal or "content_filter"

        return ParsedResponse(
            text=text,
            tokens=self._tokens(data.get("usage") or {}),
            model_actual=data.get("model"),
            signal_refusal=signal,
            truncated=reason == "length",
        )
