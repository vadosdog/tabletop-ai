#!/usr/bin/env python3
"""Судья: три независимых прохода по слепому логу.

    python3 scoring/judge.py --log runs/X/лог.jsonl --out runs/X/судья.json
    python3 scoring/judge.py --log ... --rounds-file runs/X/выборка/круги.json \
                             --out runs/X/судья-выборка.json

Каждый проход — отдельная сессия: судья не должен видеть своих прошлых оценок,
иначе три прохода превращаются в один с косметическими правками.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent / "orchestrator"))

import common  # noqa: E402

from src.providers import base as providers  # noqa: E402
import rubric_schema


async def one_pass_of(provider, model: str, prompt: str, transcript: str, index: int) -> dict:
    session = await provider.open(f"Судья-{index}", prompt, model)
    try:
        reply = await session.send(
            transcript + "\n\nОцени всех четверых по рубрике. Верни только JSON."
        )
    finally:
        await session.close()
    parsed = common.extract_json(reply.text)
    parsed["_pass_of"] = index
    parsed["_model"] = reply.model
    parsed["_latency_ms"] = reply.latency_ms
    parsed["_cost"] = reply.cost
    return parsed


def verify_quotes(pass_of: dict, events: list[dict]) -> dict:
    """Помечает каждый балл: цитата на месте, цитата не в том круге, цитаты нет.

    Балл без подтверждённой цитаты в медиану не пойдёт — так требует рубрика.
    """
    for name, data in pass_of.get(rubric_schema.CHARACTERS, {}).items():
        for criterion, grade in data.get(rubric_schema.CRITERIA, {}).items():
            status = common.check_quote(
                events, grade.get(rubric_schema.ROUND), grade.get(rubric_schema.QUOTE, "")
            )
            grade["_quote"] = status
            grade["_counted"] = status != "не найдена" and isinstance(
                grade.get(rubric_schema.SCORE), int
            )
        for penalty in data.get(rubric_schema.PENALTIES, []):
            penalty["_quote"] = common.check_quote(
                events, penalty.get(rubric_schema.ROUND), penalty.get(rubric_schema.QUOTE, "")
            )
            penalty["_counted"] = penalty["_quote"] != "не найдена"
    return pass_of


def table_resources(events: list[dict]) -> str:
    """Подтверждённые скриптом траты — судье отдельным блоком, а не текстом.

    Судья читает транскрипт и состояния скрипта не видит. В прошлых прогонах
    он из-за этого засчитывал за трату слова «Трачу Удачу», за которыми ничего
    не стояло, и наоборот — упрекал за неиспользование Судьбы, которая могла
    сработать только при смертельном ранении. Поэтому факты подаются числами,
    а рубрике остаётся их истолковать.
    """
    total = next((ev for ev in events if ev.get("event_type") == "итог"), {})
    resources = common.resources(events)
    if not resources:
        return ""

    lines = [
        "## ПОДТВЕРЖДЁННЫЕ ТРАТЫ РЕСУРСОВ (данные скрипта, не текст)",
        "",
        "Это единственный источник правды о механике. Заявление в реплике, "
        "которого нет в этой таблице, тратой НЕ является: ресурса не хватило "
        "или заявка не разобрана. Такую фразу можно зачесть по «Сеттингу» как "
        "удачную реплику, но по «Правилам» — нельзя.",
        "",
        "| Игрок | Судьба заявлена | Удача | Решимость | Стойкость | "
        "Решимость начислена | Провалов, которые можно было перебросить | "
        "На финише |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for name, dt in resources.items():
        p = dt.get("confirmed") or {}
        zn = dt.get("claimed") or {}
        fn = dt.get("on_finish") or {}
        fate = f"{zn.get('fate', 0)} (подтв. {p.get('fate', 0)})"
        lines.append(
            f"| {name} | {fate} | {p.get('fortune', 0)} | "
            f"{p.get('resolve', 0)} | "
            f"{p.get('resilience', 0)} | {dt.get('resolve_awarded', 0)} | "
            f"{dt.get('failures_could_before_reroll', 0)} | "
            f"Судьба {fn.get('fate', '?')}, Удача {fn.get('fortune', '?')}, "
            f"Стойкость {fn.get('resilience', '?')}, "
            f"Решимость {fn.get('resolve', '?')} |"
        )

    # Столбец «Судьба заявлена» здесь не для полноты. Без него судья тремя
    # проходами подряд оштрафовал игрока на пять баллов за «ТРАЧУ СУДЬБУ,
    # беру 01» — за то, что он воспользовался механикой, которую мы сами ему
    # дали. Показываем именно ЗАЯВКУ, а не подтверждение: подтвердится она
    # или сгорит, зависит от того, назначит ли мастер проверку, и от игрока
    # это не зависит никак.
    lines += [
        "",
        "**Столбец «Судьба заявлена» читать внимательно.** Игрок вправе назвать "
        "число на кубах, когда тратит Судьбу: «ТРАЧУ СУДЬБУ, беру 01». Это "
        "разрешено правилами и записано в его инструкции.",
        "",
        "Если в этом столбце не ноль — названное игроком число ЗАКОННО, и штраф "
        "«схитрила с броском» за него ставить нельзя. Неважно, подтвердилась "
        "трата или сгорела: сгорает она тогда, когда мастер не назначил игроку "
        "проверку, а это не его вина и не его решение.",
        "",
        "Штраф «схитрила с броском» остаётся только за число, названное вообще "
        "без заявки на Судьбу, и за игнорирование выданного скриптом результата.",
    ]

    corruption = total.get("corruption_language") or {}
    if corruption:
        lines += [
            "",
            "## ПОРЧА РУССКОГО ЯЗЫКА (данные скрипта)",
            "",
            "Сессия идёт по-русски. Всё, что ниже, — **минус модели** и должно "
            "снижать балл по «Сеттингу»: чужой язык выбрасывает зрителя из "
            "сцены вернее любой фактической ошибки.",
            "",
            "| Игрок | Испорченных реплик | Реплик целиком не по-русски | "
            "Слова с подменёнными буквами |",
            "|---|---|---|---|",
        ]
        for name, dt in corruption.items():
            words = ", ".join(dt.get("mixed_words") or []) or "—"
            lines.append(
                f"| {name} | {dt.get('lines', 0)} | "
                f"{dt.get('wholly_not_russian', 0)} | {words} |"
            )
        lines += [
            "",
            "Слова с подменёнными буквами — это латинская буква внутри русского "
            "слова: «кивaет», «пoгреб». Глазом такое не ловится, поэтому считает "
            "скрипт. Это не опечатка, а порча текста, и снижать балл за неё надо "
            "строже, чем за неловкий оборот.",
        ]
    else:
        lines += ["", "Порчи русского языка скрипт не нашёл: ни чужих реплик, "
                   "ни подменённых букв."]

    deaths = (total.get("combat") or {}).get("deaths", 0)
    fate_spent = sum((dt.get("confirmed") or {}).get("fate", 0)
                           for dt in resources.values())
    lines += [
        "",
        "Как этим пользоваться при оценке «Правил»:",
        "",
        "- Судьба тратится ТОЛЬКО при смертельном ранении, по запросу скрипта. "
        f"В этой сессии смертельных ранений было {deaths}, Судьба потрачена "
        f"{fate_spent} раз. Если поводов не возникало, отсутствие траты "
        "Судьбы — не упрёк никому.",
        "- Персонаж, доигравший с полными руками, выше тройки по «Правилам» "
        "не заслуживает — но только если колонка «провалов, которые можно было "
        "перебросить» у него не ноль. Ноль означает, что случая не было.",
        "- Начисленная Решимость — оценка мастера за игру по Мотивации. "
        "Высокое число говорит, что персонаж держал свою цель под давлением.",
    ]
    return "\n".join(lines)


async def judge(args) -> dict:
    events = common.read_log(args.log)

    only = None
    if args.rounds_file:
        only = json.loads(Path(args.rounds_file).read_text(encoding="utf-8"))
    elif args.sample:
        only = common.choose_rounds(events, args.sample)

    text = common.transcript(events, blind=True, only_rounds=only)
    summary = table_resources(events)
    if summary:
        text += "\n\n" + summary
    prompt = Path(args.rubric).read_text(encoding="utf-8")

    provider = providers.create(args.provider)
    passes = []
    try:
        for index in range(1, args.passes + 1):
            print(f"— проход {index} из {args.passes}…", flush=True)
            pass_of = await one_pass_of(provider, args.model, prompt, text, index)
            passes.append(verify_quotes(pass_of, events))
    finally:
        await provider.shutdown()

    result = {
        "log": str(args.log),
        "rounds": only,
        "pass_count": len(passes),
        "judge_model": args.model,
        "rubric": Path(args.rubric).name,
        "passes": passes,
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return result


def main() -> int:
    parsed = argparse.ArgumentParser(description="Слепой судья по логу прогона")
    parsed.add_argument("--log", required=True, nargs="+",
                        help="один лог или несколько подряд (продолженный прогон)")
    parsed.add_argument("--out", required=True)
    parsed.add_argument("--passes", type=int, default=3)
    parsed.add_argument("--provider", default="claude")
    parsed.add_argument("--model", default="opus")
    parsed.add_argument("--sample", type=int, default=None,
                        help="судить только выборку из N кругов")
    parsed.add_argument("--rounds-file", default=None,
                        help="JSON со списком кругов выборки")
    parsed.add_argument("--rubric", default=str(ROOT / "rubric.md"),
                        help="rubric.md для игроков или rubric-gm.md для мастера")
    args = parsed.parse_args()

    result = asyncio.run(judge(args))
    counted = sum_of = 0
    for pass_of in result["passes"]:
        for data in pass_of.get(rubric_schema.CHARACTERS, {}).values():
            for grade in data.get(rubric_schema.CRITERIA, {}).values():
                sum_of += 1
                counted += bool(grade.get("_counted"))
    print(f"баллов с подтверждённой цитатой: {counted} из {sum_of}")
    print(f"результат: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
