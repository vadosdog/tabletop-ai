"""OpenRouter как прослойка. За столом через него сидит Qwen 3.7 Max.

Прослойка избавляет от регистрации в китайском облаке и обходится дешевле
прямого подключения, но добавляет риск, которого у прямых подключений нет:
**OpenRouter — маршрутизатор.** За одним именем модели может стоять несколько
хостов с разными сборками и разной квантизацией, и он вправе переключиться между
ними когда угодно. Молчаливое переключение посреди тридцатикругового прогона
означало бы, что вторую половину игры мы сравниваем не то, что думаем.

Поэтому здесь три замка:

1. `provider.order` — явный список апстримов, `allow_fallbacks: false` — запрет
   уходить к другим. Если единственный разрешённый лежит, запрос падает, и это
   правильно: упасть громко лучше, чем доиграть на другой сборке.
2. `provider.require_parameters: true` — не отдавать запрос апстриму, который не
   умеет наши параметры генерации. Иначе прослойка тихо выбросит температуру, и
   равенство условий окажется мнимым.
3. Кто ответил — в лог на каждом вызове, а смена апстрима посреди прогона —
   отдельным событием, а не тихой заменой.

Кэш: у Qwen через OpenRouter нет автоматического кэширования по префиксу
(`supports_implicit_caching: false` в описании эндпоинта), в отличие от GPT и
Grok. Цены на чтение и запись кэша при этом есть, то есть кэш только явный, по
пометкам. Пометки ставим на системный промпт — самый большой неизменный кусок, —
а если апстрим их не примет, снимаем и пишем в расхождения. Нулевая доля кэша
здесь не ошибка, но в отчёте она должна быть видна: условия по кэшу у Qwen
отличаются от остальных.
"""

from __future__ import annotations

from .base import register
from .settings import key
from .common import ParsedResponse, HttpSession
from .chat import ChatProvider


class OpenRouterProvider(ChatProvider):
    name = "openrouter"
    base_url = "https://openrouter.ai/api/v1/chat/completions"

    # У OpenRouter уровень размышления передаётся объектом, а не строкой,
    # поэтому поле из chat.py не используется — тело собирается ниже.
    field_reasoning = "reasoning"

    def __init__(self, *arguments, upstreams: list[str] | None = None,
                 explicit_cache: bool = True, **named):
        super().__init__(*arguments, **named)
        # Список разрешённых апстримов приходит из конфига: зашивать его в код
        # нельзя, состав хостов у модели меняется со временем.
        self.upstreams = list_upstreams(upstreams)
        self.explicit_cache = explicit_cache
        self.seen_upstreams: list[str] = []

    def _headings(self, session: HttpSession) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {key(self.name)}",
            "Content-Type": "application/json",
            # OpenRouter просит представиться: по этим полям он разводит трафик
            # и показывает расход в кабинете отдельной строкой. Только латиница:
            # заголовки обязаны быть ASCII.
            "HTTP-Referer": "https://github.com/vasilyev/nri-progon",
            "X-Title": "NRI: four models at one table",
        }

    def _messages(self, session: HttpSession) -> list[dict]:
        messages = super()._messages(session)
        if not self.explicit_cache:
            return messages
        # Пометка кэша на системный промпт: он у игрока самый большой и не
        # меняется весь прогон, а история дописывается и кэшируется хуже.
        messages[0] = {
            "role": "system",
            "content": [{
                "type": "text",
                "text": session.system_prompt,
                "cache_control": {"type": "ephemeral"},
            }],
        }
        return messages

    def _body(self, session: HttpSession) -> dict:
        body = super()._body(session)
        # Строковый reasoning_effort из chat.py прослойке не годится — у неё
        # объект. Снимаем его и ставим свой.
        body.pop("reasoning_effort", None)

        level = self.params.level(self.name)
        if level and "reasoning" not in self.dropped:
            body["reasoning"] = {"effort": level}

        if "provider" not in self.dropped:
            body["provider"] = {
                "order": self.upstreams,
                # Никаких запасных хостов: лучше упасть, чем доиграть на другой
                # сборке и не заметить.
                "allow_fallbacks": False,
                # Не отдавать апстриму, который не умеет наши параметры.
                "require_parameters": True,
            }
        # Просим прослойку посчитать деньги самой: её цифра точнее нашего
        # пересчёта по ставкам из конфига, потому что это её же счёт.
        body["usage"] = {"include": True}
        return body

    def _clear_rejected(self, reply: str) -> str | None:
        """Сначала спасаем пометки кэша, потом уже общие параметры.

        Кэш — единственное, чем здесь можно пожертвовать без ущерба для
        сравнения. Фиксацией апстрима жертвовать нельзя: без неё прогон
        перестаёт быть честным, и падение уместнее.
        """
        tail = reply.lower()
        if self.explicit_cache and "cache_control" in tail:
            self.explicit_cache = False
            self.observed_discrepancies.append(
                "апстрим не принял пометки явного кэша — они сняты. "
                "Автоматического кэша по префиксу у этой модели нет, так что "
                "доля попаданий будет нулевой: весь контекст каждый ход "
                "оплачивается по полной входной цене, в отличие от остальных."
            )
            return "cache_control"
        return super()._clear_rejected(reply)

    def _parse(self, data: dict) -> ParsedResponse:
        parsed = super()._parse(data)

        upstream = data.get("provider") or data.get("provider_name")
        parsed.upstream = str(upstream) if upstream else None
        if parsed.upstream:
            # Кто ответил — видно в каждом ходе, а не только в отдельном событии.
            parsed.model_actual = f"{parsed.model_actual or '?'} @ {parsed.upstream}"
            if parsed.upstream not in self.seen_upstreams:
                very_first = not self.seen_upstreams
                self.seen_upstreams.append(parsed.upstream)
                self._write(
                    "апстрим", upstream=parsed.upstream,
                    allowed=self.upstreams,
                    first=very_first,
                )
                if not very_first:
                    # Смена посреди прогона — событие, а не тихая замена.
                    self.observed_discrepancies.append(
                        f"апстрим сменился посреди прогона: было "
                        f"{', '.join(self.seen_upstreams[:-1])}, стало "
                        f"{parsed.upstream}. Часть ходов сыграна на другой сборке, "
                        f"и сравнивать их с остальными надо с оговоркой."
                    )

        usage = data.get("usage") or {}
        if isinstance(usage.get("cost"), (int, float)):
            parsed.cost = float(usage["cost"])
        return parsed


def list_upstreams(given: list[str] | None) -> list[str]:
    """Пустой список означал бы «маршрутизируй как хочешь» — это запрещено."""
    upstreams = [i for i in (given or []) if i]
    if not upstreams:
        raise SystemExit(
            "openrouter: не задан список апстримов. Без него прослойка вольна "
            "сменить хост посреди прогона, и сравнение развалится.\n"
            "Положить в config.json:\n"
            '  "апстримы_openrouter": ["alibaba"]\n'
            "Актуальный список хостов у модели:\n"
            "  curl -s https://openrouter.ai/api/v1/models/qwen/qwen3.7-max/endpoints "
            "| python3 -m json.tool"
        )
    return upstreams


register("openrouter", OpenRouterProvider)
