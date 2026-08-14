"""Восстановление сессии из лога: продолжить прерванный прогон.

Пять свежих агентов получают каждый свою историю — ровно то, что он видел, и
ничего сверх того. Чужие тайные ходы, чужие личные блоки и ходы, сделанные в
режиме ДЕЙСТВИЕ, в его историю не попадают, как не попадали и в прогоне.

Восстановление идёт через подачу текста, а не через resume у провайдера. Так
дороже на одну большую подачу, зато работает с любой моделью — во втором
выпуске за столом будут GPT, Gemini и Grok, и своей истории они не помнят.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import protocol

GM = protocol.GM

HEADER_PLAYER = """# ЧТО БЫЛО РАНЬШЕ

Эта сессия уже идёт. Ниже — всё, что ты видел и говорил, от первого вечера до
последней сцены. Твои собственные реплики помечены «ТЫ». Остальное — то, что
при тебе сказали другие и что описал мастер.

Прочитай и продолжай с того же места: тот же голос, та же цель, та же тайна,
те же отношения с остальными. Не пересказывай прошлое, не подводи итогов, не
здоровайся заново. Просто играй дальше.
"""

HEADER_GM = """# ЧТО БЫЛО РАНЬШЕ

Эта сессия уже идёт. Ниже — весь ход игры: ходы игроков, тайные ходы, твои
сцены и результаты бросков. Твои собственные сцены помечены «ТЫ».

Прочитай и веди дальше с того же места. Не переигрывай прошлое и не начинай
сцену заново — продолжай ровно с того, чем кончилась последняя.
"""


@dataclass
class ResumeState:
    last_round: int = 0
    mode: str = "ДЕЙСТВИЕ"
    talk_rounds: int = 0
    issued_numbers: set[int] = field(default_factory=set)
    rolls: int = 0
    source: str = ""


def restore(
    events: list[dict], names: list[str]
) -> tuple[dict[str, str], ResumeState]:
    """Возвращает историю на каждого агента и состояние прогона."""
    chunks: dict[str, list[str]] = {GM: [], **{name: [] for name in names}}
    modes: dict[int, str] = {
        ev["round"]: ev.get("mode", "ДЕЙСТВИЕ")
        for ev in events if ev.get("event_type") == "круг"
    }
    state = ResumeState()
    current_round = None

    def everyone(line: str, excluding: str | None = None) -> None:
        for name in names:
            if name != excluding:
                chunks[name].append(line)

    for ev in events:
        kind = ev.get("event_type")

        if kind == "бросок":
            roll = ev["roll"]
            state.issued_numbers.update({roll["rolled"], roll["target"]})
            state.rolls += 1
            continue  # результат уже вписан в сцену мастера, дважды не подаём

        if kind != "ход":
            continue

        round = ev.get("round", 0)
        if round != current_round:
            current_round = round
            header = ("## Круг ноль, вечер накануне" if round == 0
                     else f"## Круг {round}. Режим: {modes.get(round, 'ДЕЙСТВИЕ')}")
            chunks[GM].append(f"\n{header}")
            everyone(f"\n{header}")

        speaker = ev.get("speaker", "")
        text = (ev.get("text") or "").strip()
        visibility = ev.get("visibility", "всем")
        if not text:
            continue

        if speaker == GM:
            if visibility == "всем":
                everyone(f"МАСТЕР:\n{text}")
                chunks[GM].append(f"ТЫ (сцена):\n{text}")
            elif visibility.startswith("только "):
                to = visibility.removeprefix("только ")
                if to in chunks:
                    chunks[to].append(f"ТОЛЬКО ДЛЯ ТЕБЯ:\n{text}")
                chunks[GM].append(f"ТЫ (только для {to}):\n{text}")
            continue

        # Ход игрока.
        secret = visibility == "только мастеру"
        mark = "ТАЙНЫЙ ХОД, " if secret else ""
        chunks[GM].append(f"{mark}{speaker.upper()}: {text}")
        if speaker in chunks:
            chunks[speaker].append(f"ТЫ{' (тайно)' if secret else ''}: {text}")
        # В разговоре ход видели остальные, в действии — нет. Круг ноль виден всем.
        talk = round == 0 or modes.get(round) == protocol.TALK
        if talk and not secret:
            everyone(f"ХОД ИГРОКА — {speaker.upper()}:\n{text}", excluding=speaker)

    state.last_round = max(modes) if modes else 0
    scenes_gm = [
        ev for ev in events
        if ev.get("event_type") == "ход" and ev.get("speaker") == GM
        and ev.get("visibility") == "всем"
    ]
    if scenes_gm:
        state.mode = scenes_gm[-1].get("mode", protocol.ACTION)
    state.talk_rounds = sum(
        1 for round, mode in modes.items() if mode == protocol.TALK
    )

    history = {
        agent: (HEADER_GM if agent == GM else HEADER_PLAYER)
        + "\n" + "\n\n".join(lines).strip()
        for agent, lines in chunks.items()
    }
    return history, state
