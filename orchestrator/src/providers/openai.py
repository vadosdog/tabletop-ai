"""OpenAI, gpt-5.6-terra через Responses API.

Кэш у OpenAI автоматический по совпадающему префиксу — явных пометок не просит.
Помогает ему `prompt_cache_key`: он приклеивает запросы одного агента к одной
машине, иначе половина ходов уходит на холодный кэш. Ключ у каждого персонажа
свой, потому что контексты у них разные с первого круга.

Особенность, которая идёт в отчёт: `max_output_tokens` у OpenAI считает вместе с
токенами размышления. При одном и том же лимите видимого текста здесь влезает
меньше, чем у остальных, — поэтому лимит взят с запасом вдесятеро.
"""

from __future__ import annotations

from .base import register
from .settings import key
from .common import HttpProvider, ParsedResponse, HttpSession, Tokens


class OpenAIProvider(HttpProvider):
    name = "openai"
    base_url = "https://api.openai.com/v1/responses"

    def _url(self) -> str:
        return self.base_url

    def _headings(self, session: HttpSession) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {key(self.name)}",
            "Content-Type": "application/json",
        }

    def _body(self, session: HttpSession) -> dict:
        input = []
        for step in session.history:
            kind = "input_text" if step["role"] == "user" else "output_text"
            input.append({
                "role": step["role"],
                "content": [{"type": kind, "text": step["text"]}],
            })

        body: dict = {
            "model": session.model,
            "instructions": session.system_prompt,
            "input": input,
            "max_output_tokens": self.params.max_output,
            # Историю держим у себя: транскрипт должен лежать в нашем логе,
            # а не на стороне вендора.
            "store": False,
            "prompt_cache_key": f"нри-{session.agent}",
        }

        level = self.params.level(self.name)
        if level and "reasoning_effort" not in self.dropped:
            body["reasoning"] = {"effort": level}
        if self.params.temperature is not None and "temperature" not in self.dropped:
            body["temperature"] = self.params.temperature
        if self.params.top_p is not None and "top_p" not in self.dropped:
            body["top_p"] = self.params.top_p
        return body

    def _parse(self, data: dict) -> ParsedResponse:
        chunks: list[str] = []
        signal: str | None = None

        for item in data.get("output") or []:
            if item.get("type") != "message":
                continue
            for block in item.get("content") or []:
                if block.get("type") == "output_text" and block.get("text"):
                    chunks.append(block["text"])
                elif block.get("type") == "refusal":
                    # Явное поле отказа: вендор сказал прямо, гадать не о чем.
                    signal = "refusal"
                    if block.get("refusal"):
                        chunks.append(block["refusal"])

        truncated = False
        reason = ((data.get("incomplete_details") or {}).get("reason"))
        if reason == "max_output_tokens":
            truncated = True
        elif reason == "content_filter":
            signal = signal or "content_filter"

        usage = data.get("usage") or {}
        tokens = Tokens(
            input=int(usage.get("input_tokens") or 0),
            output=int(usage.get("output_tokens") or 0),
            reasoning=int(
                (usage.get("output_tokens_details") or {}).get("reasoning_tokens") or 0
            ),
            cache_read=int(
                (usage.get("input_tokens_details") or {}).get("cached_tokens") or 0
            ),
            raw=usage,
        )
        # Кэшированные токены OpenAI показывает внутри input_tokens, а не рядом.
        # Не вычесть — счёт входа будет вдвое больше настоящего.
        tokens.input = max(0, tokens.input - tokens.cache_read)

        return ParsedResponse(
            text="\n\n".join(d.strip() for d in chunks if d.strip()).strip(),
            tokens=tokens,
            model_actual=data.get("model"),
            signal_refusal=signal,
            truncated=truncated,
        )


register("openai", OpenAIProvider)
