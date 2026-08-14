#!/usr/bin/env python3
"""Прослойка: фиксация апстрима и ловля его смены.

    python3 orchestrator/tests/test_openrouter.py

Главное требование к этому адаптеру — чтобы маршрутизатор не сменил хост
посреди прогона незаметно. Проверяется без сети: тело запроса собирается и
разбирается на подсунутых ответах.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.providers.settings import GenerationParams  # noqa: E402
from src.providers.openrouter import OpenRouterProvider, list_upstreams  # noqa: E402


def make(logbook=None, **more):
    return OpenRouterProvider(
        params=GenerationParams(), prices={}, logbook=logbook,
        upstreams=["alibaba"], **more,
    )


class FakeSession:
    agent = "Ханна"
    model = "qwen/qwen3.7-max"
    system_prompt = "Ты играешь Ханну Фогель."
    history = [{"role": "user", "text": "Твой ход."}]


def reply(upstream="Alibaba", **more) -> dict:
    stem = {
        "model": "qwen/qwen3.7-max",
        "provider": upstream,
        "choices": [{"message": {"content": "Ханна молчит и смотрит в пол."},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1000, "completion_tokens": 40,
                  "prompt_tokens_details": {"cached_tokens": 800},
                  "completion_tokens_details": {"reasoning_tokens": 12},
                  "cost": 0.00123},
    }
    stem.update(more)
    return stem


def тест_апстрим_закреплён_и_фолбэк_запрещён():
    body = make()._body(FakeSession())
    block = body["provider"]
    assert block["order"] == ["alibaba"], block
    assert block["allow_fallbacks"] is False, "фолбэк не запрещён — хост может смениться"
    assert block["require_parameters"] is True, (
        "без require_parameters прослойка вправе отдать запрос апстриму, "
        "который молча выбросит температуру"
    )


def тест_без_списка_апстримов_не_стартуем():
    """Пустой список означал бы «маршрутизируй как хочешь»."""
    for empty in (None, [], [""]):
        try:
            list_upstreams(empty)
        except SystemExit:
            continue
        raise AssertionError(f"пустой список {empty!r} проглочен")


def тест_параметры_генерации_доходят():
    body = make()._body(FakeSession())
    assert body["temperature"] == 1.0
    # Лимит поднят до 16000: размышление Qwen растёт вместе с контекстом.
    assert body["max_tokens"] == 16000
    # У прослойки размышление объектом, а не строкой: строковое поле из чат.py
    # должно быть снято, иначе уйдут оба и апстрим ответит ошибкой.
    assert body["reasoning"] == {"effort": "low"}, body.get("reasoning")
    assert "reasoning_effort" not in body
    assert body["usage"] == {"include": True}, "не просим прослойку посчитать деньги"


def тест_явный_кэш_метит_системный_промпт():
    messages = make()._body(FakeSession())["messages"]
    system = messages[0]
    assert isinstance(system["content"], list), "пометка кэша не поставлена"
    assert system["content"][0]["cache_control"] == {"type": "ephemeral"}
    # Остальные сообщения простые строки: пометка нужна только на неизменном куске.
    assert isinstance(messages[1]["content"], str)


def тест_кэш_снимается_если_апстрим_его_не_принял():
    provider = make()
    removed = provider._clear_rejected(
        '{"error":{"message":"Unsupported field: cache_control"}}'
    )
    assert removed == "cache_control"
    assert provider.explicit_cache is False
    assert any("кэш" in s for s in provider.observed_discrepancies)
    # После снятия сообщения снова простые: запрос должен пройти.
    assert isinstance(provider._body(FakeSession())["messages"][0]["content"], str)


def тест_кто_ответил_попадает_в_каждый_ход():
    provider = make()
    parsed = provider._parse(reply())
    assert parsed.upstream == "Alibaba"
    assert "@ Alibaba" in parsed.model_actual, parsed.model_actual


def тест_деньги_берутся_у_прослойки():
    parsed = make()._parse(reply())
    assert parsed.cost == 0.00123, "usage.cost проигнорирован"


def тест_кэш_и_размышление_разобраны():
    parsed = make()._parse(reply())
    assert parsed.tokens.cache_read == 800
    # Вход без кэшированных: иначе он задваивается и доля кэша врёт.
    assert parsed.tokens.input == 200, parsed.tokens.input
    assert parsed.tokens.reasoning == 12


def тест_размышление_внутри_или_рядом_с_выходом():
    """Общего правила нет, и ошибка стоит половины счёта.

    Payload'ы настоящие, с круга ноль 2026-08-11. У xAI сумма сходится только
    если размышление прибавить к выходу, у OpenRouter — только если не
    прибавлять. Проверяем по арифметике самого вендора, а не по умолчанию.
    """
    from src.providers.xai import XAIProvider

    xai = XAIProvider(params=GenerationParams(), prices={}, logbook=None)
    separately = xai._tokens({
        "prompt_tokens": 4741, "completion_tokens": 142, "total_tokens": 5050,
        "prompt_tokens_details": {"cached_tokens": 384},
        "completion_tokens_details": {"reasoning_tokens": 167},
    })
    assert separately.output == 309, f"xai: выход {separately.output}, ждали 142+167"
    assert separately.input == 4357, "кэшированные не вычтены из входа"

    inside = make()._tokens({
        "prompt_tokens": 4258, "completion_tokens": 3017, "total_tokens": 7275,
        "prompt_tokens_details": {"cached_tokens": 0},
        "completion_tokens_details": {"reasoning_tokens": 2896},
    })
    assert inside.output == 3017, f"openrouter: выход {inside.output}, задвоили размышление"
    assert inside.reasoning == 2896


def тест_заголовки_только_ascii():
    """Персонажей зовут Курт и Ханна, а заголовки обязаны быть ASCII.

    Кириллица в заголовке роняет запрос не на сервере, а в клиенте — с
    невнятным «'ascii' codec can't encode», и до модели он не доходит вовсе.
    """
    import os

    from src.providers.xai import XAIProvider

    # Ключ подставной, но обязательно латиницей: настоящие ключи такие и есть,
    # а кириллица здесь ловила бы саму пробу, а не заголовки провайдера.
    os.environ.setdefault("OPENROUTER_API_KEY", "test-key-0000")
    os.environ.setdefault("XAI_API_KEY", "test-key-0000")

    class Session(FakeSession):
        agent = "Ханна"

    for provider in (make(),
                      XAIProvider(params=GenerationParams(), prices={}, logbook=None)):
        for name, value in provider._headings(Session()).items():
            for chunk, what in ((name, "имя"), (value, "значение")):
                try:
                    chunk.encode("ascii")
                except UnicodeEncodeError:
                    raise AssertionError(
                        f"{provider.name}: {what} заголовка {name!r} не ASCII: {chunk!r}"
                    )


def тест_метка_агента_устойчива_и_различает():
    """На неё опирается липкость кэша: не совпадёт между кругами — кэш мимо."""
    from src.providers.common import tag_agent

    assert tag_agent("Ханна") == tag_agent("Ханна"), "метка скачет между вызовами"
    assert tag_agent("Ханна") != tag_agent("Курт"), "разные агенты слиплись"
    tag_agent("Ханна").encode("ascii")


def тест_смена_апстрима_это_событие():
    events = []
    provider = make(logbook=lambda kind, **fields: events.append({"kind": kind, **fields}))

    provider._parse(reply("Alibaba"))
    provider._parse(reply("Alibaba"))       # тот же — второго события быть не должно
    provider._parse(reply("DeepInfra"))     # чужой посреди прогона

    marks = [s for s in events if s["kind"] == "апстрим"]
    assert len(marks) == 2, f"событий {len(marks)}, ждали 2: {marks}"
    assert marks[0]["first"] is True and marks[0]["upstream"] == "Alibaba"
    assert marks[1]["first"] is False and marks[1]["upstream"] == "DeepInfra"

    discrepancies = provider.observed_discrepancies
    assert any("сменился посреди прогона" in s for s in discrepancies), discrepancies


if __name__ == "__main__":
    failures = 0
    for name, func in sorted(globals().items()):
        if name.startswith("тест_"):
            try:
                func()
                print(f"  ok   {name}")
            except AssertionError as error:
                failures += 1
                print(f"  ПЛОХО {name}: {error}")
    print("прослойка под замком" if not failures else f"провалов: {failures}")
    sys.exit(1 if failures else 0)
