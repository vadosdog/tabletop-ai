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

from .base import зарегистрировать
from .настройки import ключ
from .общий import ПровайдерHTTP, Разбор, СессияHTTP, Токены


class ПровайдерOpenAI(ПровайдерHTTP):
    имя = "openai"
    базовый_url = "https://api.openai.com/v1/responses"

    def _url(self) -> str:
        return self.базовый_url

    def _заголовки(self, сессия: СессияHTTP) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {ключ(self.имя)}",
            "Content-Type": "application/json",
        }

    def _тело(self, сессия: СессияHTTP) -> dict:
        вход = []
        for шаг in сессия.история:
            тип = "input_text" if шаг["роль"] == "user" else "output_text"
            вход.append({
                "role": шаг["роль"],
                "content": [{"type": тип, "text": шаг["текст"]}],
            })

        тело: dict = {
            "model": сессия.модель,
            "instructions": сессия.системный_промпт,
            "input": вход,
            "max_output_tokens": self.параметры.предел_ответа,
            # Историю держим у себя: транскрипт должен лежать в нашем логе,
            # а не на стороне вендора.
            "store": False,
            "prompt_cache_key": f"нри-{сессия.агент}",
        }

        уровень = self.параметры.уровень(self.имя)
        if уровень and "reasoning_effort" not in self.отброшенные:
            тело["reasoning"] = {"effort": уровень}
        if self.параметры.температура is not None and "temperature" not in self.отброшенные:
            тело["temperature"] = self.параметры.температура
        if self.параметры.top_p is not None and "top_p" not in self.отброшенные:
            тело["top_p"] = self.параметры.top_p
        return тело

    def _разобрать(self, данные: dict) -> Разбор:
        куски: list[str] = []
        сигнал: str | None = None

        for элемент in данные.get("output") or []:
            if элемент.get("type") != "message":
                continue
            for блок in элемент.get("content") or []:
                if блок.get("type") == "output_text" and блок.get("text"):
                    куски.append(блок["text"])
                elif блок.get("type") == "refusal":
                    # Явное поле отказа: вендор сказал прямо, гадать не о чем.
                    сигнал = "refusal"
                    if блок.get("refusal"):
                        куски.append(блок["refusal"])

        оборван = False
        причина = ((данные.get("incomplete_details") or {}).get("reason"))
        if причина == "max_output_tokens":
            оборван = True
        elif причина == "content_filter":
            сигнал = сигнал or "content_filter"

        usage = данные.get("usage") or {}
        токены = Токены(
            вход=int(usage.get("input_tokens") or 0),
            выход=int(usage.get("output_tokens") or 0),
            размышление=int(
                (usage.get("output_tokens_details") or {}).get("reasoning_tokens") or 0
            ),
            кэш_чтение=int(
                (usage.get("input_tokens_details") or {}).get("cached_tokens") or 0
            ),
            сырое=usage,
        )
        # Кэшированные токены OpenAI показывает внутри input_tokens, а не рядом.
        # Не вычесть — счёт входа будет вдвое больше настоящего.
        токены.вход = max(0, токены.вход - токены.кэш_чтение)

        return Разбор(
            текст="\n\n".join(к.strip() for к in куски if к.strip()).strip(),
            токены=токены,
            модель_факт=данные.get("model"),
            сигнал_отказа=сигнал,
            оборван=оборван,
        )


зарегистрировать("openai", ПровайдерOpenAI)
