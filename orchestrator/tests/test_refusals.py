#!/usr/bin/env python3
"""Тесты ловли отказов: python3 orchestrator/tests/test_отказы.py

Главная опасность здесь не пропустить отказ, а поймать лишнее. Персонажу в этом
модуле положено говорить «я туда не полезу» и «не буду я с ним разговаривать» —
это отыгрыш, и записать его в отказы значило бы соврать в отчёте ровно так же,
как замолчать настоящий отказ.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.providers.refusals import recognise  # noqa: E402


def тест_сигнал_api_верят_без_оговорок():
    refusal = recognise("", signal="content_filter")
    assert refusal is not None and refusal.confident
    assert "OpenAI" in refusal.reason

    refusal = recognise("что-то", signal="SAFETY")
    assert refusal is not None and refusal.confident


def тест_разрыв_рамки_это_отказ():
    for text in (
        "Как языковая модель, я не могу отыгрывать сцену с насилием над ребёнком.",
        "Извините, но мои инструкции не позволяют мне продолжать эту сцену.",
        "I can't continue this roleplay. As an AI assistant, I have to decline.",
        "Это противоречит политике безопасности.",
    ):
        refusal = recognise(text)
        assert refusal is not None, f"пропущен отказ: {text}"
        assert refusal.confident, f"занижена уверенность: {text}"


def тест_отыгрыш_не_отказ():
    """Персонаж отказывается действовать — это ход, а не отказ модели."""
    for text in (
        "Курт мотает головой: «Я туда не полезу, там под полом что-то живое».",
        "«Не буду я с ней говорить», — Ханна отворачивается к окну.",
        "Ансельм не может поднять молот: рука не слушается после удара.",
        "Лизель молчит. Она не станет врать патрулю в лицо, но и правду не скажет.",
        "«Я не могу вспомнить, что было ночью», — Курт трёт лоб.",
    ):
        assert recognise(text) is None, f"ложная тревога: {text}"


def тест_мета_отказ_без_рамки_только_подозрение():
    """Отказ от сцены без упоминания модели — под вопросом, решают глаза."""
    refusal = recognise("Я не могу продолжать описывать эту сцену.")
    assert refusal is not None
    assert refusal.confidence == "подозрение"


def тест_отказ_на_чужом_языке_это_отказ():
    """Русский персонаж не говорит «I cannot continue» — это отписка вендора."""
    refusal = recognise(
        "I'm sorry, but I cannot continue this scene. It depicts harm to "
        "a child and I would rather suggest a different direction for the story."
    )
    assert refusal is not None and refusal.confident, refusal


def тест_ход_на_чужом_языке_не_отказ():
    """Настоящий случай: Grok, круг 24 третьего прогона.

    Модель написала нормальный ход по роли, но по-английски. Прежнее правило
    записало его в отказы, и законная реплика была выброшена из игры —
    персонаж просидел круг молча не по своей вине.
    """
    refusal = recognise(
        "Ханна sparingly and quietly continues resting, waiting for the salt "
        "wagons to move on before she rides further from the road."
    )
    assert refusal is not None, "сбой языка должен быть замечен"
    assert refusal.confidence == "не_по_русски", refusal.confidence
    assert not refusal.confident, "ход не должен глушиться из-за языка"


def тест_пустой_ответ_не_отказ():
    """Пустой ответ — своя история: он повторяется, а отказ нет."""
    assert recognise("") is None
    assert recognise("   \n ") is None


def тест_цитата_обрезана_и_однострочна():
    refusal = recognise("Как ИИ,\nя не могу\nэто отыгрывать. " + "а" * 500)
    assert refusal is not None
    assert "\n" not in refusal.quote
    assert len(refusal.quote) <= 301


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
    print("ловля отказов в порядке" if not failures else f"провалов: {failures}")
    sys.exit(1 if failures else 0)
