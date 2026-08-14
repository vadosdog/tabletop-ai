#!/usr/bin/env python3
"""Отчёт по прогону: таблица баллов, калибровка, расхождение, цитаты, транскрипт.

    python3 scoring/report.py --log runs/X/лог.jsonl --judge runs/X/судья.json \
        --out runs/X/отчёт [--human runs/X/выборка/оценка.csv] \
        [--judge-sample runs/X/судья-выборка.json]

Оркестратор баллов не ставит, судья не сводит таблиц. Здесь всё сводится.
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common  # noqa: E402
import metrics  # noqa: E402
import rubric_schema


# --- сведение оценок судьи -------------------------------------------------

def _identify(raw: str, names: list[str]) -> str | None:
    key = (raw or "").strip().lower()
    for name in names:
        if key.startswith(name.lower()[:4]):
            return name
    return None


def collate(judge: dict, names: list[str], criteria: list[str] | None = None) -> dict:
    """Медиана трёх проходов и разброс между ними. Баллы без цитаты не в счёт."""
    criteria = criteria or common.CRITERIA
    collected = {name: {criterion: [] for criterion in criteria} for name in names}
    dropped = {name: 0 for name in names}
    penalties_by_passes = {name: [] for name in names}
    all_of_penalties = {name: [] for name in names}

    for pass_of in judge["passes"]:
        sum_of_penalties = {name: 0 for name in names}
        for raw_name, data in pass_of.get(rubric_schema.CHARACTERS, {}).items():
            name = _identify(raw_name, names)
            if name is None:
                continue
            for criterion, grade in data.get(rubric_schema.CRITERIA, {}).items():
                if criterion not in criteria:
                    continue
                if grade.get("_counted"):
                    collected[name][criterion].append(int(grade[rubric_schema.SCORE]))
                else:
                    dropped[name] += 1
            for penalty in data.get(rubric_schema.PENALTIES, []):
                if not penalty.get("_counted"):
                    continue
                weight = common.PENALTIES.get(penalty.get("kind", ""), 0)
                sum_of_penalties[name] += weight
                all_of_penalties[name].append({**penalty, "weight": weight, "pass_of": pass_of.get("_pass_of")})
        for name in names:
            penalties_by_passes[name].append(sum_of_penalties[name])

    table = {}
    for name in names:
        line = {}
        sum_of = 0
        for criterion in criteria:
            scores = collected[name][criterion]
            median = statistics.median(scores) if scores else None
            line[criterion] = {
                "median": median,
                "spread": (max(scores) - min(scores)) if len(scores) > 1 else 0,
                "passes": scores,
            }
            if median is not None:
                sum_of += median
        penalty = statistics.median(penalties_by_passes[name]) if penalties_by_passes[name] else 0
        table[name] = {
            "criteria": line,
            "penalty": penalty,
            "penalties_detailed": all_of_penalties[name],
            "scores_without_quotes": dropped[name],
            "total": round(sum_of + penalty, 1),
        }
    return table


# --- человек ---------------------------------------------------------------

def read_human(path: str, names: list[str]) -> dict[str, dict[str, float]]:
    grades: dict[str, dict[str, float]] = {}
    with Path(path).open(encoding="utf-8") as fn:
        for line in csv.DictReader(fn):
            name = _identify(line.get(rubric_schema.CSV_CHARACTER, ""), names)
            criterion = (line.get(rubric_schema.CSV_CRITERION) or "").strip().lower()
            raw_value = (line.get(rubric_schema.CSV_SCORE) or "").strip().replace(",", ".")
            if name is None or criterion not in common.CRITERIA or not raw_value:
                continue
            grades.setdefault(name, {})[criterion] = float(raw_value)
    return grades


def discrepancy(table: dict, human: dict) -> dict:
    """По каждому критерию отдельно: систематическое важнее случайного."""
    total = {}
    for criterion in common.CRITERIA:
        margins = []
        for name, grades in human.items():
            judge = table.get(name, {}).get("criteria", {}).get(criterion, {}).get("median")
            when_has = grades.get(criterion)
            if judge is None or when_has is None:
                continue
            margins.append(judge - when_has)
        if not margins:
            continue
        total[criterion] = {
            "systematic": round(statistics.fmean(margins), 2),
            "random": round(statistics.fmean(abs(rp) for rp in margins), 2),
            "pairs": len(margins),
        }
    return total


# --- калибровка ------------------------------------------------------------

def calibration(table: dict, control: str | None, discrepancies: dict,
               cast: dict | None = None) -> list[dict]:
    totals = {name: data["total"] for name, data in table.items()}
    lowest = ([name for name, c in totals.items() if c == min(totals.values())]
              if totals else [])
    # Ничья внизу — это не «контрольный последний», это отсутствие сигнала.
    minimum = lowest[0] if len(lowest) == 1 else None
    spread_totals = round(max(totals.values()) - min(totals.values()), 1) if totals else 0

    # Контрольный игрок имеет смысл только если он и правда на другой модели.
    models = {name: (cast or {}).get(name, {}).get("model") for name in table}
    other = {mk for name, mk in models.items() if name != control and mk}
    control_real = bool(
        control and models.get(control) and models[control] not in other
    )

    all_of_spreads = [
        data["criteria"][d]["spread"]
        for data in table.values() for d in common.CRITERIA
    ]
    stable = sum(1 for rp in all_of_spreads if rp <= 1)
    share = stable / len(all_of_spreads) if all_of_spreads else 0

    # Потолок: стабильность на максимуме — это не различающая способность.
    maximum = 5 * len(common.CRITERIA)
    on_ceiling = sum(1 for c in totals.values() if c >= maximum)
    grades = [data["criteria"][d]["median"]
              for data in table.values() for d in common.CRITERIA]
    fives = sum(1 for about in grades if about == 5)
    total_of_grades = sum(1 for about in grades if about is not None)

    has_scores = any(
        data["criteria"][d]["median"] is not None
        for data in table.values() for d in common.CRITERIA
    )
    if not has_scores:
        return [{
            "question": "Калибровка",
            "reply": "не считалась",
            "digit": "отчёт собран без судьи (--judge не передан)",
            "output_text": "",
        }]

    items = [
        {
            "question": "Баллы различаются?",
            "reply": "да" if spread_totals > 1 else "нет",
            "digit": f"разброс итогов {spread_totals}",
            "output_text": "" if spread_totals > 1 else "рубрика слепая, переписывать",
        },
        {
            "question": "Контрольный игрок внизу?",
            "reply": ("да" if control_real and minimum == control
                      else "нет" if control_real else "нечего проверять"),
            "digit": (
                f"контрольный {control} на {models.get(control)}, низший балл у "
                + (minimum if minimum else f"ничья: {', '.join(lowest)}")
                if control_real else
                (f"контрольный {control} на той же модели, что и остальные — "
                 "слабого игрока в прогоне не было"
                 if control else "контрольный игрок не назначен")
            ),
            "output_text": ("скоринг не измеряет качество игры"
                      if control_real and minimum != control else ""),
        },
        {
            "question": "Рубрика различает?",
            "reply": "нет" if on_ceiling >= 2 else "да",
            "digit": (f"на максимуме {on_ceiling} из {len(table)} персонажей, "
                      f"пятёрок {fives} из {total_of_grades}"),
            "output_text": ("рубрика упёрлась в потолок: сильных игроков она не "
                      "разводит, якоря на пятёрку надо поднимать"
                      if on_ceiling >= 2 else ""),
        },
        {
            "question": "Судья стабилен?",
            "reply": "да" if share > 0.5 else "нет",
            "digit": f"разброс ≤1 у {stable} из {len(all_of_spreads)} оценок",
            "output_text": "" if share > 0.5 else "критерии сформулированы плохо",
        },
    ]

    if discrepancies:
        bad = [d for d, zn in discrepancies.items() if abs(zn["systematic"]) > 1]
        items.append({
            "question": "Судья согласен с человеком?",
            "reply": "нет" if bad else "да",
            "digit": "; ".join(
                f"{d}: систематическое {zn['systematic']:+}, случайное {zn['random']}"
                for d, zn in discrepancies.items()
            ),
            "output_text": (f"переписать формулировки: {', '.join(bad)}" if bad else ""),
        })
    else:
        items.append({
            "question": "Судья согласен с человеком?",
            "reply": "не проверено",
            "digit": "ручная оценка не подана (--human)",
            "output_text": "",
        })
    return items


# --- рендер ----------------------------------------------------------------

def _cell(value) -> str:
    return "—" if value is None else f"{value:g}"


def _section_gm(table_gm: dict, judge_gm: dict) -> list[str]:
    lines = _table_scores(table_gm, common.CRITERIA_GM,
                             "## Мастер")
    lines.insert(2, "Мастер — такой же участник эксперимента. Судья тот же и "
                     "так же слеп.")
    for name, data in table_gm.items():
        for criterion in common.CRITERIA_GM:
            for pass_of in judge_gm.get("passes", []):
                grade = (pass_of.get(rubric_schema.CHARACTERS, {}).get(name, {})
                          .get("criteria", {}).get(criterion))
                if grade and grade.get("_counted"):
                    lines.append(
                        f"- {criterion}, балл {grade['score']}, круг "
                        f"{grade.get('round')} (проход {pass_of.get('_pass_of')}): "
                        f"«{grade.get('quote')}»"
                    )
    lines.append("")
    return lines


def _section_reveal(reveal: dict) -> list[str]:
    reveal_totals = reveal.get("_summary", {})
    lines = ["## Раскрытие ваншота", "",
              "Общий счёт стола, не баллы игроков.", "",
              "| Вопрос | Раскрыт | Круг |", "|---|---|---|"]
    for item in reveal.get("questions", []):
        lines.append(
            f"| {item.get('key')} | {'да' if item.get('раскрыт') else 'нет'} "
            f"| {item.get('round') or '—'} |"
        )
    lines += ["", "| Тайна игрока | Вскрыта перед партией | Круг |", "|---|---|---|"]
    for item in reveal.get("тайны", []):
        lines.append(
            f"| {item.get('character')} | {'да' if item.get('exposed') else 'нет'} "
            f"| {item.get('round') or '—'} |"
        )
    outcome = reveal.get("outcome", {})
    lines += [
        "",
        f"**Итог: партия раскрыла {reveal_totals.get('загадок_раскрыто')} загадки из "
        f"{reveal_totals.get('загадок_всего')} и вскрыла {reveal_totals.get('тайн_вскрыто')} тайну "
        f"из {reveal_totals.get('тайн_всего')}.**",
        "",
        f"- серебро: {outcome.get('серебро', '?')}",
        f"- дожили: {', '.join(outcome.get('survived') or []) or '—'}",
        f"- погибли: {', '.join(outcome.get('died') or []) or '—'}",
        f"- чем кончилось: {outcome.get('weapon_ended', '')}",
        "",
    ]
    for item in reveal.get("questions", []):
        if item.get("quote"):
            lines.append(
                f"- {item.get('key')}, круг {item.get('round')} "
                f"({item.get('_quote')}): «{item.get('quote')}»"
            )
    lines.append("")
    return lines


def _section_combat(events) -> list[str]:
    summary = common.combat(events)
    if not summary or not summary.get("attacks"):
        return []
    lines = ["## Бой", "",
              "Раны считал скрипт, мастер только описывал.", "",
              f"- атак: {summary.get('attacks')}, попаданий: {summary.get('hits')}, "
              f"критов: {summary.get('crits')}",
              f"- дублей: {summary.get('doubles', '—')}, фумблов: "
              f"{summary.get('fumbles', '—')}",
              f"- смертей: {summary.get('deaths', 0)}, Судьба потрачена: "
              f"{summary.get('fate_spent', 0)} раз",
              ]
    wounds = summary.get("wounds_lost") or {}
    if wounds:
        lines.append("- ран потеряно: " + ", ".join(
            f"{name} — {amount}" for name, amount in sorted(
                wounds.items(), key=lambda p: -p[1])))
    if summary.get("reached_to_zero"):
        lines.append("- дошли до нуля ран: " + ", ".join(summary["reached_to_zero"]))
    injuries = summary.get("injuries") or {}
    if injuries:
        lines.append("- травмы: " + "; ".join(
            f"{name} — {', '.join(rows)}" for name, rows in injuries.items()))
    conditions = summary.get("conditions_at_end") or {}
    if conditions:
        lines.append("- состояния на конец сессии: " + "; ".join(
            f"{name} — {', '.join(rows)}" for name, rows in conditions.items()))
    lines.append("")
    return lines


def _section_growth(events) -> list[str]:
    lines_growth = common.growth(events)
    if not lines_growth:
        return []
    header = ["Круг", "Контекст одного агента, тыс. токенов", "У кого",
             "Токенов за круг на всех, тыс.", "Секунд"]
    lines = ["## Разгон контекста", "",
              "Историю не режем нарочно: провалы внимания на длинном контексте — "
              "результат эксперимента, а не помеха. Но знать, где потолок, надо.",
              "",
              "**Контекст одного агента** — сколько истории уходит в модель на "
              "один вызов. Только эта цифра сравнима с окном модели. Соседняя "
              "колонка — сумма по всем пятерым за круг: это мера расхода, а не "
              "размер контекста, складывать окна разных агентов незачем.", "",
              "| " + " | ".join(header) + " |", "|---" * len(header) + "|"]
    for line in lines_growth:
        if line["round"] % 5 and line["round"] not in (1, max(
                s["round"] for s in lines_growth)):
            continue
        lines.append("| " + " | ".join([
            str(line["round"]),
            f"{line['context_agent'] / 1000:.0f}",
            line["у_кого"] or "—",
            f"{line['tokens_total_of'] / 1000:.0f}",
            str(line["seconds"]),
        ]) + " |")
    peak = max(lines_growth, key=lambda s: s["context_agent"])
    first = lines_growth[0]
    if first["context_agent"]:
        growth_for_round = (
            (peak["context_agent"] - first["context_agent"])
            / max(peak["round"] - first["round"], 1)
        )
        reserve = 200_000  # окно, на которое стоит закладываться
        to_ceiling = (
            int((reserve - peak["context_agent"]) / growth_for_round + peak["round"])
            if growth_for_round > 0 else None
        )
        lines += [
            "",
            f"Контекст одного агента: {first['context_agent'] / 1000:.0f} тыс. "
            f"токенов на первом круге, пик {peak['context_agent'] / 1000:.0f} тыс. "
            f"на круге {peak['round']} у агента «{peak['у_кого']}» — прирост около "
            f"{growth_for_round / 1000:.1f} тыс. токенов за круг.",
        ]
        if to_ceiling:
            lines.append(
                f"При таком темпе окно в 200 тыс. токенов упрётся примерно на "
                f"круге {to_ceiling}. Это и есть настоящая причина держать предел "
                f"в тридцать кругов."
            )
        lines.append("")
    return lines


def _start(events) -> dict:
    for event in events:
        if event.get("event_type") == "старт":
            return event
    return {}


def _section_params(events) -> list[str]:
    """Условия сравнения. Самый важный блок в отчёте после баллов.

    Если у одной модели температура выше, а у другой лимит короче — сравниваются
    настройки, а не модели. Поэтому здесь и то, что задано всем, и то, что задать
    не удалось: замолчать второе значило бы соврать про первое.
    """
    block = (_start(events) or {}).get("generation_params")
    if not block:
        return []

    given = block.get("given_everyone", {})
    lines = ["## Условия сравнения", "",
              "Одно и то же у всех игроков, выставлено до первого хода:", "",
              "| Параметр | Значение |", "|---|---|"]
    captions = {
        "temperature": "температура",
        "top_p": "top_p",
        "max_output_tokens": "предел ответа, токенов",
        "reasoning": "режим размышления",
        "timeout_s": "таймаут ответа, с",
        "attempts": "попыток при сбое сети",
    }
    for key, signature in captions.items():
        value = given.get(key)
        lines.append(f"| {signature} | {'не задаётся' if value is None else value} |")

    in_words = block.get("reasoning_in_words_vendor") or {}
    if in_words:
        lines += ["", "Режим размышления словами каждого вендора:", "",
                   "| Провайдер | Уровень |", "|---|---|"]
        for name, level in sorted(in_words.items()):
            lines.append(f"| {name} | {level or '—'} |")
        lines += ["",
                   "Выключить размышление у всех нельзя: Gemini и Grok этого не умеют, "
                   "нижняя ступень у обоих — «low». Поэтому выровнялись по нижней общей "
                   "ступени. Одно и то же слово у разных вендоров означает разное число "
                   "токенов — доля размышления по каждому видна в таблице расхода.", ""]

    discrep = block.get("known_discrepancies") or {}
    if discrep:
        lines += ["", "### Известные расхождения", "",
                   "Здесь всё, что выровнять не удалось. Это не оговорки задним "
                   "числом: список составлен до прогона и проверен живым запросом "
                   "к каждому API.", ""]
        for name in sorted(discrep):
            lines.append(f"**{name}**")
            lines.append("")
            for item in discrep[name]:
                lines.append(f"- {item}")
            lines.append("")

    caveat = block.get("caveat_about_determinism")
    if caveat:
        lines += ["### Воспроизводимость", "", caveat, ""]
    return lines + [""]


def _section_usage(events) -> list[str]:
    """Сколько стоил прогон у кого, доля кэша и доля размышления."""
    total = common.total_run(events)
    provider_totals = total.get("provider_totals") or {}
    if not provider_totals:
        return []

    lines = ["## Расход по провайдерам", "",
              "| Провайдер | Вход | Кэш | Доля кэша | Выход | Из них размышление | "
              "Повторов | Отказов | Деньги |",
              "|---|---|---|---|---|---|---|---|---|"]
    for name in sorted(provider_totals):
        dt = provider_totals[name]
        share_cache = dt.get("share_cache")
        reasoning = dt.get("reasoning", 0)
        share_rp = dt.get("share_reasoning")
        lines.append(
            f"| {name} | {dt.get('input', 0)} | {dt.get('cache_read', 0)} | "
            f"{'—' if share_cache is None else f'{share_cache:.0%}'} | "
            f"{dt.get('output', 0)} | {reasoning}"
            f"{'' if share_rp is None else f' ({share_rp:.0%})'} | "
            f"{dt.get('retries', '—')} | {dt.get('refusals', 0)} | "
            f"${dt.get('cost_usd', 0):.4f} |"
        )

    lines += ["",
               "Токены размышления входят в выходные и по цене выхода же и считаются: "
               "второй раз за них не платят, в столбце они выделены только чтобы "
               "показать долю."]

    if "claude" in provider_totals:
        lines += ["",
                   "**Claude идёт по подписке.** Сумма в его строке — пересчёт по "
                   "публичным ставкам, который отдаёт SDK, а не счёт: по подписке "
                   "эти деньги не списывались. Суммы остальных трёх посчитаны по "
                   "ставкам из конфига и требуют сверки с настоящими счетами; сырые "
                   "токены лежат в логе, пересчёт не требует нового прогона."]

    truncations = {i: dt.get("truncations_by_limit") for i, dt in provider_totals.items()
              if dt.get("truncations_by_limit")}
    if truncations:
        lines += ["", f"⚠️ Ответы, упёршиеся в предел длины: {truncations}. "
                   "Такой ход обрезан на полуслове, и в сравнение его брать нельзя."]
    else:
        lines += ["", "Ни один ответ не упёрся в предел длины: лимит взят с запасом "
                   "и ни у кого не сработал, то есть на результат не повлиял."]

    resolved = {i: dt["discrepancies_resolved_on_the_fly"]
                  for i, dt in provider_totals.items()
                  if dt.get("discrepancies_resolved_on_the_fly")}
    if resolved:
        lines += ["", "### Выяснилось по ходу прогона", ""]
        for name in sorted(resolved):
            for item in resolved[name]:
                lines.append(f"- **{name}**: {item}")
        lines.append("")
    return lines + [""]


def _section_upstream(events) -> list[str]:
    """Кто на самом деле обслуживал запросы через прослойку.

    Прямые подключения этого раздела не рождают: там вендор и есть исполнитель.
    А маршрутизатор вправе сменить хост, и если он это сделал, часть ходов
    сыграна на другой сборке — молчать об этом нельзя.
    """
    marks = [ev for ev in events if ev.get("event_type") == "апстрим"]
    if not marks:
        return []

    provider_totals: dict[str, list[dict]] = {}
    for mark in marks:
        provider_totals.setdefault(mark.get("provider", "?"), []).append(mark)

    lines = ["## Через прослойку", ""]
    for provider, rows in sorted(provider_totals.items()):
        allowed = rows[0].get("allowed") or []
        lines += [
            f"**{provider}** — разрешённые апстримы: "
            f"{', '.join(allowed) or '—'}, фолбэк запрещён.",
            "",
        ]
        if len(rows) == 1:
            lines += [
                f"Весь прогон отвечал один апстрим: **{rows[0].get('upstream')}**. "
                "Закрепление удержалось, сборка не менялась.",
                "",
            ]
        else:
            chain = " → ".join(str(about.get("upstream")) for about in rows)
            lines += [
                f"⚠️ **Апстрим менялся посреди прогона:** {chain}.",
                "",
                "Смена произошла с круга "
                + ", ".join(str(about.get("round")) for about in rows[1:])
                + ". Ходы до и после смены сыграны на разных сборках, и "
                "сравнивать их между собой можно только с этой оговоркой.",
                "",
            ]
    return lines


def _section_resources(events) -> list[str]:
    """Судьба, Удача, Стойкость, Решимость — заявлено против подтверждённого.

    Разница между колонками — не придирка, а суть: в прошлых прогонах Удачи
    в коде не было вовсе, игроки её «тратили», и судья засчитывал это за
    работу с механикой. Пока эти два числа не стоят рядом, балл по «Правилам»
    ничего не значит.
    """
    resources = common.resources(events)
    if not resources:
        return []

    lines = ["## Ресурсы", "",
              "| Игрок | Заявлено | Подтверждено | Решимость начислена | "
              "Провалов можно было перебросить | На финише |",
              "|---|---|---|---|---|---|"]
    for name, dt in resources.items():
        finish = dt.get("on_finish") or {}
        left = (f"Судьба {finish.get('fate', '?')}, "
                    f"Удача {finish.get('fortune', '?')}/{finish.get('fortune_max', '?')}, "
                    f"Стойкость {finish.get('resilience', '?')}, "
                    f"Решимость {finish.get('resolve', '?')}/"
                    f"{finish.get('resolve_max', '?')}")
        claimed = ", ".join(f"{rubric_schema.resource_name(d)} ×{v}"
                            for d, v in (dt.get("claimed") or {}).items())
        confirmed = ", ".join(f"{rubric_schema.resource_name(d)} ×{v}"
                              for d, v in (dt.get("confirmed") or {}).items())
        lines.append(
            f"| {name} | {claimed or '—'} | {confirmed or '—'} | "
            f"{dt.get('resolve_awarded', 0)} | "
            f"{dt.get('failures_could_before_reroll', 0)} | {left} |"
        )

    discrepancies = [
        (name, dt) for name, dt in resources.items()
        if (dt.get("claimed") or {}) != (dt.get("confirmed") or {})
    ]
    if discrepancies:
        lines += ["", "### Заявлено, но не подтверждено", "",
                   "Игрок объявил трату, скрипт её не принял. Либо тратить было "
                   "нечего, либо это баг — и то и другое надо видеть. "
                   "**Судье такие заявки за трату не засчитываются.**", ""]
        rejected = [ev for ev in events if ev.get("event_type") == "ресурс"
                     and ev.get("claimed") and not ev.get("confirmed")
                     and not ev.get("pending")]
        for event in rejected:
            reason = event.get("reason") or "без объяснения"
            resource = rubric_schema.resource_name(event.get("resource") or "")
            lines.append(f"- круг {event.get('round')}, **{event.get('who')}**, "
                          f"{resource}: {reason}")
        if not rejected:
            lines.append("- отклонённых заявок нет")
        lines.append("")

        # Заявка на Удачу живёт один круг: не подвернулось провала — сгорела.
        # Это не отказ скрипта, а промах игрока по времени, и путать их нельзя.
        burnt = [ev for ev in events if ev.get("event_type") == "ресурс"
                   and ev.get("pending")]
        if burnt:
            lines += ["Заявки на Удачу, принятые в ожидание (сработали бы на "
                       "первом же проваленном броске того же круга):", ""]
            for ev in burnt:
                lines.append(f"- круг {ev.get('round')}, **{ev.get('who')}**")
            lines.append("")

    rerolls = [ev for ev in events if ev.get("event_type") == "ресурс"
                 and ev.get("roll_before")]
    if rerolls:
        lines += ["### Перебросы за Удачу", "",
                   "| Круг | Игрок | Было | Стало | Помогло |", "|---|---|---|---|---|"]
        for ev in rerolls:
            to, after = ev["roll_before"], ev["roll_after"]
            lines.append(
                f"| {ev.get('round')} | {ev.get('who')} | {to['rolled']} из {to['target']} | "
                f"{after['rolled']} из {after['target']} | "
                f"{'да' if ev.get('helped') else 'нет'} |"
            )
        lines.append("")

    without_spends = [name for name, dt in resources.items()
                if not (dt.get("confirmed") or {})
                and dt.get("failures_could_before_reroll", 0) > 0]
    if without_spends:
        lines += ["", "Доиграли с полными руками, имея возможность потратить: "
                   + ", ".join(without_spends) + ". Число провалов в таблице показывает, "
                   "сколько раз случай был — без него упрёк был бы несправедлив.", ""]
    return lines + [""]


def _section_refusals(events) -> list[str]:
    """Счётчик отказов по каждому игроку с цитатой.

    Ноль здесь значит ровно то, что написано, только если ловля проверена
    искусственным отказным запросом. Ссылка на эту проверку — ниже таблицы.
    """
    total = common.total_run(events)
    refusals = total.get("refusals") or []

    suspicions = [
        ev for ev in events
        if ev.get("event_type") == "ход"
        and "подозрение_на_отказ" in (ev.get("tags") or [])
    ]

    lines = ["## Отказы моделей", ""]
    if not refusals:
        lines += ["Ни одна модель не отказалась играть сцену.", ""]
    else:
        by_players: dict[str, int] = {}
        for entry in refusals:
            by_players[entry["who"]] = by_players.get(entry["who"], 0) + 1
        lines += ["| Игрок | Провайдер | Отказов |", "|---|---|---|"]
        providers_players = {zn["who"]: zn.get("provider", "?") for zn in refusals}
        for name in sorted(by_players):
            lines.append(
                f"| {name} | {providers_players.get(name, '?')} | {by_players[name]} |"
            )
        lines += ["", "### Цитаты", ""]
        for entry in refusals:
            lines.append(
                f"- круг {entry.get('round', '?')}, **{entry['who']}** "
                f"({entry.get('provider', '?')}): «{entry['quote']}»"
            )
        lines += ["",
                   "Отказ не повторялся: тихий ретрай спрятал бы его. Персонаж "
                   "считался промолчавшим, круг шёл дальше, текст отказа за стол "
                   "не уходил — ни мастер, ни другие игроки его не видели.", ""]

    if suspicions:
        lines += [f"Кроме того, {len(suspicions)} ответ(ов) помечено подозрением "
                   "на отказ — их надо прочитать глазами, автомат в них не уверен:", ""]
        for event in suspicions:
            quote = " ".join((event.get("text") or "").split())[:200]
            lines.append(f"- круг {event.get('round', '?')}, "
                          f"**{event.get('speaker')}**: «{quote}»")
        lines.append("")

    lines += ["Ловля отказов проверена отдельно: искусственный отказный запрос "
               "к каждому провайдеру (`preflight.py --refusal`) и прогон на подделке "
               "(`tests/test_refusal_in_run.py`). Без этого ноль в таблице значил "
               "бы и «никто не отказался», и «мы не умеем ловить».", ""]
    return lines


def build_report(events, table, counters, items, discrepancies, judge,
                  table_gm=None, judge_gm=None, reveal=None) -> str:
    names = list(table)
    total_run = common.total_run(events)
    cast = common.cast(events)
    control = common.control(events)

    lines = ["# Отчёт по репетиционному прогону", ""]

    lines += ["## Прогон", ""]
    lines += [
        f"- кругов: {total_run.get('rounds', '?')}, остановка: "
        f"{total_run.get('stop', '?')}",
        f"- по часам: {total_run.get('minutes', '?')} мин",
        f"- токенов: {total_run.get('tokens', {})}, "
        f"стоимость ${total_run.get('cost_usd', 0)}",
        f"- бросков: {total_run.get('rolls', '?')}, зерно "
        f"{total_run.get('seed', '?')} (прогон воспроизводится)",
        f"- судья: {judge.get('judge_model')}, проходов {judge.get('pass_count')}"
        + (f", по выборке кругов {judge.get('rounds')}" if judge.get("rounds") else "")
        if judge.get("passes") else "- судья: не запускался",
        "",
    ]

    lines += ["## Кто за кого играл", "",
               "| Персонаж | Провайдер | Модель |", "|---|---|---|"]
    for name, settings in cast.items():
        note = " ← контрольный" if name == control else ""
        lines.append(
            f"| {name}{note} | {settings.get('provider')} | {settings.get('model')} |"
        )
    lines += ["", "Судье этой таблицы не показывали.", ""]

    if not judge.get("passes"):
        lines += ["## Баллы", "",
                   "Судья не запускался: отчёт собран по логу. "
                   "Таблица баллов появится после `judge.py`.", ""]
    else:
        lines += _table_scores(table)

    if table_gm:
        lines += _section_gm(table_gm, judge_gm or {})
    if reveal:
        lines += _section_reveal(reveal)

    lines += _tail_report(table, counters, items, discrepancies, judge)
    lines += _section_resources(events)
    lines += _section_params(events)
    lines += _section_upstream(events)
    lines += _section_refusals(events)
    lines += _section_usage(events)
    lines += _section_combat(events)
    lines += _section_growth(events)
    return "\n".join(lines).strip() + "\n"


def _table_scores(table: dict, criteria: list[str] | None = None,
                    heading: str = "## Баллы") -> list[str]:
    criteria = criteria or common.CRITERIA
    names = list(table)
    lines = [heading, "",
               "Медиана трёх проходов, в скобках разброс между проходами.", "",
               "| Персонаж | " + " | ".join(c.capitalize() for c in criteria)
               + " | Штрафы | Итог |",
               "|---" * (len(criteria) + 3) + "|"]
    for name in names:
        cells = []
        for criterion in criteria:
            data = table[name]["criteria"][criterion]
            cells.append(f"{_cell(data['median'])} ({data['spread']})")
        lines.append(
            f"| {name} | " + " | ".join(cells)
            + f" | {table[name]['penalty']:g} | **{table[name]['total']:g}** |"
        )
    lines.append("")

    dropped = {name: table[name]["scores_without_quotes"] for name in names}
    if any(dropped.values()):
        lines += [
            "Баллов отброшено за ненайденную цитату: "
            + ", ".join(f"{name} — {n}" for name, n in dropped.items() if n),
            "",
        ]
    return lines


def _tail_report(table, counters, items, discrepancies, judge) -> list[str]:
    names = list(table)
    lines = ["## Без баллов, но в таблицу", "",
               "| Персонаж | Кругов отыграл | Тайных ходов | Средняя длина реплики | "
               "Обратились | Ответил | Самобросков |",
               "|---|---|---|---|---|---|---|"]
    for name in names:
        mk = counters[name]
        lines.append(
            f"| {name} | {mk.get('rounds_played', mk['turns'])} | {mk['secret_turns']} | "
            f"{mk['mean_length_lines']} | {mk['обратились_к_нему']} | "
            f"{mk['answered']} | {mk['self_rolls']} |"
        )
    lines += ["", "«Обратились» и «ответил» считаются по упоминанию имени — "
                   "это эвристика, а не точный счёт.", ""]

    lines += ["## Калибровка", ""]
    for item in items:
        tail = f" — {item['output_text']}" if item["output_text"] else ""
        lines.append(f"- **{item['question']}** {item['reply'].upper()}. "
                      f"{item['digit']}{tail}")
    lines.append("")

    if discrepancies:
        lines += ["## Расхождение судьи и человека", "",
                   "| Критерий | Систематическое | Случайное | Пар |",
                   "|---|---|---|---|"]
        for criterion, data in discrepancies.items():
            lines.append(
                f"| {criterion} | {data['systematic']:+g} | "
                f"{data['random']:g} | {data['pairs']} |"
            )
        lines += ["", "Положительное систематическое — судья щедрее человека.", ""]

    if not judge.get("passes"):
        return lines

    lines += ["## Цитаты — заготовка монтажного листа", ""]
    for name in names:
        lines.append(f"### {name}")
        for criterion in common.CRITERIA:
            for pass_of in judge["passes"]:
                for raw, data in pass_of.get(rubric_schema.CHARACTERS, {}).items():
                    if _identify(raw, names) != name:
                        continue
                    grade = data.get(rubric_schema.CRITERIA, {}).get(criterion)
                    if not grade or not grade.get("_counted"):
                        continue
                    lines.append(
                        f"- {criterion}, балл {grade['score']}, круг "
                        f"{grade.get('round')} (проход {pass_of.get('_pass_of')}, "
                        f"цитата {grade.get('_quote')}): «{grade.get('quote')}»"
                    )
        for penalty in table[name]["penalties_detailed"]:
            lines.append(
                f"- ШТРАФ {penalty.get('kind')} ({penalty.get('weight')}), круг "
                f"{penalty.get('round')}: «{penalty.get('quote')}»"
            )
        lines.append("")

    return lines


def main() -> int:
    parsed = argparse.ArgumentParser(description="Сводный отчёт по прогону")
    parsed.add_argument("--log", required=True, nargs="+",
                        help="один лог или несколько подряд (продолженный прогон)")
    parsed.add_argument("--judge", default=None,
                        help="оценки судьи; без них будут транскрипт и счётчики")
    parsed.add_argument("--out", required=True)
    parsed.add_argument("--human", default=None, help="CSV с ручными оценками")
    parsed.add_argument("--judge-sample", default=None,
                        help="оценки судьи по той же выборке, что у человека")
    parsed.add_argument("--judge-gm", default=None,
                        help="оценки мастера (judge.py --rubric rubric-gm.md)")
    parsed.add_argument("--reveal", default=None,
                        help="раскрытие ваншота (reveal.py)")
    args = parsed.parse_args()

    events = common.read_log(args.log)
    names = common.characters(events)
    if args.judge:
        judge = json.loads(Path(args.judge).read_text(encoding="utf-8"))
    else:
        # Без судьи отчёт всё равно нужен: транскрипт, счётчики, время, расход.
        judge = {"passes": [], "pass_count": 0, "judge_model": None, "rounds": None}

    table = collate(judge, names)
    counters = metrics.compute(events, names)

    discrepancies = {}
    if args.human:
        human = read_human(args.human, names)
        source = table
        if args.judge_sample:
            sampled = json.loads(Path(args.judge_sample).read_text(encoding="utf-8"))
            source = collate(sampled, names)
        discrepancies = discrepancy(source, human)

    table_gm = judge_gm = reveal = None
    if args.judge_gm:
        judge_gm = json.loads(Path(args.judge_gm).read_text(encoding="utf-8"))
        table_gm = collate(judge_gm, ["Мастер"], common.CRITERIA_GM)
    if args.reveal:
        reveal = json.loads(Path(args.reveal).read_text(encoding="utf-8"))

    items = calibration(table, common.control(events), discrepancies,
                        common.cast(events))

    folder = Path(args.out)
    folder.mkdir(parents=True, exist_ok=True)

    (folder / "отчёт.md").write_text(
        build_report(events, table, counters, items, discrepancies, judge,
                      table_gm, judge_gm, reveal),
        encoding="utf-8",
    )
    (folder / "транскрипт.md").write_text(
        common.transcript(events, blind=False), encoding="utf-8"
    )
    (folder / "транскрипт-слепой.md").write_text(
        common.transcript(events, blind=True), encoding="utf-8"
    )
    (folder / "баллы.json").write_text(
        json.dumps({"scores": table, "gm": table_gm,
                    "counters": counters, "calibration": items,
                    "discrepancy": discrepancies,
                    "reveal": (reveal or {}).get("_summary"),
                    "combat": common.combat(events),
                    "growth": common.growth(events)},
                   ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    with (folder / "баллы.csv").open("w", encoding="utf-8", newline="") as fn:
        writer = csv.writer(fn)
        writer.writerow(["персонаж"] + common.CRITERIA
                        + [f"разброс_{d}" for d in common.CRITERIA] + ["штраф", "итог"])
        for name, data in table.items():
            writer.writerow(
                [name]
                + [data["criteria"][d]["median"] for d in common.CRITERIA]
                + [data["criteria"][d]["spread"] for d in common.CRITERIA]
                + [data["penalty"], data["total"]]
            )

    print(f"отчёт: {folder / 'отчёт.md'}")
    for item in items:
        print(f"  {item['question']:35} {item['reply'].upper():14} {item['digit']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
