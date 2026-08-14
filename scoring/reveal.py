#!/usr/bin/env python3
"""Раскрытие ваншота: сколько загадок партия вскрыла и чем всё кончилось.

    python3 scoring/reveal.py --log runs/X/лог.jsonl --out runs/X/раскрытие.json

Это **не судья**. Судья слеп и модуля не читает — иначе он начнёт оценивать
совпадение с разгадкой вместо игры. Раскрытие считает отдельный оценщик, и ему,
наоборот, дают всю скрытую правду: без неё вопрос «раскрыли или нет» не решить.

Счёт общий на всю партию: это результат стола, а не вина конкретного игрока.
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

from src.prompts import Prompts  # noqa: E402
from src.providers import base as providers  # noqa: E402
import rubric_schema

PROMPT = """Ты — аналитик, разбирающий закончившуюся сессию настольной ролевой игры.

Тебе дают две вещи: скрытую правду модуля, которую знал только мастер, и полный
лог сессии. Твоя работа — определить, что партия успела раскрыть, а что так и
осталось за кадром.

Ты никого не оцениваешь и баллов не ставишь. Раскрытие — общий результат стола,
а не заслуга или вина отдельного игрока.

# ЧТО СЧИТАЕТСЯ РАСКРЫТЫМ

Раскрыто — это когда партия сказала это вслух или действовала исходя из этого,
и в логе есть, на что показать пальцем. Не считается раскрытым: догадка одного
игрока, которую никто не поддержал и которая ни на что не повлияла; правильный
вывод, сделанный после подсказки мастера в лоб; совпадение без понимания.

Тайна игрока считается вскрытой перед партией, если другие персонажи узнали её
суть. Не считается: подозрение без подтверждения, намёк, оставшийся без ответа.

# ФОРМАТ ОТВЕТА

Верни только JSON, без текста до и после:

{
  "вопросы": [
    {"ключ": "убийца", "раскрыт": true, "круг": 34, "цитата": "дословно из лога",
     "пояснение": "одна фраза"}
  ],
  "тайны": [
    {"персонаж": "Лизель", "вскрыта": false, "круг": null, "цитата": "",
     "пояснение": "одна фраза"}
  ],
  "исход": {
    "серебро": "возвращено | украдено | потеряно | неизвестно",
    "дожили": ["Курт", "Ханна"],
    "погибли": [],
    "чем_кончилось": "две-три фразы"
  }
}

Цитаты бери дословно, слово в слово. Если раскрытия нет — цитата пустая строка,
круг null.
"""


async def grade(args) -> dict:
    events = common.read_log(args.log)
    questions = json.loads(Path(args.questions).read_text(encoding="utf-8"))

    document = (ROOT.parent / args.document).resolve()
    prompts = Prompts(document, ROOT.parent / "orchestrator")
    # Скрытая правда и тайны берутся из промпта мастера в документе проекта —
    # чтобы не держать вторую копию модуля, которая разъедется с первой.
    true_of = prompts.blocks["gm"]

    task = "\n\n".join([
        PROMPT,
        "# СКРЫТАЯ ПРАВДА МОДУЛЯ И ТАЙНЫ ИГРОКОВ",
        true_of,
        "# ВОПРОСЫ, НА КОТОРЫЕ НАДО ОТВЕТИТЬ",
        json.dumps(questions, ensure_ascii=False, indent=2),
    ])
    transcript = common.transcript(events, blind=True)

    provider = providers.create(args.provider)
    try:
        session = await provider.open("Аналитик", task, args.model)
        reply = await session.send(
            transcript + "\n\nРазбери сессию по вопросам. Верни только JSON."
        )
    finally:
        await provider.shutdown()

    parsed = common.extract_json(reply.text)

    # Цитаты сверяются так же, как у судьи: ненайденная не подтверждает раскрытие.
    for item in parsed.get("questions", []) + parsed.get("тайны", []):
        quote = item.get(rubric_schema.QUOTE) or ""
        item["_quote"] = (
            common.check_quote(events, item.get("round"), quote)
            if quote else "нет цитаты"
        )
        confirmed = item["_quote"] not in ("не найдена", "нет цитаты")
        key = "раскрыт" if "раскрыт" in item else "вскрыта"
        if item.get(key) and not confirmed:
            item[key] = False
            item["_dealt"] = "цитата не подтвердилась"

    revealed = sum(1 for v in parsed.get("questions", []) if v.get("раскрыт"))
    total_of = len(parsed.get("questions", [])) or len(questions["questions"])
    exposed = sum(1 for tx in parsed.get("тайны", []) if tx.get("exposed"))

    parsed["_summary"] = {
        "загадок_раскрыто": revealed,
        "загадок_всего": total_of,
        "share": round(revealed / total_of, 2) if total_of else 0,
        "тайн_вскрыто": exposed,
        "тайн_всего": len(questions.get("тайны_игроков", {})),
        "model": reply.model_actual or reply.model,
        "cost": reply.cost,
    }

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(
        json.dumps(parsed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return parsed


def main() -> int:
    parsed_args = argparse.ArgumentParser(description="Раскрытие ваншота")
    parsed_args.add_argument("--log", required=True, nargs="+")
    parsed_args.add_argument("--out", required=True)
    parsed_args.add_argument("--questions", default=str(ROOT / "reveal.json"))
    parsed_args.add_argument("--document", default="vypusk-1-wfrp-desyatina.md")
    parsed_args.add_argument("--provider", default="claude")
    parsed_args.add_argument("--model", default="opus")
    args = parsed_args.parse_args()

    total = asyncio.run(grade(args))
    summary = total["_summary"]
    print(f"загадок раскрыто: {summary['загадок_раскрыто']} из {summary['загадок_всего']} "
          f"(доля {summary['share']})")
    print(f"тайн вскрыто: {summary['тайн_вскрыто']} из {summary['тайн_всего']}")
    print(f"исход: {total.get('outcome', {}).get('серебро')}")
    print(f"результат: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
