#!/usr/bin/env python3
"""Запуск прогона.

    python3 orchestrator/run.py --provider stub          # сухой прогон, без токенов
    python3 orchestrator/run.py --draw-vendors           # жребий: кому какой вендор
    python3 orchestrator/run.py --smoke openrouter       # дым-тест на одном вендоре
    python3 orchestrator/run.py                          # живой прогон

Живой прогон идёт на подписке. Запускать из чистого шелла: ANTHROPIC_API_KEY
перебивает подписку и жжёт платные кредиты, ANTHROPIC_BASE_URL уводит запросы
не туда. Скрипт вычищает их сам, если не передан --keep-env.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from src import protocol  # noqa: E402
from src.dice import Dice  # noqa: E402
from src.logbook import Logbook  # noqa: E402
from src.combat import Combat  # noqa: E402
from src.prompts import Prompts  # noqa: E402
from src.restore import restore  # noqa: E402
from src.providers import base as providers  # noqa: E402
from src.providers import settings  # noqa: E402
from src.session import Run  # noqa: E402

UNSAFE_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")
MSK = timezone(timedelta(hours=3))


def tag_time() -> str:
    """Дата и время старта по Москве — чтобы папки сортировались по порядку."""
    return datetime.now(MSK).strftime("%Y-%m-%d_%H:%M")


def clean_env(keep: bool) -> list[str]:
    cleared = []
    for key in UNSAFE_ENV:
        if os.environ.get(key):
            if keep:
                print(f"! {key} оставлен в окружении по --keep-env")
            else:
                os.environ.pop(key)
                cleared.append(key)
    if cleared:
        print(f"— вычищено из окружения: {', '.join(cleared)} (идём на подписке)")
    return cleared


def draw_vendors(config: dict, path: Path,
                    avoid_retries: bool = False) -> dict[str, str]:
    """Кому какой вендор. Раздаёт генератор по зерну, не автор.

    Листы персонажей неравны: у Лизель Обаяние 52 и социальный набор, у Курта
    драка. Судья оценивает драму и раскрытие тайны, так что раздача руками дала
    бы повод сказать, что вендора подобрали под персонажа. Зерно снимает вопрос.
    """
    vendors = config.get("player_vendors") or {}
    if not vendors:
        raise SystemExit("в конфиге нет блока «вендоры_игроков» — раздавать нечего")
    names = list(config["base_order"])
    if len(vendors) != len(names):
        raise SystemExit(
            f"вендоров {len(vendors)}, персонажей {len(names)} — раздача не сойдётся"
        )

    rng = random.Random(config["seed"])
    # Сортировка до перемешивания: иначе жребий зависел бы от порядка ключей
    # в файле, и его нельзя было бы воспроизвести по одному зерну.
    order = sorted(vendors)

    # Второй прогон нужен, чтобы отделить модель от листа персонажа. Если
    # вендор остался на прежнем персонаже, по этой паре мы ничего нового не
    # узнаём: непонятно, кого мы измерили — модель или монаха с молотом.
    # Поэтому требуем полной перестановки: у всех новый персонаж.
    # Разводим со ВСЕМИ прошлыми раздачами, а не только с последней. Иначе
    # третий жребий может повторить первый — так и вышло на зерне 20260813,
    # и слот прогона ушёл бы впустую: по повторной паре нового не узнать.
    history = config.get("draw_history") or []
    previous = config.get("last_draw") or {}
    if previous and previous not in history:
        history = history + [previous]

    if avoid_retries and history:
        for attempt in range(2000):
            rng.shuffle(order)
            if all(all(v != former.get(i) for i, v in zip(names, order))
                   for former in history):
                break
        else:
            raise SystemExit(
                f"не удалось развести вендоров со всеми {len(history)} прошлыми "
                "раздачами: вариантов больше нет. Либо снимите --avoid-repeat, "
                "либо очистите «история_раздач» в конфиге — но тогда повторная "
                "пара «модель-персонаж» ничего нового не покажет."
            )
    else:
        rng.shuffle(order)

    draw = {}
    for name, vendor in zip(names, order):
        config["players"][name] = {"provider": vendor, "model": vendors[vendor]}
        draw[name] = vendor
    # Запоминаем всю историю, чтобы следующий жребий разводился от каждой.
    config["last_draw"] = draw
    config["draw_history"] = history + [draw]

    path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"— жребий вендоров (зерно {config['seed']})"
          + (f", разведён со всеми {len(history)} прошлыми раздачами"
             if avoid_retries and history else "") + ":")
    for name, vendor in draw.items():
        earlier = [c[name] for c in history if c.get(name)]
        mark = f"   (раньше: {', '.join(earlier)})" if earlier else ""
        print(f"    {name:8} → {vendor} / {vendors[vendor]}{mark}")
    return draw


# Иероглифы, кана, хангыль и — что важнее всего — восточная пунктуация. У
# моделей с сильным китайским уклоном в середину русской реплики проскакивает
# не столько 好, сколько 。и ，: они похожи на обычные точку с запятой, глаз за
# них в потоке текста не цепляется, а на экране зритель их увидит.
FOREIGN_LETTER = re.compile(
    "["
    "\u3000-\u303f"    # восточная пунктуация: 。、「」《》
    "\u3040-\u30ff"    # хирагана и катакана
    "\u3400-\u4dbf"    # иероглифы, дополнение A
    "\u4e00-\u9fff"    # основные иероглифы
    "\uf900-\ufaff"    # иероглифы совместимости
    "\uac00-\ud7af"    # хангыль
    "\uff00-\uffef"    # полноширинные формы: ，．！？
    "]"
)


def export_lines(folder: Path) -> Path:
    """Дым-тест: только реплики подряд, без служебных блоков.

    Смотреть надо на язык, а не на механику. Урок Haiku — «уходю», «троем
    остались», «замораживается на полу» — стоил целого прогона, и заметить это
    можно только глазами, в сплошном тексте, без тегов, бросков и меток.

    Машина здесь помогает только в одном: находит буквы чужих алфавитов. Всё
    остальное — кальки, вывихнутые падежи, неживые обороты — ловится только
    чтением, и файл сделан для того, чтобы его прочитали целиком.
    """
    from src.logbook import read

    lines: list[str] = []
    findings: list[str] = []
    round = None
    for event in read(folder / "лог.jsonl"):
        if event.get("event_type") != "ход" or not event.get("text"):
            continue
        if event.get("round") != round:
            round = event.get("round")
            lines.append(f"\n─── круг {round} ───\n")
        text = event["text"].strip()
        foreign = FOREIGN_LETTER.findall(text)
        mark = f"  ⚠️ ЧУЖОЕ ПИСЬМО: {' '.join(sorted(set(foreign)))}" if foreign else ""
        if foreign:
            findings.append(
                f"круг {round}, {event['speaker']}: {' '.join(sorted(set(foreign)))}"
            )
        lines.append(f"{event['speaker'].upper()}:{mark} {text}\n")

    header = ["# Реплики дым-теста — прочитать целиком, глазами", ""]
    if findings:
        header += ["## ⚠️ Найдены буквы чужих алфавитов", ""]
        header += [f"- {n}" for n in findings]
        header += ["", "Ниже они помечены прямо в тексте.", ""]
    else:
        header += ["Букв чужих алфавитов не найдено. Это всё, что проверил автомат: "
                  "кальки, падежи и мёртвые обороты он не видит — читайте.", ""]

    path = folder / "реплики.txt"
    path.write_text("\n".join(header + lines).strip() + "\n", encoding="utf-8")
    return path


async def run_session(config: dict, args) -> Run:
    folder = Path(args.out)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "config.json").write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    document = (ROOT / config["document"]).resolve()
    prompts = Prompts(document, ROOT)
    logbook = Logbook(folder / "лог.jsonl")
    dice = Dice(config, config["seed"], config.get("doubles_share", 0.0))

    names = {config["gm"]["provider"]} | {
        n["provider"] for n in config["players"].values()
    }
    params = settings.GenerationParams(**config.get("generation_params", {}))
    prices = {d: zn for d, zn in (config.get("price_per_million_usd") or {}).items()
            if not d.startswith("_")}

    # Провайдеры пишут в тот же лог: повторы, отказы, снятые параметры. Номер
    # круга берётся на момент записи, поэтому прогон объявлен заранее пустым.
    run: Run | None = None

    def v_logbook(event_type: str, **fields):
        logbook.write(event_type, round=run.round if run else 0, **fields)

    connected = {
        name: providers.create(
            name,
            limit_cost=config.get("cost_limit_usd_per_agent"),
            params=params,
            prices=prices,
            logbook=v_logbook,
            # Кому прослойке разрешено отдавать запрос. Прямые подключения этот
            # довод игнорируют, у них маршрутизировать нечего.
            upstreams=config.get("openrouter_upstreams"),
            # Кто в сравнении. Мастер и судья вне его, им уровень размышления
            # не выравнивают.
            players=set(config["players"]),
        )
        for name in names
    }

    combat = Combat(config, dice, ROOT / "crits.json")
    run = Run(config, prompts, logbook, dice, connected, combat, params)
    try:
        await run.setup()
        if args.note:
            run.deliver(
                protocol.GM, f"СЛУЖЕБНОЕ. {args.note}", "скрипт"
            )

        if args.continue_from:
            past = [json.loads(s) for s in
                       Path(args.continue_from).read_text(encoding="utf-8").splitlines() if s.strip()]
            history, state = restore(past, run.names)
            state.source = str(args.continue_from)
            run.resume(history, state)
            print(f"— продолжаю с круга {state.last_round + 1}, режим "
                  f"{state.mode}, подано "
                  f"{sum(len(tx) for tx in history.values()) // 1000} тыс. символов истории")
            await run.play(s_round_zero=False)
        elif args.round_zero_only:
            try:
                await run.round_zero()
                run.stop = "только круг ноль"
            finally:
                run._totals()
        else:
            await run.play()
    finally:
        await run.shutdown()
        logbook.close()
    return run


def main() -> int:
    parsed = argparse.ArgumentParser(description="Оркестратор прогона НРИ")
    parsed.add_argument("--config", default=str(ROOT / "config.json"))
    parsed.add_argument("--out", default=None, help="папка прогона")
    parsed.add_argument("--provider", default=None,
                        help="перебить провайдера у всех агентов, например stub")
    parsed.add_argument("--model", default=None, help="перебить модель у всех агентов")
    parsed.add_argument("--rounds", type=int, default=None)
    parsed.add_argument("--seed", type=int, default=None)
    parsed.add_argument("--round-zero-only", action="store_true",
                        help="остановиться после круга ноль и вводной сцены")
    parsed.add_argument("--continue-from", default=None,
                        help="лог прерванного прогона: продолжить с последнего круга")
    parsed.add_argument("--final-from", type=int, default=None,
                        help="с какого круга подталкивать мастера к развязке")
    parsed.add_argument("--hard-final-from", type=int, default=None,
                        help="с какого круга требовать развязку и тег [ФИНАЛ]")
    parsed.add_argument("--doubles", type=float, default=0.0,
                        help="ПРОВЕРОЧНЫЙ РЕЖИМ: доля подкрученных дублей, 0..1")
    parsed.add_argument("--note", default=None,
                        help="разовое служебное указание мастеру в начале прогона")
    parsed.add_argument("--draw-vendors", action="store_true",
                        help="бросить жребий вендоров по зерну, записать в конфиг и выйти")
    parsed.add_argument("--avoid-repeat", action="store_true",
                        help="жребий: у всех вендоров новый персонаж относительно "
                             "прошлой раздачи — иначе повторную пару нечем объяснить")
    parsed.add_argument("--smoke", default=None, metavar="ПРОВАЙДЕР",
                        help="дым-тест: пять кругов на одном провайдере, "
                             "на выходе только реплики подряд")
    parsed.add_argument("--keep-env", action="store_true",
                        help="не вычищать ANTHROPIC_* из окружения")
    args = parsed.parse_args()

    path_config = Path(args.config)
    config = json.loads(path_config.read_text(encoding="utf-8"))

    if args.seed is not None:
        config["seed"] = args.seed
    if args.doubles:
        config["doubles_share"] = args.doubles
        print(f"! ПОДКРУЧЕННЫЙ ГЕНЕРАТОР: дубли в {args.doubles:.0%} бросков. "
              "Прогон проверочный, за настоящий его выдавать нельзя.")
    if args.draw_vendors:
        draw_vendors(config, path_config, args.avoid_repeat)
        return 0
    if args.smoke:
        # Дым-тест: один вендор за всех, пять кругов. Задача одна — прочитать
        # глазами и убедиться, что модель не ломает русский. Мастера подменяем
        # тоже: иначе он останется на Claude и уйдёт живым запросом с чужой
        # моделью, а дым-тест перестанет быть тестом одного вендора.
        config["max_rounds"] = args.rounds or 5
        vendors = config.get("player_vendors") or {}
        model = args.model or vendors.get(args.smoke)
        if not model:
            print(f"! не знаю, какую модель брать у {args.smoke!r}: "
                  "нет в «вендоры_игроков» и не задана через --model")
            return 2
        for settings_agent in [config["gm"], *config["players"].values()]:
            settings_agent["provider"] = args.smoke
            settings_agent["model"] = model
    if args.rounds is not None and not args.smoke:
        config["max_rounds"] = args.rounds
    if args.final_from is not None:
        config["finale_from_round"] = args.final_from
    if args.hard_final_from is not None:
        config["hard_finale_from_round"] = args.hard_final_from
    if args.provider:
        config["gm"]["provider"] = args.provider
        for settings_player in config["players"].values():
            settings_player["provider"] = args.provider
    if args.model:
        config["gm"]["model"] = args.model
        for settings_player in config["players"].values():
            settings_player["model"] = args.model

    dry = (args.provider or args.smoke or "").startswith(("stub", "заглуш"))
    if not dry:
        clean_env(args.keep_env)

    # Ключи проверяются до первого запроса и только у занятых вендоров. Упасть
    # на двадцатом круге из-за отсутствующего ключа — худший из вариантов.
    taken = {config["gm"]["provider"]} | {
        n["provider"] for n in config["players"].values()
    }
    key_report = settings.check_keys(taken)
    for name, tail in sorted(key_report.found.items()):
        print(f"— ключ {name}: {tail}")
    for name in key_report.without_key:
        print(f"— {name}: без ключа (подписка или заглушка)")

    if args.out is None:
        variant = "сухой" if dry else ("дым" if args.smoke else "прогон")
        args.out = str(ROOT.parent / "runs" / f"{tag_time()}_{variant}")
    else:
        # Имя дали руками — всё равно ставим дату и время впереди.
        path = Path(args.out)
        if not re.match(r"\d{4}-\d{2}-\d{2}_\d{2}:\d{2}_", path.name):
            args.out = str(path.with_name(f"{tag_time()}_{path.name}"))

    run = asyncio.run(run_session(config, args))

    print()
    print(f"кругов: {run.round}, остановка: {run.stop}")
    print(f"бросков: {run.dice.rolls}, зерно: {run.dice.seed}"
          + (f", подкручено {run.dice.rigged}"
             if run.dice.doubles_share else ""))
    print(f"токенов: {run.token_totals}, стоимость: ${run.cost:.4f}")
    for name, data in run.counters.items():
        print(f"  {name:8} ходов {data['turns']:3}  тайных {data['secret']:2}  "
              f"самобросков {data['self_rolls']:2}")

    summary = run._collate_providers()
    if summary:
        print("\nпо провайдерам:")
        for name, data in sorted(summary.items()):
            cache = data.get("share_cache")
            print(f"  {name:8} вход {data.get('input', 0):7}  "
                  f"выход {data.get('output', 0):6} "
                  f"(размышление {data.get('reasoning', 0):5})  "
                  f"кэш {'—' if cache is None else f'{cache:.0%}':>4}  "
                  f"повторов {data.get('retries', 0):2}  "
                  f"отказов {data.get('refusals', 0):2}  "
                  f"${data.get('cost_usd', 0.0):.4f}")

    if run.refusals:
        print(f"\nотказов: {len(run.refusals)} — разобрать поимённо:")
        for refusal in run.refusals:
            print(f"  круг {refusal['round']:2} {refusal['who']:8} "
                  f"({refusal['provider']}): {refusal['quote'][:120]}")

    print(f"\nлог: {Path(args.out) / 'лог.jsonl'}")
    if args.smoke:
        path_lines = export_lines(Path(args.out))
        print(f"реплики для чтения глазами: {path_lines}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
