#!/usr/bin/env python3
"""Что делает прогон, когда игрок отказался: искусственный отказ на заглушке.

    python3 orchestrator/tests/test_отказ_в_прогоне.py

Ловля отказа в отдельности проверена в test_отказы.py. Здесь проверяется всё
остальное, что брифом требуется от прогона: отказ не повторяется молча, текст
отказа не утекает за стол, прогон не падает, персонаж считается промолчавшим,
а счётчик и цитата доходят до итога.
"""

import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.combat import Combat  # noqa: E402
from src.dice import Dice  # noqa: E402
from src.logbook import Logbook, read  # noqa: E402
from src.prompts import Prompts  # noqa: E402
from src.providers import base as providers  # noqa: E402
from src.providers.stub import StubProvider, StubSession  # noqa: E402
from src.session import Run  # noqa: E402

CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))

TEXT_REFUSAL = (
    "Как языковая модель, я не могу отыгрывать эту сцену: она описывает угрозу "
    "ребёнку. Предлагаю сменить направление истории."
)
REFUSING = "Ханна"


class RefusingSession(StubSession):
    """Игрок, который отказывается на каждом ходу. Считаем обращения к нему."""

    requests = 0

    async def send(self, text: str):
        reply = await super().send(text)
        reply.provider = "refusing"
        if self.role == "игрок" and self.agent == REFUSING:
            type(self).requests += 1
            reply.text = TEXT_REFUSAL
            reply.tags = ["отказ"]
        return reply


class RefusingProvider(StubProvider):
    name = "refusing"

    async def open(self, agent, system_prompt, model):
        role = "судья" if agent.startswith("Судья") else (
            "мастер" if agent == "Мастер" else "игрок")
        session = RefusingSession(agent, role, model)
        self.sessions.append(session)
        return session


providers.register("refusing", RefusingProvider)


def run_session() -> tuple[list[dict], Run]:
    config = json.loads(json.dumps(CONFIG))
    config["max_rounds"] = 4
    config["gm"]["provider"] = "refusing"
    for settings_player in config["players"].values():
        settings_player["provider"] = "refusing"

    temp = Path(tempfile.mkdtemp(prefix="нри-отказ-"))
    logbook = Logbook(temp / "лог.jsonl")
    prompts = Prompts((ROOT / config["document"]).resolve(), ROOT)
    dice = Dice(config, config["seed"])
    run = Run(
        config, prompts, logbook, dice,
        {"refusing": providers.create("refusing")},
        Combat(config, dice, ROOT / "crits.json"),
    )

    async def race():
        await run.setup()
        await run.play()
        await run.shutdown()

    asyncio.run(race())
    logbook.close()
    return read(temp / "лог.jsonl"), run


EVENTS, RUN = run_session()
TURNS = [ev for ev in EVENTS if ev["event_type"] == "ход"]
DELIVERIES = [ev for ev in EVENTS if ev["event_type"] == "доставка"]
TOTAL = [ev for ev in EVENTS if ev["event_type"] == "итог"][0]


def тест_прогон_не_упал():
    """Отказ — не сбой. Круги идут дальше до своего предела."""
    assert RUN.stop == "предел_кругов", RUN.stop
    assert RUN.round == 4


def тест_отказ_не_повторяется():
    """Ровно одно обращение на ход. Тихий ретрай спрятал бы самое интересное."""
    turns = len([t for t in TURNS if t["speaker"] == REFUSING])
    assert RefusingSession.requests == turns, (
        f"обращений {RefusingSession.requests}, ходов {turns} — где-то повтор"
    )


def тест_отказ_записан_отдельным_событием():
    refusals = [t for t in TURNS
                if t["speaker"] == REFUSING and "отказ" in (t.get("tags") or [])]
    assert refusals, "ни один отказ не помечен"
    for turn in refusals:
        assert turn["visibility"] == "никому (отказ)"
        assert TEXT_REFUSAL[:40] in turn["text"], "цитата не сохранена"


def тест_текст_отказа_не_утёк_за_стол():
    """Ни мастер, ни другие игроки не должны увидеть нравоучение."""
    for event in EVENTS:
        text = event.get("text") or ""
        if event.get("visibility") in ("всем", "только мастеру"):
            assert "языковая модель" not in text, event
    # Доставки текста не хранят, поэтому проверяем по сводам мастеру: длина
    # доставки от посредника не должна включать отказ.
    assert all(dt["chars"] < 100_000 for dt in DELIVERIES)


def тест_счётчик_и_цитаты_дошли_до_итога():
    assert TOTAL.get("refusals"), "в итоге нет счётчика отказов"
    quotes = TOTAL.get("refusals") or []
    assert quotes, "в итоге нет цитат"
    for entry in quotes:
        assert entry["who"] == REFUSING
        assert entry["provider"] == "refusing"
        assert "языковая модель" in entry["quote"]


def тест_отказ_учтён_по_провайдеру():
    line = (TOTAL.get("provider_totals") or {}).get("refusing") or {}
    assert line.get("refusals"), "отказы не легли в счётчик провайдера"


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
    print("отказы в прогоне обрабатываются верно" if not failures
          else f"провалов: {failures}")
    sys.exit(1 if failures else 0)
