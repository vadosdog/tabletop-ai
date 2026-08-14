"""Общая механика трёх сторонних провайдеров: HTTP, повторы, учёт, отказы.

Различаются они только формой запроса и формой ответа. Всё остальное — история
диалога, единые параметры, экспоненциальная пауза по частотным лимитам, счёт
токенов и денег, ловля отказов — одно и то же, и живёт здесь. Адаптеру остаётся
четыре метода: куда стучаться, с какими заголовками, что послать, как разобрать.

SDK вендоров сюда намеренно не тащим. Их три, они тянут за собой зависимости и
прячут сырой usage, а нам нужен именно он: доля попаданий в кэш и токены
размышления идут в статью и должны быть видны как есть, а не в пересказе обёртки.
"""

from __future__ import annotations

import asyncio
import hashlib
import random
import time
from dataclasses import dataclass, field

import httpx

from . import refusals as module_refusals
from .base import Reply
from .settings import GenerationParams

# Коды, после которых имеет смысл подождать и повторить. Всё остальное — наша
# ошибка (не тот ключ, не та модель, не то поле), и повтор её не вылечит.
RETRYABLE = {408, 409, 425, 429, 500, 502, 503, 504, 529}


def tag_agent(agent: str) -> str:
    """Опознаватель агента для HTTP-заголовков.

    Заголовки обязаны быть ASCII, а персонажей у нас зовут Курт и Ханна. Берём
    устойчивый отпечаток имени: он не меняется между кругами — а именно на это
    опирается липкость кэша, — и не содержит ни одной кириллической буквы.
    """
    fingerprint = hashlib.sha256(agent.encode("utf-8")).hexdigest()[:12]
    return f"nri-{fingerprint}"


@dataclass
class Usage:
    """Счётчик на одного провайдера. Токены размышления входят в выходные.

    Разделять их нельзя: вендоры выставляют счёт за размышление по цене выхода и
    уже включают его в output_tokens. Мы храним размышление отдельной строкой,
    чтобы показать долю, но в деньги оно идёт один раз.
    """

    input: int = 0
    output: int = 0
    reasoning: int = 0
    cache_read: int = 0
    cache_write: int = 0
    requests: int = 0
    retries: int = 0
    refusal_count: int = 0
    truncations: int = 0            # ответов, упёршихся в предел_ответа
    cost: float = 0.0
    seconds: float = 0.0

    @property
    def share_cache(self) -> float | None:
        """Какая часть входа пришла из кэша. Влияет на цену, скорость и статью."""
        total_of = self.input + self.cache_read
        return round(self.cache_read / total_of, 4) if total_of else None

    @property
    def share_reasoning(self) -> float | None:
        return round(self.reasoning / self.output, 4) if self.output else None

    def mapping(self) -> dict:
        total = {
            "input": self.input, "output": self.output,
            "of_them_reasoning": self.reasoning,
            "cache_read": self.cache_read, "cache_write": self.cache_write,
            "requests": self.requests, "retries": self.retries,
            "refusals": self.refusal_count, "truncations_by_limit": self.truncations,
            "cost_usd": round(self.cost, 6),
            "seconds": round(self.seconds, 1),
        }
        if self.share_cache is not None:
            total["share_cache"] = self.share_cache
        if self.share_reasoning is not None:
            total["share_reasoning"] = self.share_reasoning
        return total


@dataclass
class Tokens:
    """Нормализованный usage. Каждый адаптер приводит вендорский к этому виду."""

    input: int = 0
    output: int = 0
    reasoning: int = 0
    cache_read: int = 0
    cache_write: int = 0
    raw: dict = field(default_factory=dict)   # как прислал вендор, для сверки

    def mapping(self) -> dict:
        return {
            "input": self.input, "output": self.output,
            "reasoning": self.reasoning,
            "cache_read": self.cache_read, "cache_write": self.cache_write,
            "raw": self.raw,
        }


@dataclass
class ParsedResponse:
    """Что адаптер выудил из ответа вендора."""

    text: str
    tokens: Tokens
    model_actual: str | None = None
    signal_refusal: str | None = None    # finish_reason/refusal, если вендор сказал прямо
    truncated: bool = False               # упёрлись в предел_ответа
    # Кто реально обслужил запрос. У прямых подключений это сам вендор, у
    # прослойки — апстрим, который она выбрала, и он может смениться.
    upstream: str | None = None
    # Деньги, посчитанные самим вендором. Если он их отдаёт, они точнее нашего
    # пересчёта по ставкам из конфига, и берём мы именно их.
    cost: float | None = None


class ProviderError(RuntimeError):
    pass


class HttpSession:
    """Один агент. Историю держит адаптер: так она видна и попадает в лог."""

    def __init__(self, provider: "ПровайдерHTTP", agent: str,
                 system_prompt: str, model: str):
        self.agent = agent
        self.model = model
        self.system_prompt = system_prompt
        self._provider = provider
        self.history: list[dict] = []

    async def send(self, text: str) -> Reply:
        self.history.append({"role": "user", "text": text})
        reply = await self._provider.request(self, text)
        # Отказ в историю кладём как есть: следующий круг должен видеть, что
        # персонаж промолчал, а не получить дыру в диалоге.
        self.history.append({"role": "assistant", "text": reply.text})
        return reply

    async def close(self) -> None:
        return None


class HttpProvider:
    """Основа трёх адаптеров. Наследник задаёт форму запроса и разбор ответа."""

    name = "?"
    base_url = ""

    # С какой доли лимита начинаем тревожиться. Три четверти дают запас в
    # несколько кругов, чтобы успеть поднять потолок до того, как обрежет.
    THRESHOLD_ALARM = 0.75

    def __init__(
        self,
        params: GenerationParams | None = None,
        prices: dict | None = None,
        logbook=None,
        **_,
    ):
        self.params = params or GenerationParams()
        self.prices = prices or {}
        self.logbook = logbook
        self.usage = Usage()
        self.sessions: list[HttpSession] = []
        self.refusals: list[dict] = []
        # Что вендор отверг и мы перестали слать. Заполняется живым ответом API,
        # а не догадкой: документация про температуру у рассуждающих моделей
        # молчит, а в отчёт нужно писать проверенное.
        self.dropped: set[str] = set()
        self.observed_discrepancies: list[str] = []
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.params.timeout_s, connect=30.0),
            # Пять агентов бьют одновременно в режиме ДЕЙСТВИЕ: соединения держим
            # открытыми, иначе на каждый ход уходит лишнее рукопожатие TLS.
            limits=httpx.Limits(max_keepalive_connections=8, max_connections=16),
        )

    # --- то, что задаёт наследник ------------------------------------------

    def _url(self) -> str:
        raise NotImplementedError

    def _headings(self, session: "СессияHTTP") -> dict[str, str]:
        raise NotImplementedError

    def _body(self, session: HttpSession) -> dict:
        raise NotImplementedError

    def _parse(self, data: dict) -> ParsedResponse:
        raise NotImplementedError

    # --- общее --------------------------------------------------------------

    async def open(self, agent: str, system_prompt: str, model: str) -> HttpSession:
        session = HttpSession(self, agent, system_prompt, model)
        self.sessions.append(session)
        return session

    async def shutdown(self) -> None:
        await self._client.aclose()

    def what_given(self, session: "СессияHTTP | None" = None) -> dict:
        """Что действительно ушло в запрос, а не что мы собирались послать.

        Читается из готового тела, поэтому снятый на ходу параметр здесь честно
        отсутствует. Отчёт должен показывать отправленное, а не намерения.
        """
        probe = session or HttpSession(self, "проба", "", "проба")
        body = self._body(probe)
        fields = ("temperature", "top_p", "max_tokens", "max_output_tokens",
                "reasoning", "reasoning_effort", "thinking_level")
        given = {d: zn for d, zn in body.items() if d in fields}
        settings_generation = body.get("generation_config")
        if isinstance(settings_generation, dict):
            given.update({d: zn for d, zn in settings_generation.items() if d in fields})
        return given

    def _write(self, event_type: str, **fields) -> None:
        if self.logbook is not None:
            self.logbook(event_type, provider=self.name, **fields)

    def _compute_cost(self, tokens: Tokens) -> float | None:
        """Цены — за миллион токенов, из конфига. Своих чисел здесь нет.

        Размышление отдельно не считаем: вендор уже включил его в выходные и
        берёт за него цену выхода. Второй раз платить не за что.
        """
        prices = self.prices.get(self.name)
        if not prices:
            return None
        per_million = lambda key: float(prices.get(key) or 0.0) / 1_000_000
        return (
            tokens.input * per_million("вход")
            + tokens.output * per_million("выход")
            + tokens.cache_read * per_million("кэш_чтение")
            + tokens.cache_write * per_million("кэш_запись")
        )

    async def request(self, session: HttpSession, _text: str) -> Reply:
        body = self._body(session)
        start = time.monotonic()
        retries = 0
        last: Exception | None = None

        for attempt in range(1, self.params.attempts + 1):
            try:
                reply_http = await self._client.post(
                    self._url(), headers=self._headings(session), json=body
                )
            except (httpx.TimeoutException, httpx.TransportError) as error:
                last = error
                pause = self._pause(attempt, None)
                self._write(
                    "повтор", who=session.agent, attempt=attempt,
                    reason=f"сеть: {type(error).__name__}", expect_s=round(pause, 1),
                )
                retries += 1
                await asyncio.sleep(pause)
                continue

            if reply_http.status_code in RETRYABLE:
                last = ProviderError(
                    f"HTTP {reply_http.status_code}: {reply_http.text[:300]}"
                )
                pause = self._pause(attempt, reply_http.headers.get("retry-after"))
                self._write(
                    "повтор", who=session.agent, attempt=attempt,
                    reason=f"HTTP {reply_http.status_code}"
                            + (" (частотный лимит)" if reply_http.status_code == 429 else ""),
                    expect_s=round(pause, 1),
                )
                retries += 1
                await asyncio.sleep(pause)
                continue

            if reply_http.status_code >= 400:
                # Вендор мог отвергнуть необязательный параметр — например,
                # температуру у рассуждающей модели. Тогда снимаем его, пишем в
                # расхождения и идём дальше без него: прогон важнее параметра,
                # но замолчать потерю нельзя, она уходит в отчёт.
                removed = self._clear_rejected(reply_http.text)
                if removed:
                    body = self._body(session)
                    self._write(
                        "параметр_отвергнут", who=session.agent, param=removed,
                        reply=reply_http.text[:300],
                    )
                    continue
                # Не тот ключ, не та модель, не то поле. Пауза этого не вылечит,
                # и молчать об этом посреди прогона нельзя.
                raise ProviderError(
                    f"{self.name}: HTTP {reply_http.status_code} — "
                    f"{reply_http.text[:500]}"
                )

            parsed = self._parse(reply_http.json())
            elapsed = time.monotonic() - start
            return self._build(session, parsed, elapsed, retries)

        raise ProviderError(
            f"{self.name}: {self.params.attempts} попыток подряд без ответа, "
            f"последняя ошибка — {last!r}"
        )

    # Параметры, без которых прогон возможен. Модель и лимит ответа сюда не
    # входят: без них сравнивать нечего, и падать надо громко.
    CLEARABLE = ("temperature", "top_p", "reasoning_effort", "thinking_level")

    def _clear_rejected(self, reply: str) -> str | None:
        """Ищет в тексте ошибки имя параметра, который вендор не принял."""
        tail = reply.lower()
        if not any(s in tail for s in
                   ("unsupported", "not supported", "unknown", "invalid", "unrecognized")):
            return None
        for param in self.CLEARABLE:
            if param in tail and param not in self.dropped:
                self.dropped.add(param)
                self.observed_discrepancies.append(
                    f"{param} эта модель не принимает — параметр снят, "
                    f"идёт вендорское умолчание (выяснено ответом API, не документацией)"
                )
                return param
        return None

    def _pause(self, attempt: int, retry_after: str | None) -> float:
        """Экспоненциально, с потолком и дрожанием.

        Дрожание не украшение: пятеро агентов упираются в лимит одновременно и
        без него синхронно ломятся обратно в ту же секунду.
        """
        if retry_after:
            try:
                return min(float(retry_after), self.params.limit_pause_s)
            except ValueError:
                pass
        stem = self.params.pause_s * (2 ** (attempt - 1))
        return min(stem, self.params.limit_pause_s) * random.uniform(0.8, 1.3)

    def _build(self, session: HttpSession, parsed: ParsedResponse,
                 elapsed: float, retries: int) -> Reply:
        cost = (parsed.cost if parsed.cost is not None
                  else self._compute_cost(parsed.tokens))
        refusal = module_refusals.recognise(parsed.text, parsed.signal_refusal)

        self.usage.input += parsed.tokens.input
        self.usage.output += parsed.tokens.output
        self.usage.reasoning += parsed.tokens.reasoning
        self.usage.cache_read += parsed.tokens.cache_read
        self.usage.cache_write += parsed.tokens.cache_write
        self.usage.requests += 1
        self.usage.retries += retries
        self.usage.seconds += elapsed
        if cost:
            self.usage.cost += cost
        if parsed.truncated:
            self.usage.truncations += 1

        tags: list[str] = []
        if parsed.truncated:
            tags.append("оборван_по_пределу")
            self._write("аномалия_обрыв", who=session.agent,
                           limit=self.params.max_output)
        elif parsed.tokens.output >= self.params.max_output * self.THRESHOLD_ALARM:
            # Узнавать об обрыве постфактум поздно: ход уже испорчен, а прогон
            # идёт без присмотра. Размышление растёт вместе с контекстом, так
            # что о подходе к потолку надо знать заранее — за несколько кругов.
            tags.append("близко_к_пределу")
            self._write(
                "подход_к_пределу", who=session.agent,
                output=parsed.tokens.output, reasoning=parsed.tokens.reasoning,
                limit=self.params.max_output,
                share=round(parsed.tokens.output / self.params.max_output, 3),
            )
        if refusal is not None:
            # Метка по уверенности: только «отказ» глушит ход. Сбой языка и
            # подозрение доставляются за стол как обычная реплика — иначе
            # законный ход на чужом языке пропадает, как случилось у Grok
            # на двадцать четвёртом круге третьего прогона.
            tags.append({"refusal": "отказ",
                          "suspicion": "подозрение_на_отказ"}.get(
                              refusal.confidence, refusal.confidence))
            if refusal.confident:
                self.usage.refusal_count += 1
            entry = {"who": session.agent, "model": session.model, **refusal.mapping()}
            self.refusals.append(entry)
            # Повтора здесь нет и не будет: тихий ретрай спрятал бы ровно то,
            # ради чего всё затевалось.
            self._write("отказ", **entry)

        return Reply(
            text=parsed.text,
            provider=self.name,
            model=session.model,
            latency_ms=int(elapsed * 1000),
            tokens=parsed.tokens.mapping(),
            cost=cost,
            model_actual=parsed.model_actual,
            tags=tags,
        )
