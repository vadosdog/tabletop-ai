#!/usr/bin/env python3
"""Собирает папку для импорта в Notion: «Прогоны» и страница на каждый прогон.

    python3 scripts/собрать-для-notion.py

Notion при импорте Markdown разворачивает папку в родительскую страницу, а файлы
внутри — в дочерние. Поэтому дерево делаем ровно таким, каким оно должно
получиться на той стороне, и ничего потом не двигаем руками.

На каждый прогон: чем запускали и с какими параметрами, что сказал судья,
комментарий от Luke целиком. Логи и транскрипты не тащим — они на диске,
а в Notion от них будет только шум.
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scoring"))
sys.path.insert(0, str(ROOT / "orchestrator"))

import common  # noqa: E402
from report import collate  # noqa: E402

OUTPUT = ROOT / "notion" / "Прогоны"
# Только боевые: проверки мастера и круги ноль в выпуск не идут.
SAMPLE = "боевой-прогон"


def index_run(folder: Path) -> int:
    tail = folder.name.rsplit("-", 1)[-1]
    return int(tail) if tail.isdigit() else 1


def table_scores(folder: Path, events: list[dict]) -> list[str]:
    judge_path = folder / "судья.json"
    if not judge_path.exists():
        return ["Судья по этому прогону не запускался."]

    judge = json.loads(judge_path.read_text(encoding="utf-8"))
    table = collate(judge, common.characters(events))
    cast = common.cast(events)

    lines = [
        f"Три слепых прохода, модель судьи — {judge.get('judge_model', '?')}. "
        "Медиана трёх проходов, в скобках разброс между ними.",
        "",
        "| Персонаж | Модель | Сеттинг | Правила | Цель | Тайна | Драма | Штрафы | Итог |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    order = sorted(table, key=lambda i: -table[i]["total"])
    for name in order:
        dt = table[name]
        cr = dt["criteria"]
        cells = []
        for criterion in common.CRITERIA:
            about = cr.get(criterion, {})
            cells.append(f"{about.get('median', '—')} ({about.get('spread', 0)})")
        lines.append(
            f"| {name} | {(cast.get(name) or {}).get('model', '?')} | "
            + " | ".join(cells)
            + f" | {dt['penalty']} | **{dt['total']}** |"
        )
    return lines


def page(folder: Path) -> str:
    events = common.read_log([str(folder / "лог.jsonl")])
    total = common.total_run(events)
    cast = common.cast(events)
    start = next((ev for ev in events if ev.get("event_type") == "старт"), {})
    config = json.loads((folder / "config.json").read_text(encoding="utf-8"))
    index = index_run(folder)

    lines = [f"# Боевой прогон №{index}", ""]
    lines += [
        f"**{total.get('rounds', '?')} кругов, остановка «{total.get('stop', '?')}», "
        f"{total.get('minutes', '?')} минут.** Зерно {total.get('seed', '?')}, "
        f"бросков {total.get('rolls', '?')}.",
        "",
        f"Папка прогона: `{folder.relative_to(ROOT)}`",
        "",
        "## Чем запускали",
        "",
        "```bash",
        f"# жребий вендоров: раздача по зерну, у всех новый персонаж",
        f"python3 orchestrator/run.py --seed {total.get('seed', '?')} "
        "--draw-vendors --avoid-repeat",
        "",
        "# сам прогон, отсоединённым процессом",
        f"python3 orchestrator/run.py --out {folder.relative_to(ROOT)}",
        "",
        "# судья: три слепых прохода",
        f"python3 scoring/judge.py --log {folder.relative_to(ROOT)}/лог.jsonl \\",
        f"    --out {folder.relative_to(ROOT)}/судья.json",
        "",
        "# отчёт",
        f"python3 scoring/report.py --log {folder.relative_to(ROOT)}/лог.jsonl \\",
        f"    --judge {folder.relative_to(ROOT)}/судья.json "
        f"--out {folder.relative_to(ROOT)}/отчёт",
        "```",
        "",
        "## Кто за кого играл",
        "",
        "| Персонаж | Провайдер | Модель |",
        "|---|---|---|",
    ]
    for name, what in cast.items():
        lines.append(f"| {name} | {what.get('provider')} | {what.get('model')} |")

    params = (start.get("generation_params") or {}).get("given_everyone") or {}
    if params:
        lines += ["", "## Условия сравнения", "",
                   "Одинаково у всех игроков, выставлено до первого хода:", "",
                   "| Параметр | Значение |", "|---|---|"]
        for key, value in params.items():
            lines.append(
                f"| {key.replace('_', ' ')} | "
                f"{'не задаётся' if value is None else value} |"
            )
    lines += ["", f"Предел кругов {config.get('max_rounds')}, "
               f"мастер {config['gm']['model']}, "
               f"судья {config['judge']['model']}."]

    lines += ["", "## Оценки судьи", ""] + table_scores(folder, events)

    resources = common.resources(events)
    if any_of_events_resources(events):
        lines += ["", "## Ресурсы", "",
                   "| Игрок | Заявлено | Подтверждено | Решимость начислена | "
                   "Провалов можно было перебросить |", "|---|---|---|---|---|"]
        for name, dt in resources.items():
            zn = ", ".join(f"{d} ×{v}" for d, v in (dt["claimed"] or {}).items()) or "—"
            p = ", ".join(f"{d} ×{v}" for d, v in (dt["confirmed"] or {}).items()) or "—"
            lines.append(f"| {name} | {zn} | {p} | {dt['resolve_awarded']} | "
                          f"{dt['failures_could_before_reroll']} |")

    comment = folder / "комментарий.md"
    if comment.exists():
        text = comment.read_text(encoding="utf-8")
        # Заголовок первого уровня уже есть у страницы — понижаем, чтобы
        # в Notion не было двух H1 подряд.
        text = re.sub(r"^# ", "## ", text, count=1, flags=re.MULTILINE)
        text = re.sub(r"^## (?!Комментарий)", "### ", text[text.index("## "):],
                       flags=re.MULTILINE)
        lines += ["", "## Комментарий от Luke", "", text.split("\n", 1)[1].strip()]

    return "\n".join(lines).rstrip() + "\n"


def any_of_events_resources(events: list[dict]) -> bool:
    return any(ev.get("event_type") == "ресурс" for ev in events)


def main() -> int:
    folders = sorted(p for p in (ROOT / "runs").iterdir()
                   if p.is_dir() and SAMPLE in p.name and (p / "лог.jsonl").exists())
    legacy = [f for f in folders if common.is_legacy_format(f)]
    folders = [f for f in folders if f not in legacy]
    for f in legacy:
        print(f"  пропущен (формат до перехода на латиницу): {f.name}")
    if not folders:
        sys.exit("боевых прогонов в текущем формате не нашлось"
                 + (f"; пропущено старых: {len(legacy)}" if legacy else ""))

    if OUTPUT.exists():
        shutil.rmtree(OUTPUT)
    OUTPUT.mkdir(parents=True)

    contents = ["# Прогоны", "",
                  "Боевые прогоны выпуска: четыре сессии по 29 кругов, каждая "
                  "модель на каждом персонаже.", "",
                  "| Прогон | Кругов | Минут | Состав |", "|---|---|---|---|"]

    for folder in folders:
        index = index_run(folder)
        name_file = f"Боевой прогон №{index}.md"
        (OUTPUT / name_file).write_text(page(folder), encoding="utf-8")

        events = common.read_log([str(folder / "лог.jsonl")])
        total = common.total_run(events)
        cast = common.cast(events)
        who = ", ".join(f"{i}→{(s or {}).get('provider')}"
                        for i, s in cast.items() if i != "Мастер")
        contents.append(f"| №{index} | {total.get('rounds', '?')} | "
                          f"{total.get('minutes', '?')} | {who} |")
        print(f"  собрана: {name_file}")

    (OUTPUT / "Прогоны.md").write_text("\n".join(contents) + "\n", encoding="utf-8")
    print(f"\nготово: {OUTPUT.relative_to(ROOT)}")
    print("Перетащить эту папку в Notion: Import → Markdown & CSV.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
