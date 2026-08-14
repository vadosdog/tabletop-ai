"""Общее для скоринга: чтение лога, транскрипты, выборка, проверка цитат."""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
import rubric_schema

CRITERIA = rubric_schema.CRITERIA_NAMES
CRITERIA_GM = rubric_schema.CRITERIA_GM
PENALTIES = {"вышла из роли": -2, "сыграла за мастера": -3, "схитрила с броском": -5}


def read_log(paths: str | Path | list) -> list[dict]:
    """Один лог или несколько подряд: продолженный прогон лежит в двух файлах."""
    if isinstance(paths, (str, Path)):
        paths = [paths]
    events: list[dict] = []
    for path in paths:
        with Path(path).open(encoding="utf-8") as fn:
            for line in fn:
                line = line.strip()
                if line:
                    events.append(json.loads(line))
    return events


def characters(events: list[dict]) -> list[str]:
    start_event = next((ev for ev in events if ev.get("event_type") == "старт"), None)
    if start_event and "состав" in start_event:
        return [name for name in start_event["cast"] if name != "Мастер"]
    names, order = set(), []
    for ev in events:
        name = ev.get("speaker")
        if ev.get("event_type") == "ход" and name and name not in names and name != "Мастер":
            names.add(name)
            order.append(name)
    return order


def cast(events: list[dict]) -> dict:
    """Кто на какой модели. Судье это не показывается никогда."""
    start_event = next((ev for ev in events if ev.get("event_type") == "старт"), None)
    return (start_event or {}).get("cast", {})


def control(events: list[dict]) -> str | None:
    start_event = next((ev for ev in events if ev.get("event_type") == "старт"), None)
    return (start_event or {}).get("control_player")


def down(events: list[dict]) -> dict[str, dict]:
    """Кто когда выбывал и возвращался. Судье это нужно, чтобы не занизить балл."""
    total: dict[str, dict] = {}
    for ev in events:
        kind = ev.get("event_type")
        if kind == "выбытие":
            total.setdefault(ev["speaker"], {"down": [], "comebacks": []})
            total[ev["speaker"]]["down"].append(
                {"round": ev["round"], "reason": ev.get("text", "")}
            )
        elif kind == "возвращение":
            total.setdefault(ev["speaker"], {"down": [], "comebacks": []})
            total[ev["speaker"]]["comebacks"].append(ev["round"])
    return total


def rounds_played(events: list[dict]) -> dict[str, int]:
    """Сколько кругов персонаж реально ходил — по ходам, а не по номеру круга."""
    count: dict[str, int] = {}
    for ev in events:
        if ev.get("event_type") == "ход" and ev.get("speaker") != "Мастер":
            count[ev["speaker"]] = count.get(ev["speaker"], 0) + 1
    return count


def is_legacy_format(folder) -> bool:
    """Прогон в старом формате: ключи лога и конфига по-русски.

    После перехода на латиницу scoring читает только новые прогоны. Старые
    остаются на диске как есть — материал прошлых выпусков, — но собрать по
    ним отчёт нельзя, и молча падать с KeyError на этом не годится.
    """
    from pathlib import Path
    config = Path(folder) / "config.json"
    if config.exists():
        try:
            return "мастер" in json.loads(config.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return False
    return False


def total_run(events: list[dict]) -> dict:
    """Сводит итоги всех частей прогона в один: продолжение — это второй файл."""
    totals = [ev for ev in events if ev.get("event_type") == "итог"]
    if not totals:
        return {}
    if len(totals) == 1:
        return totals[0]

    combined = dict(totals[-1])
    combined["rounds"] = max(i.get("rounds", 0) for i in totals)
    combined["minutes"] = round(sum(i.get("minutes", 0) for i in totals), 1)
    combined["rolls"] = sum(i.get("rolls", 0) for i in totals)
    combined["cost_usd"] = round(sum(i.get("cost_usd", 0) for i in totals), 4)
    tokens: dict[str, int] = {}
    for i in totals:
        for key, value in (i.get("tokens") or {}).items():
            if isinstance(value, int):
                tokens[key] = tokens.get(key, 0) + value
    combined["tokens"] = tokens
    combined["parts"] = len(totals)
    return combined


def resources(events: list[dict]) -> dict:
    """Сводка по ресурсам, пересчитанная из сырых событий, а не из итога.

    Событие «ресурс» — единственный источник правды: итог считается в конце
    прогона и однажды уже посчитал неверно (неудавшееся начисление Решимости
    падало в графу трат). Сырьё в логе при этом было целым, поэтому здесь мы
    им и пользуемся, а из итога берём только то, чего в событиях нет:
    остаток на финише и число провалов.
    """
    summary: dict[str, dict] = {}
    of_total = (total_run(events) or {}).get("resources") or {}
    for name, dt in of_total.items():
        summary[name] = {
            "claimed": {}, "confirmed": {}, "resolve_awarded": 0,
            "failures_could_before_reroll": dt.get(
                "failures_could_before_reroll", 0),
            "on_finish": dt.get("on_finish") or {},
            "motivation": dt.get("motivation", ""),
        }

    for ev in events:
        if ev.get("event_type") != "ресурс":
            continue
        line = summary.setdefault(ev.get("who", "?"), {
            "claimed": {}, "confirmed": {}, "resolve_awarded": 0,
            "failures_could_before_reroll": 0, "on_finish": {}, "motivation": "",
        })
        # Начисление опознаётся по наличию поля, а не по его величине: у
        # неудавшегося начисления там ноль, и по «если начислено» оно
        # засчитывалось игроку за попытку потратить.
        award = ev.get("variant") == "начисление" or "начислено" in ev
        if award:
            line["resolve_awarded"] += ev.get("awarded") or 0
            continue
        resource = ev.get("resource", "?")
        line["claimed"][resource] = line["claimed"].get(resource, 0) + 1
        if ev.get("confirmed"):
            line["confirmed"][resource] = line["confirmed"].get(resource, 0) + 1
    return summary


def rounds(events: list[dict]) -> list[int]:
    numbers = sorted({ev["round"] for ev in events if ev.get("event_type") == "ход"})
    return numbers


def choose_rounds(events: list[dict], amount: int, start: int | None = None) -> list[int]:
    """Непрерывная выборка кругов: человеку нужен контекст, а не нарезка вразбивку."""
    all_of = [d for d in rounds(events) if d > 0]
    if len(all_of) <= amount:
        return all_of
    if start is None:
        # По центру сессии: завязка уже прошла, развязка ещё не началась.
        centre = len(all_of) // 2
        first = max(0, centre - amount // 2)
    else:
        first = max(0, all_of.index(start) if start in all_of else 0)
    return all_of[first: first + amount]


# --- транскрипт -----------------------------------------------------------

HEADINGS_ROUNDS = {0: "Круг 0. Вечер накануне, знакомство у очага"}


def transcript(
    events: list[dict],
    *,
    blind: bool = True,
    only_rounds: list[int] | None = None,
) -> str:
    """Читаемый лог. В слепом виде нет ни провайдеров, ни моделей, ни стоимости."""
    sample = set(only_rounds) if only_rounds is not None else None
    lines: list[str] = ["# ЛОГ СЕССИИ", ""]

    if not blind:
        lines.append("Состав:")
        for name, settings in cast(events).items():
            lines.append(f"- {name}: {settings.get('provider')} / {settings.get('model')}")
        ctrl = control(events)
        if ctrl:
            lines.append(f"- контрольный игрок: {ctrl}")
        lines.append("")

    total_of_rounds = max(rounds(events) or [0])
    spent = down(events)
    played = rounds_played(events)
    if spent:
        lines += ["## Кто сколько отыграл", ""]
        for name, amount in played.items():
            tail = ""
            when = spent.get(name)
            if when:
                chunks = [f"выбыл в круге {v['round']} ({v['reason']})"
                         for v in when["down"]]
                chunks += [f"вернулся в круге {d}" for d in when["comebacks"]]
                tail = " — " + "; ".join(chunks)
            lines.append(f"- {name}: ходов {amount} из {total_of_rounds + 1}{tail}")
        lines += [
            "",
            "Молчание выбывшего — не игра игрока: его в эти круги не спрашивали.",
            "",
        ]

    current = None
    for ev in events:
        kind = ev.get("event_type")
        round_no = ev.get("round")
        if kind in ("старт", "итог", "доставка"):
            continue
        if sample is not None and round_no not in sample:
            continue

        if round_no != current:
            current = round_no
            header = HEADINGS_ROUNDS.get(round_no)
            if header is None:
                event_round = next(
                    (d for d in events
                     if d.get("event_type") == "круг" and d.get("round") == round_no),
                    {},
                )
                mode = event_round.get("mode", "")
                order = ", ".join(event_round.get("order", []))
                header = f"Круг {round_no}." + (f" Режим: {mode}." if mode else "")
                header += f" Порядок хода: {order}." if order else ""
            lines += ["", f"## {header}", ""]

        if kind == "ход":
            lines.append(_line(ev))
        elif kind == "бросок":
            c = ev["roll"]
            total = "успех" if c["success"] else "провал"
            lines.append(
                f"[БРОСОК СКРИПТА] {c['character']}, {c['skill']}, {c['difficulty']} — "
                f"выпало {c['rolled']}, цель {c['target']}, {total}, "
                f"уровней успеха {c['success_levels']:+d}"
            )
        elif kind == "аномалия" and "самоброс" in ev.get("tags", []):
            lines.append(
                f"[ПОМЕТКА СКРИПТА] {ev['speaker']} назвал число броска сам. "
                f"Бросок отклонён, скрипт подставил настоящий."
            )
        elif kind == "атака":
            at = ev["attack"]
            chunks = [f"[БОЙ] {at['attacker']} → {at['target']} ({at['weapons']})"]
            if at["hit"]:
                chunks.append(f"{at['location']}, снято ран {at['wounds_dealt']}, "
                             f"осталось {at['wounds_left']}")
                if at.get("crit"):
                    chunks.append(f"КРИТ: {at['crit']['text']}")
                if at.get("conditions"):
                    chunks.append("состояния: " + ", ".join(at["conditions"]))
                if at.get("injury"):
                    chunks.append(f"травма: {at['injury']}")
            else:
                chunks.append("промах")
            lines.append(". ".join(chunks))
        elif kind == "судьба":
            lines.append(
                f"[СУДЬБА] {ev['speaker']}: "
                + ("потратил, смерть отменена" if ev.get("spent") else "не потратил")
            )
        elif kind == "кровотечение":
            c = ev["roll"]
            lines.append(f"[КРОВОТЕЧЕНИЕ] {ev['speaker']}: {c['consequence']}")
        elif kind == "выбытие":
            lines.append(
                f"[ВЫБЫЛ ИЗ ИГРЫ] {ev['speaker']}: {ev.get('text','')}. "
                "Дальше его ходов не запрашивали."
            )
        elif kind == "возвращение":
            lines.append(f"[ВЕРНУЛСЯ В ИГРУ] {ev['speaker']}")
        elif kind == "финал":
            lines.append("[МАСТЕР ОБЪЯВИЛ ФИНАЛ]")
        else:
            continue  # служебные события в транскрипт не идут
        lines.append("")

    return "\n".join(lines).strip() + "\n"


def _line(ev: dict) -> str:
    who = ev["speaker"].upper()
    visibility = ev.get("visibility", "всем")
    order = ev.get("order_in_round")

    marks = []
    if visibility == "только мастеру":
        marks.append("ТАЙНЫЙ ХОД, остальные игроки его не видели")
    elif visibility.startswith("только "):
        marks.append(f"видел только {visibility.removeprefix('только ')}")
    if who != "МАСТЕР" and order:
        marks.append(f"{order}-й в круге")

    tail = f" [{'; '.join(marks)}]" if marks else ""
    return f"{who}{tail}: {ev.get('text', '')}"


def combat(events: list[dict]) -> dict:
    """Счётчики боя. Берутся из итога прогона, а если его нет — из событий."""
    total = total_run(events).get("combat")
    if total:
        return total
    attacks = [ev for ev in events if ev.get("event_type") == "атака"]
    if not attacks:
        return {}
    wounds = {}
    for ev in attacks:
        at = ev["attack"]
        wounds[at["target"]] = wounds.get(at["target"], 0) + at["wounds_dealt"]
    return {
        "attacks": len(attacks),
        "hits": sum(1 for ev in attacks if ev["attack"]["hit"]),
        "crits": sum(1 for ev in attacks if ev["attack"].get("crit")),
        "wounds_lost": wounds,
    }


def growth(events: list[dict]) -> list[dict]:
    """Размер контекста и время круга — цифры для статьи.

    Контекст меряется токенами на **одного агента**: только эта цифра говорит,
    близко ли до потолка окна модели. Сумма по пятерым за круг ничего не значит
    — у каждого агента своё окно, и складывать их незачем; она идёт отдельной
    колонкой как мера расхода, а не как размер контекста.

    У мастера за круг бывает несколько вызовов (он останавливается на проверке и
    продолжает после броска), поэтому его токены делятся на число ответов в
    сцене — иначе выйдет сумма двух перекрывающихся контекстов.

    Время — по часам, от конца прошлого круга до конца этого. Сумма латентностей
    агентов для разговорного круга совпала бы с ним, а для параллельного
    завысила бы вчетверо.
    """
    by_rounds: dict[int, dict] = {}
    end: dict[int, float] = {}
    for ev in events:
        round_no = ev.get("round")
        if round_no is None:
            continue
        end[round_no] = max(end.get(round_no, 0.0), ev.get("seconds_from_start") or 0.0)
        if ev.get("event_type") != "ход" or not (
            ev.get("context_chars") or ev.get("tokens")
        ):
            continue
        line = by_rounds.setdefault(
            round_no, {"round": round_no, "fresh_chars": 0, "tokens_total_of": 0,
                   "context_agent": 0, "у_кого": "", "seconds": 0.0}
        )
        line["fresh_chars"] += ev.get("context_chars") or 0
        tokens = ev.get("tokens") or {}
        total_of = sum(
            tokens.get(d, 0) or 0
            for d in ("input_tokens", "cache_read_input_tokens",
                      "cache_creation_input_tokens")
        )
        line["tokens_total_of"] += total_of
        on_call = total_of // max(ev.get("replies_v_scene") or 1, 1)
        if on_call > line["context_agent"]:
            line["context_agent"] = on_call
            line["у_кого"] = ev.get("speaker", "")

    previous = 0.0
    for round_no in sorted(by_rounds):
        by_rounds[round_no]["seconds"] = round(max(end.get(round_no, 0.0) - previous, 0.0), 1)
        previous = end.get(round_no, previous)
    return [by_rounds[d] for d in sorted(by_rounds)]


# --- цитаты ---------------------------------------------------------------

def _normalise(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").lower().replace("ё", "е")
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def check_quote(events: list[dict], round_no: int | None, quote: str) -> str:
    """«в круге», «в логе», «частично» или «не найдена».

    Балл без цитаты не засчитывается — значит, цитату надо уметь проверить.
    «Частично» — судья склеил куски разных предложений, но каждый кусок в логе
    есть. Это не выдумка, засчитываем с пометкой. Выдумку не засчитываем.
    """
    needle = _normalise(quote)
    if len(needle) < 12:
        return "не найдена"

    texts = [
        (ev.get("round"), _normalise(ev.get("text", "")))
        for ev in events if ev.get("event_type") in ("ход", "аномалия")
    ]

    everywhere = ""
    for index, haystack in texts:
        if needle in haystack:
            if round_no is not None and index == round_no:
                return "в круге"
            everywhere = "в логе"
    if everywhere:
        return everywhere

    chunks = [d for d in re.split(r"[.!?…]+", quote) if len(_normalise(d)) >= 20]
    if chunks and all(
        any(_normalise(chunk) in haystack for _, haystack in texts) for chunk in chunks
    ):
        return "частично"
    return "не найдена"


# --- разбор ответа судьи ---------------------------------------------------

def extract_json(text: str) -> dict:
    """Судья иногда оборачивает JSON в текст или в тройные кавычки."""
    v_quotes = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidate = v_quotes.group(1) if v_quotes else None
    if candidate is None:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("в ответе судьи нет JSON")
        candidate = text[start: end + 1]
    return json.loads(candidate)
