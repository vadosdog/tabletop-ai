"""Счётчики без модели. Считаются прямо по логу, судья к ним не привлекается.

В таблицу идут без баллов: тайные ходы, средняя длина реплики, обращения и
ответы. Последнее — эвристика по имени, и в отчёте она так и подписана.
"""

from __future__ import annotations

import re
import unicodedata


def _normalise(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").lower().replace("ё", "е")
    return re.sub(r"\s+", " ", text)


def _mentioned(text: str, name: str) -> bool:
    """Имя в реплике. По корню, чтобы поймать «Курта», «Ханне», «Лизель»."""
    root = _normalise(name)[:4]
    return bool(re.search(rf"\b{re.escape(root)}\w*", _normalise(text)))


def compute(events: list[dict], names: list[str]) -> dict[str, dict]:
    total = {
        name: {
            "turns": 0, "secret_turns": 0, "self_rolls": 0, "chars": 0,
            "mean_length_lines": 0.0, "обратились_к_нему": 0, "answered": 0,
            "rounds_played": 0, "down_v_round": None,
        }
        for name in names
    }

    turns = [ev for ev in events if ev["event_type"] == "ход" and ev["speaker"] in names]
    for turn in turns:
        data = total[turn["speaker"]]
        data["turns"] += 1
        data["chars"] += len(turn.get("text", ""))
        if turn.get("visibility") == "только мастеру":
            data["secret_turns"] += 1
        if "самоброс" in turn.get("tags", []):
            data["self_rolls"] += 1

    for name, data in total.items():
        data["rounds_played"] = len(
            {t["round"] for t in turns if t["speaker"] == name}
        )
    for ev in events:
        if ev.get("event_type") == "выбытие" and ev.get("speaker") in total:
            total[ev["speaker"]]["down_v_round"] = ev["round"]

    for name, data in total.items():
        data["mean_length_lines"] = (
            round(data["chars"] / data["turns"], 1) if data["turns"] else 0.0
        )

    # Обращение: реплика A упоминает B. Ответ: ближайший следующий ход B
    # упоминает A. Эвристика, но повторяемая и проверяемая по логу.
    for index, turn in enumerate(turns):
        speaker = turn["speaker"]
        if turn.get("visibility") == "только мастеру":
            continue
        for recipient in names:
            if recipient == speaker or not _mentioned(turn.get("text", ""), recipient):
                continue
            total[recipient]["обратились_к_нему"] += 1
            following = next(
                (t for t in turns[index + 1:] if t["speaker"] == recipient), None
            )
            if following and _mentioned(following.get("text", ""), speaker):
                total[recipient]["answered"] += 1

    return total
