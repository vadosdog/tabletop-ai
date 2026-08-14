"""xAI, grok-4.5 напрямую через /chat/completions.

Кэш у xAI автоматический, но привязан к машине: без заголовка `x-grok-conv-id`
запросы одного агента расходятся по серверам и половина ходов оплачивается по
полной входной цене. Заголовок ставим у каждого персонажа свой.

Размышление здесь выключить нельзя — нижняя ступень `reasoning_effort` это
«low». Это записано в известные расхождения и уходит в отчёт.

Вся форма запроса и разбора — в чат.py, общая с OpenRouter.
"""

from __future__ import annotations

from .base import register
from .settings import key
from .common import tag_agent
from .chat import ChatProvider, HttpSession


class XAIProvider(ChatProvider):
    name = "xai"
    base_url = "https://api.x.ai/v1/chat/completions"

    # У xAI размышление лежит рядом с выходом, а не внутри него: на круге ноль
    # 4741 + 142 + 167 = 5050 = total_tokens. Не сложить — выход и деньги
    # занижены наполовину.
    reasoning_separately = True

    def _headings(self, session: HttpSession) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {key(self.name)}",
            "Content-Type": "application/json",
            # Липкость кэша: без неё запросы агента разъезжаются по машинам
            # и половина ходов оплачивается по полной входной цене. Имя
            # персонажа сюда не поставить — заголовки обязаны быть ASCII.
            "x-grok-conv-id": tag_agent(session.agent),
        }


register("xai", XAIProvider)
