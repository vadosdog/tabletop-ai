#!/usr/bin/env python3
"""Сухой прогон целиком и проверки по логу.

    python3 orchestrator/tests/test_routing.py

Гоняет оркестратор на заглушках и сверяет по логу то, что руками сверять дорого:
ротацию стартового игрока, изоляцию тайного хода, адресность личного блока,
умолчание режима и ловлю самобросков.
"""

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import asyncio  # noqa: E402

from src.combat import Combat  # noqa: E402
from src.dice import Dice  # noqa: E402
from src.logbook import Logbook, read  # noqa: E402
from src.prompts import Prompts  # noqa: E402
from src.providers import base as providers  # noqa: E402
from src.session import Run  # noqa: E402

CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))


def run_session() -> list[dict]:
    config = json.loads(json.dumps(CONFIG))
    config["max_rounds"] = 16
    config["gm"]["provider"] = "stub"
    for settings in config["players"].values():
        settings["provider"] = "stub"

    temp = Path(tempfile.mkdtemp(prefix="нри-тест-"))
    logbook = Logbook(temp / "лог.jsonl")
    prompts = Prompts((ROOT / config["document"]).resolve(), ROOT)
    dice = Dice(config, config["seed"])
    run = Run(
        config, prompts, logbook, dice, {"stub": providers.create("stub")},
        Combat(config, dice, ROOT / "crits.json"),
    )

    async def race():
        await run.setup()
        await run.play()
        await run.shutdown()

    asyncio.run(race())
    logbook.close()
    return read(temp / "лог.jsonl")


EVENTS = run_session()
ROUNDS = {ev["round"]: ev for ev in EVENTS if ev["event_type"] == "круг"}
TURNS = [ev for ev in EVENTS if ev["event_type"] == "ход"]
DELIVERIES = [ev for ev in EVENTS if ev["event_type"] == "доставка"]
ANOMALIES = [ev for ev in EVENTS if ev["event_type"] == "аномалия"]


def test_rotation_in_talk_mode():
    base = CONFIG["base_order"]
    talk = [ev for ev in ROUNDS.values() if ev["mode"] == "РАЗГОВОР"]
    assert len(talk) >= 5, "сухой прогон не задел пять разговорных кругов"
    for previous, following in zip(talk, talk[1:]):
        before = base.index(previous["order"][0])
        after = base.index(following["order"][0])
        assert after == (before + 1) % len(base), (
            f"стартовый игрок не сдвинулся: {previous['order'][0]} → "
            f"{following['order'][0]}"
        )


def test_turns_are_not_delivered_in_action_mode():
    for round, event in ROUNDS.items():
        if event["mode"] != "ДЕЙСТВИЕ":
            continue
        between_players = [
            dt for dt in DELIVERIES
            if dt["round"] == round and dt["sender"] in CONFIG["base_order"]
            and dt["to"] in CONFIG["base_order"]
        ]
        assert not between_players, f"в круге {round} игроки увидели друг друга в действии"


def test_secret_turn_goes_only_to_the_gm():
    secret = [t for t in TURNS if t["visibility"] == "только мастеру"]
    assert secret, "в сухом прогоне не было ни одного тайного хода"
    for turn in secret:
        leaks = [
            dt for dt in DELIVERIES
            if dt["round"] == turn["round"] and dt["sender"] == turn["speaker"]
            and dt["to"] != "Мастер"
        ]
        assert not leaks, f"тайный ход {turn['speaker']} утёк: {leaks}"
        recipient = [
            dt for dt in DELIVERIES
            if dt["round"] == turn["round"] and dt["sender"] == turn["speaker"]
            and dt["to"] == "Мастер"
        ]
        assert recipient, "тайный ход не дошёл до мастера"


def test_private_block_reaches_only_its_addressee():
    private = [t for t in TURNS if t["visibility"].startswith("только ")
              and t["speaker"] == "Мастер"]
    assert private, "мастер ни разу не выдал личный блок"
    for turn in private:
        to = turn["visibility"].removeprefix("только ")
        from_gm = [
            dt for dt in DELIVERIES if dt["round"] == turn["round"] and dt["sender"] == "Мастер"
        ]
        # Троим — только публичная сцена, адресату — сцена и личный блок.
        count = {}
        for dt in from_gm:
            count[dt["to"]] = count.get(dt["to"], 0) + 1
        assert count[to] == 2, f"{to} получил {count[to]} сообщений вместо двух"
        for other, amount in count.items():
            if other != to:
                assert amount == 1, f"{other} увидел личный блок для {to}"


def test_round_zero_is_sequential():
    zeroes = [t for t in TURNS if t["round"] == 0 and t["speaker"] != "Мастер"]
    assert [t["speaker"] for t in zeroes] == CONFIG["round_zero_order"]
    # Каждый ход немедленно уходит троим остальным.
    for turn in zeroes[:-1]:
        recipients = {dt["to"] for dt in DELIVERIES
                    if dt["round"] == 0 and dt["sender"] == turn["speaker"]}
        assert len(recipients) == 3, f"ход {turn['speaker']} ушёл не троим: {recipients}"


def test_gm_stays_silent_in_round_zero_until_introductions():
    order = [ev for ev in EVENTS if ev["event_type"] == "ход" and ev["round"] == 0]
    assert order[-1]["speaker"] == "Мастер", "мастер вступил не последним"
    assert "вводная" in order[-1].get("tags", [])


def test_self_roll_caught_on_both_sides():
    who = {at["speaker"] for at in ANOMALIES if "самоброс" in at["tags"]}
    assert "Мастер" in who and who & set(CONFIG["base_order"]), (
        f"самоброски пойманы не у всех: {who}"
    )


def test_missing_mode_tag_is_logged():
    assert any("тег_режима_пропущен" in at["tags"] for at in ANOMALIES)


def test_only_the_script_rolls_dice():
    rolls = [ev for ev in EVENTS if ev["event_type"] == "бросок"]
    assert rolls, "ни одного броска не сделано"
    for ev in rolls:
        assert ev["speaker"] == "скрипт"
        assert 1 <= ev["roll"]["rolled"] <= 100
        assert ev["roll"]["seed"] == CONFIG["seed"]


def test_going_down_excludes_from_the_round():
    down_events = [ev for ev in EVENTS if ev["event_type"] == "выбытие"]
    assert down_events, "в сухом прогоне никто не выбыл"
    down = down_events[0]["speaker"]
    round_down = down_events[0]["round"]
    comeback = next(
        (ev for ev in EVENTS if ev["event_type"] == "возвращение"
         and ev["speaker"] == down), None
    )
    assert comeback, "выбывший так и не вернулся"

    absent = range(round_down + 1, comeback["round"] + 1)
    for round in absent:
        event = ROUNDS.get(round)
        if not event:
            continue
        assert down not in event["order"], (
            f"выбывшего {down} спросили в круге {round}"
        )
        turns = [t for t in TURNS if t["round"] == round and t["speaker"] == down]
        assert not turns, f"выбывший {down} сходил в круге {round}"


def test_nothing_is_delivered_to_a_downed_player():
    down_events = [ev for ev in EVENTS if ev["event_type"] == "выбытие"]
    comebacks = [ev for ev in EVENTS if ev["event_type"] == "возвращение"]
    if not down_events:
        return
    who, from_round = down_events[0]["speaker"], down_events[0]["round"]
    to_round = comebacks[0]["round"] if comebacks else max(ROUNDS) + 1
    leaks = [dt for dt in DELIVERIES
              if dt["to"] == who and from_round < dt["round"] <= to_round]
    assert not leaks, f"выбывшему {who} что-то доставили: {leaks[:2]}"


def test_comeback_returns_to_the_rotation():
    comebacks = [ev for ev in EVENTS if ev["event_type"] == "возвращение"]
    assert comebacks
    who, round = comebacks[0]["speaker"], comebacks[0]["round"]
    following = [d for n, d in sorted(ROUNDS.items()) if n > round]
    assert following, "после возвращения не осталось кругов — проверка бессмысленна"
    assert who in following[0]["order"], "вернувшегося не спросили"


def test_watchdog_for_a_stuck_mode():
    assert any("залипание_режима" in at["tags"] for at in ANOMALIES), (
        "три круга ДЕЙСТВИЯ подряд не дали предупреждения"
    )


def test_round_counter_in_the_header():
    # Мастер должен видеть «Круг N из M» — без этого он не торопится.
    for index, event in ROUNDS.items():
        header = event.get("header_gm", "")
        assert f"Круг {index} из 16" in header, header
    # Выбывшие тоже перечислены, иначе мастер забудет, кого не спрашивать.
    after_down = [ev for ev in ROUNDS.values() if ev.get("down")]
    assert after_down and "Выбыли:" in after_down[0]["header_gm"]


def test_rounds_played_in_the_summary():
    total = [ev for ev in EVENTS if ev["event_type"] == "итог"][-1]
    played = total.get("rounds_played")
    assert played and set(played) == set(CONFIG["base_order"])


def test_run_stops_on_the_finale_tag():
    total = [ev for ev in EVENTS if ev["event_type"] == "итог"][-1]
    assert total["stop"] == "финал", total["stop"]


if __name__ == "__main__":
    failed = 0
    for name, func in sorted(globals().items()):
        if name.startswith("test_"):
            try:
                func()
                print(f"  ok   {name}")
            except AssertionError as error:
                failed += 1
                print(f"  ПРОВАЛ {name}: {error}")
    print("маршрутизация в порядке" if not failed else f"провалено: {failed}")
    sys.exit(1 if failed else 0)
