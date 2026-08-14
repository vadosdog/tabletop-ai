"""Оркестратор сессии: пять контекстов, режимы, ротация, тайные ходы, броски.

Скрипт не знает правил WFRP — только сотню и таблицу сложности. Никакой игровой
логики здесь нет и быть не должно, она вся в промптах. Здесь только доставка:
кто что видит, в каком порядке и что уходит в лог.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field

from . import parse
from . import protocol
from .combat import Combat
from .dice import Dice
from .logbook import Logbook
from .prompts import Prompts
from .providers import settings as generation_params
from .providers.base import Reply, Provider, Session

GM = protocol.GM


class RunStopped(RuntimeError):
    """Агент сломался или не ответил. Лог при этом уже на диске."""


@dataclass
class Turn:
    character: str
    text: str
    secret: bool
    order: int
    tags: list[str] = field(default_factory=list)
    self_roll: str | None = None
    refusal: bool = False


class Run:
    def __init__(
        self,
        config: dict,
        prompts: Prompts,
        logbook: Logbook,
        dice: Dice,
        providers: dict[str, Provider],
        combat: Combat | None = None,
        params: generation_params.GenerationParams | None = None,
    ):
        self.config = config
        self.params = params or generation_params.GenerationParams()
        self.prompts = prompts
        self.logbook = logbook
        self.dice = dice
        self.providers = providers
        self.combat = combat

        self.names: list[str] = list(config["base_order"])
        self.sessions: dict[str, Session] = {}
        self.queue: dict[str, list[str]] = {GM: []}
        self.queue.update({name: [] for name in self.names})

        self.round = 0
        self.mode = config.get("default_mode", protocol.DEFAULT_MODE)
        self.talk_rounds = 0
        self.stop: str | None = None
        # Выбывший — это мёртвый, без сознания или тот, чья история кончилась.
        # Если персонаж слышит и видит происходящее, он не выбыл и ходит как все.
        self.down: dict[str, dict] = {}
        self.played: dict[str, int] = {name: 0 for name in self.names}
        self.actions_in_row = 0
        self._context: dict[str, int] = {}

        self.counters = {
            name: {"turns": 0, "secret": 0, "self_rolls": 0, "chars": 0}
            for name in self.names + [GM]
        }
        self.token_totals = {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0,
                        "reasoning": 0}
        # Отдельный счётчик на каждого вендора: без него нельзя сказать, сколько
        # стоил прогон у кого, а это половина статьи.
        self.provider_totals: dict[str, dict] = {}
        self.refusals: list[dict] = []
        self.actual_models: dict[str, str] = {}
        self.cost = 0.0
        # Числа, которые скрипт выдал сам. Их пересказ моделью — не самоброс.
        self.issued_numbers: set[int] = set()
        self.fate_queue: list[str] = []
        # Заявки на Удачу. Игроки ходят раньше, чем мастер объявит проверки,
        # поэтому переброс заявляется наперёд: «если провалю — перекину».
        # Заявка живёт один круг и сгорает вместе с ним.
        self.claims_fortune: dict[str, str] = {}
        # Заявки на Судьбу: имя → выбранное игроком значение на кубах.
        self.claims_fate: dict[str, int] = {}
        # Провалы игроков, которые можно было перебросить за Удачу. Нужны,
        # чтобы упрёк «доиграл с полными руками» можно было проверить: без
        # этого числа непонятно, была ли у игрока возможность потратить.
        self.player_failures: dict[str, int] = {name: 0 for name in self.names}
        # Что скрипт подтвердил, а что только прозвучало. Разница между этими
        # двумя числами — главное, чего не хватало прошлым прогонам: судья
        # засчитывал за трату слова, за которыми ничего не стояло.
        self.resources_events: list[dict] = []
        # Порча русского по игрокам: чужие слова и подменённые буквы. Идёт
        # судье как минус модели — он читает транскрипт и такое не ловит.
        self.corruption_language: dict[str, dict] = {}

    # --- подготовка -------------------------------------------------------

    async def setup(self) -> None:
        gm_config = self.config["gm"]
        self.sessions[GM] = await self.providers[gm_config["provider"]].open(
            GM, self.prompts.gm(), gm_config["model"]
        )
        for name in self.names:
            settings = self.config["players"][name]
            self.sessions[name] = await self.providers[settings["provider"]].open(
                name, self.prompts.player(name), settings["model"]
            )

        self.logbook.write(
            "старт",
            seed=self.dice.seed,
            rigging_doubles=self.dice.doubles_share or None,
            max_rounds=self.config["max_rounds"],
            mode=self.mode,
            cast={
                GM: self.config["gm"],
                **{name: self.config["players"][name] for name in self.names},
            },
            control_player=self.config.get("control_player"),
            prompts=self.prompts.summary(),
            # Условия сравнения объявляются до первого хода, а не подгоняются
            # после. Зритель должен видеть, что у всех четверых одно и то же.
            generation_params=generation_params.block_params(
                self.params,
                {GM: self.config["gm"]["provider"],
                 **{name: self.config["players"][name]["provider"] for name in self.names}},
            ),
        )

    async def shutdown(self) -> None:
        for provider in self.providers.values():
            await provider.shutdown()

    @property
    def active(self) -> list[str]:
        return [name for name in self.names if name not in self.down]

    # --- доставка ---------------------------------------------------------

    def deliver(self, to: str, text: str, sender: str) -> None:
        text = text.strip()
        if not text:
            return
        if to in self.down:
            # Выбывшему не доставляется ничего: он не воспринимает сцену.
            return
        self.queue[to].append(text)
        self.logbook.delivery(round=self.round, sender=sender, to=to, chars=len(text))

    async def ask(self, agent: str, hint: str) -> Reply:
        body = "\n\n".join(self.queue[agent] + [hint])
        self.queue[agent] = []
        # Размер отправленного — в лог: по нему видно, где начинается разгон.
        self._context[agent] = len(body)

        attempts = max(1, int(self.config.get("attempts_per_agent", 2)))
        last: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                reply = await self.sessions[agent].send(body)
            except Exception as error:  # сеть, лимиты, падение SDK
                last = error
                self.logbook.anomaly(
                    round=self.round, who=agent, tag="ошибка_агента",
                    details=f"попытка {attempt}: {error!r}",
                )
                await asyncio.sleep(2 * attempt)
                continue

            # Отказ повторять нельзя ни при каких обстоятельствах, даже если он
            # пришёл с пустым текстом. Тихий ретрай спрячет ровно то, ради чего
            # прогон затевался: как разные вендоры ведут себя на мрачной сцене.
            if "отказ" in reply.tags:
                self._count(reply)
                self._remember_model(agent, reply)
                return reply

            if reply.text.strip():
                self._count(reply)
                self._remember_model(agent, reply)
                return reply

            last = RuntimeError("пустой ответ")
            self.logbook.anomaly(
                round=self.round, who=agent, tag="пустой_ответ",
                details=f"попытка {attempt}",
            )

        raise RunStopped(f"агент {agent} не ответил: {last!r}")

    def _remember_model(self, agent: str, reply: Reply) -> None:
        if reply.model_actual:
            self.actual_models[agent] = reply.model_actual

    # Вендорское имя поля → наше. Сторонние адаптеры приводят usage к общему виду
    # сами, у Claude он приходит как есть из SDK.
    FIELDS_TOKENS = (
        # Слева — как поле зовёт вендор, справа — как оно зовётся у нас.
        ("input_tokens", "input"), ("output_tokens", "output"),
        ("cache_creation_input_tokens", "cache_write"),
        ("cache_read_input_tokens", "cache_read"),
        ("input", "input"), ("output", "output"),
        ("cache_write", "cache_write"), ("cache_read", "cache_read"),
        ("reasoning", "reasoning"),
    )

    def _count(self, reply: Reply) -> None:
        if reply.cost:
            self.cost += reply.cost

        counted = {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0,
                  "reasoning": 0}
        tokens = reply.tokens or {}
        # Основной объём уходит в кэш, а не в input_tokens: без этих двух полей
        # счётчик показывает полсотни токенов на прогон и врёт про расход.
        for key, field in self.FIELDS_TOKENS:
            if isinstance(tokens.get(key), int):
                counted[field] += tokens[key]

        for field, amount in counted.items():
            self.token_totals[field] += amount

        row = self.provider_totals.setdefault(
            reply.provider,
            {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0,
             "reasoning": 0, "requests": 0, "refusals": 0, "cost_usd": 0.0,
             "models": []},
        )
        for field, amount in counted.items():
            row[field] += amount
        row["requests"] += 1
        row["cost_usd"] += reply.cost or 0.0
        if "отказ" in reply.tags:
            row["refusals"] += 1
        model = reply.model_actual or reply.model
        if model and model not in row["models"]:
            row["models"].append(model)

    # --- круг ноль --------------------------------------------------------

    async def round_zero(self) -> None:
        order = self.config["round_zero_order"]
        scene = self.prompts.round_zero

        for name in order:
            self.deliver(name, scene, "посредник")

        turns: list[Turn] = []
        for index, name in enumerate(order, 1):
            reply = await self.ask(name, "Твой ход.")
            turn = self._parse_turn(name, reply, index, mode="КРУГ_НОЛЬ")
            turns.append(turn)
            # Ход немедленно уходит троим остальным: иначе персонажи говорят
            # в пустоту и не отвечают на обращения.
            for other in order:
                if other != name:
                    self.deliver(other, f"ХОД ИГРОКА — {name.upper()}:\n{turn.text}", name)

        self.deliver(GM, self._summary_turns(turns, "Круг ноль, вечер накануне."), "посредник")
        await self._scene_gm(briefing=True)

    # --- продолжение прерванного прогона ----------------------------------

    def resume(self, history: dict[str, str], state) -> None:
        """Подаёт каждому агенту его историю и восстанавливает состояние круга."""
        self.round = state.last_round
        self.mode = state.mode
        self.talk_rounds = state.talk_rounds
        self.issued_numbers = set(state.issued_numbers)
        for agent, text in history.items():
            self.deliver(agent, text, "посредник")
        self.logbook.write(
            "продолжение",
            round=self.round,
            mode=self.mode,
            talk_rounds=self.talk_rounds,
            source=state.source,
            sent_chars={agent: len(tx) for agent, tx in history.items()},
        )

    # --- основной цикл ----------------------------------------------------

    async def play(self, s_round_zero: bool = True) -> None:
        try:
            if s_round_zero:
                await self.round_zero()
            limit = int(self.config["max_rounds"])
            while self.stop is None and self.round < limit:
                self.round += 1
                await self._round()
            if self.stop is None:
                self.stop = "предел_кругов"
        except RunStopped as halt:
            self.stop = f"сбой: {halt}"
            self.logbook.anomaly(round=self.round, who="скрипт", tag="остановка",
                                 details=str(halt))
        finally:
            self._totals()

    def _nudge(self) -> None:
        """Подталкивает мастера к развязке: сам он не торопится.

        В модуле таймер до полудня прописан, но мастер им не пользуется, и
        сессия упирается в предел кругов посреди сцены.
        """
        soft = self.config.get("finale_from_round")
        hard = self.config.get("hard_finale_from_round")
        if hard and self.round >= hard:
            left = int(self.config["max_rounds"]) - self.round
            self.deliver(
                GM,
                "СЛУЖЕБНОЕ. Сессия заканчивается: осталось кругов "
                f"{max(left, 1)}. Доведи историю до развязки в этой сцене "
                "или в следующей и поставь [ФИНАЛ]. Что партия скажет патрулю — "
                "то и станет правдой; не подсказывай ей правильный ответ.",
                "скрипт",
            )
        elif soft and self.round >= soft:
            self.deliver(
                GM,
                "СЛУЖЕБНОЕ. Полдень близко: солнце высоко, лошади беспокойны, "
                "на дороге со стороны моста пыль. Веди историю к развязке, "
                "поднимай цену промедления.",
                "скрипт",
            )

    def _watchdog_mode(self) -> None:
        """Три ДЕЙСТВИЯ подряд — диалог умер, а на нём держится ролик."""
        if self.mode == "ДЕЙСТВИЕ":
            self.actions_in_row += 1
            # Предупреждаем на третьем круге и дальше каждый третий: длинный бой
            # — законная причина, а не повод засорять монтажный лист.
            if self.actions_in_row >= 3 and self.actions_in_row % 3 == 0:
                self.logbook.anomaly(
                    round=self.round, who=GM, tag="залипание_режима",
                    details=f"кругов ДЕЙСТВИЯ подряд: {self.actions_in_row}",
                )
        else:
            self.actions_in_row = 0

    async def _round(self) -> None:
        order = self._order_turn()
        self._watchdog_mode()

        if self.combat:
            # Иммунитет от Решимости живёт один круг и гаснет здесь, до бросков.
            self.combat.fresh_round()
            # Проверки кровотечения скрипт объявляет сам, не дожидаясь мастера.
            blood = await self._bleeding()
            if blood:
                self.deliver(GM, "Кровотечение:\n" + "\n".join(blood), "скрипт")
            # Строка остатка идёт КАЖДЫЙ круг и каждому, даже когда всё цело.
            # Ресурс, названный один раз в карточке, к двадцатому кругу для
            # модели не существует — это свойство подачи, а не модели.
            for name in order:
                self.deliver(name, self.combat.summary_player(name), "скрипт")
            summary_resources = self.combat.summary_resources(order)
            if summary_resources:
                self.deliver(GM, summary_resources, "скрипт")
        self.logbook.write(
            "круг", round=self.round, mode=self.mode, order=order,
            down=sorted(self.down) or None,
            header_gm=self._header_round(),
        )
        turns = await self._build_turns(order)
        header = self._header_round()
        self.deliver(GM, self._summary_turns(turns, header), "посредник")
        if self.combat:
            summary = self.combat.summary_gm()
            if summary:
                self.deliver(GM, summary, "скрипт")
        self._nudge()
        for turn in turns:
            if turn.secret:
                self.deliver(GM, f"ТАЙНЫЙ ХОД, только {turn.character}: {turn.text}", turn.character)
            if turn.self_roll:
                real = self.dice.fake()
                self.issued_numbers.add(real)
                self.deliver(
                    GM,
                    f"Бросок игрока {turn.character} недействителен, он его не кидает. "
                    f"Настоящий результат: {real}.",
                    "скрипт",
                )
        await self._scene_gm()

        if self.combat and self.fate_queue:
            outcomes = await self._ask_fate()
            if outcomes:
                self.deliver(GM, "Судьба:\n" + "\n".join(outcomes), "скрипт")
                for name in self.active:
                    self.deliver(name, "\n".join(outcomes), "скрипт")

    def _order_turn(self) -> list[str]:
        base = [name for name in self.config["base_order"] if name in self.active]
        if not base:
            return []
        if self.mode != protocol.TALK:
            return base
        # Ротация: иначе тот, кто всегда ходит последним, видит троих и
        # выглядит умнее — это исказило бы скоринг.
        offset = self.talk_rounds % len(base)
        self.talk_rounds += 1
        return base[offset:] + base[:offset]

    async def _build_turns(self, order: list[str]) -> list[Turn]:
        turns: list[Turn] = []

        if self.mode == protocol.TALK:
            for index, name in enumerate(order, 1):
                reply = await self.ask(name, "Твой ход.")
                turn = self._parse_turn(name, reply, index)
                turns.append(turn)
                if not turn.secret:
                    for other in order:
                        if other != name:
                            self.deliver(
                                other, f"ХОД ИГРОКА — {name.upper()}:\n{turn.text}", name
                            )
            return turns

        # ДЕЙСТВИЕ: параллельно и вслепую, друг друга не видят.
        results = await asyncio.gather(
            *(self.ask(name, "Твой ход.") for name in order),
            return_exceptions=True,
        )
        for index, (name, result) in enumerate(zip(order, results), 1):
            if isinstance(result, BaseException):
                raise RunStopped(f"агент {name}: {result!r}")
            turns.append(self._parse_turn(name, result, index))
        return turns

    def _parse_turn(self, name: str, reply: Reply, index: int, mode: str | None = None) -> Turn:
        text = reply.text.strip()
        tags = list(reply.tags)

        # Отказ: в лог целиком, за стол — ничего. Текст отказа не должен попасть
        # ни мастеру, ни другим игрокам, иначе он утечёт в сцену и испортит её
        # всем четверым. Персонаж просто промолчал, круг идёт дальше.
        if "отказ" in tags:
            self.counters[name]["turns"] += 1
            self.played[name] = self.round
            self.logbook.turn(
                round=self.round, mode=mode or self.mode, speaker=name,
                order_in_round=index, visibility="никому (отказ)", text=text,
                tags=tags, provider=reply.provider,
                model=reply.model_actual or reply.model,
                latency_ms=reply.latency_ms, tokens=reply.tokens,
                cost=reply.cost, context_chars=self._context.get(name),
            )
            self.refusals.append({
                "round": self.round, "who": name, "provider": reply.provider,
                "model": reply.model_actual or reply.model,
                "quote": " ".join(text.split())[:300],
            })
            return Turn(name, "", False, index, tags, None, refusal=True)

        secret = parse.secret_turn(text)
        self_roll = None
        tags += self._parse_spends(name, text)

        # Порча русского — минус модели, и судья должен его видеть числом.
        # Целиком нерусская реплика заметна и так, а подменённая буква внутри
        # слова не заметна вовсе, поэтому считает её скрипт, а не глаз.
        corruption = parse.corruption_language(text)
        if corruption["corrupted"]:
            tags.append("порча_языка")
            row = self.corruption_language.setdefault(
                name, {"lines": 0, "mixed_words": [], "wholly_not_russian": 0})
            row["lines"] += 1
            row["wholly_not_russian"] += int(corruption["wholly_not_russian"])
            for word in corruption["mixed_words"]:
                if word not in row["mixed_words"]:
                    row["mixed_words"].append(word)
            self.logbook.write(
                "порча_языка", round=self.round, who=name,
                mixed_words=corruption["mixed_words"] or None,
                wholly_not_russian=corruption["wholly_not_russian"] or None,
                quote=" ".join(text.split())[:200],
            )

        fragments = parse.self_rolls(text, self.issued_numbers)
        if fragments:
            self_roll = "; ".join(fragments)
            tags.append("самоброс")
            self.counters[name]["self_rolls"] += 1
            self.logbook.anomaly(round=self.round, who=name, tag="самоброс",
                                 details=self_roll)

        if secret:
            tags.append("тайный_ход")
            self.counters[name]["secret"] += 1

        self.counters[name]["turns"] += 1
        self.counters[name]["chars"] += len(text)
        self.played[name] = self.round

        self.logbook.turn(
            round=self.round,
            mode=mode or self.mode,
            speaker=name,
            order_in_round=index,
            visibility="только мастеру" if secret else "всем",
            text=text,
            tags=tags,
            provider=reply.provider,
            model=reply.model_actual or reply.model,
            latency_ms=reply.latency_ms,
            tokens=reply.tokens,
            cost=reply.cost,
            context_chars=self._context.get(name),
        )
        return Turn(name, text, secret, index, tags, self_roll)

    def _write_resource(self, event: dict) -> None:
        self.resources_events.append(event)
        self.logbook.write("ресурс", **event)

    def _parse_spends(self, name: str, text: str) -> list[str]:
        """Заявки игрока на ресурсы. Заявка — ещё не трата.

        Списывает скрипт и только если есть чем. Несостоявшаяся заявка тоже
        идёт в лог: именно на смешении заявленного и подтверждённого сгорел
        прошлый прогон, где судья зачёл за трату слова «Трачу Удачу».
        """
        if not self.combat or name not in self.combat.combatants:
            return []
        tags: list[str] = []
        for resource in parse.spends(text):
            stem = {"round": self.round, "who": name, "resource": resource,
                      "variant": "трата", "claimed": True}

            if resource == "fate":
                # Второе применение Судьбы: в безнадёжном положении игрок
                # тратит её и называет значение на кубах сам. Спасение от
                # смерти по-прежнему идёт отдельным запросом скрипта, здесь
                # только выбор числа.
                value = parse.chosen_value(text)
                self.claims_fate[name] = value
                # Названное число — не самоброс: игрок имел право его назвать,
                # и записывать это в жульничество было бы клеветой.
                self.issued_numbers.add(value)
                self._write_resource({**stem, "confirmed": False,
                                       "pending": True, "chosen": value,
                                       "reason": f"заявка принята: возьмёт {value} "
                                                  "на первой же своей проверке круга"})
                tags.append("заявка_судьбы")
                continue

            if resource == "resilience":
                total = self.combat.spend_resilience(name)
            elif resource == "fortune":
                # Игроки ходят раньше проверок, поэтому Удача копится заявкой
                # и срабатывает на первом же провале этого круга.
                self.claims_fortune[name] = "переброс"
                self._write_resource({**stem, "confirmed": False,
                                       "pending": True,
                                       "reason": "заявка принята, сработает на "
                                                  "первом проваленном броске круга"})
                tags.append("заявка_удачи")
                continue
            else:
                total = self.combat.spend(name, resource)

            if total["success"] and resource == "resolve":
                # Решимость снимает на круг ВСЁ: страх, боль, раны, штрафы.
                # Раньше она умела только убирать «сломлен» и «оглушён», и в
                # третьем прогоне Курт потратил её впустую — снимать было
                # нечего. Теперь трата не пропадает никогда.
                total["immunity"] = self.combat.give_immunity(name)
                self.deliver(
                    GM,
                    f"СЛУЖЕБНОЕ. {name} потратил Решимость: этот круг его не "
                    f"держит ничего — ни страх, ни боль, ни раны, ни штрафы. "
                    f"Что именно это оправдывает в сцене, решаешь ты: если "
                    f"счёл уместным — значит можно.",
                    "скрипт",
                )
            self._write_resource({**stem, "confirmed": total["success"],
                                   "reason": total["reason"],
                                   "before": total["before"], "after": total["after"],
                                   "immunity_for_round": total.get("immunity") is not None,
                                   "состояния_при_трате": total.get("immunity") or None})
            tags.append("ресурс_потрачен" if total["success"] else "заявка_отклонена")
            if not total["success"]:
                self.deliver(
                    GM,
                    f"СЛУЖЕБНОЕ. {name} объявил трату — "
                    f"{protocol.RESOURCE_NAMES.get(resource, resource)}, — но тратить "
                    f"нечего: {total['reason']}. Эффекта нет.",
                    "скрипт",
                )
        return tags

    def _header_round(self) -> str:
        """Мастер должен видеть, сколько осталось: без счётчика он не торопится."""
        limit = int(self.config["max_rounds"])
        header = f"Круг {self.round} из {limit}. Режим: {self.mode}."
        if self.down:
            down_names = ", ".join(
                f"{name} ({data['reason']}, с круга {data['round']})"
                for name, data in self.down.items()
            )
            header += f" Выбыли: {down_names}."
        return header

    def _summary_turns(self, turns: list[Turn], header: str) -> str:
        explicit = [t for t in turns if not t.secret and not t.refusal]
        # Про отказ мастеру говорим как про молчание, без причины: знать, что за
        # персонажем чужая модель и она отказалась, ему не положено.
        silent = [t.character for t in turns if t.refusal]
        lines = [header, "", "Ходы игроков:", ""]
        for turn in explicit:
            lines.append(f"{turn.character.upper()}: {turn.text}")
            lines.append("")
        if not explicit and not silent:
            lines.append("(все ходы этого круга были тайными)")
        for name in silent:
            lines.append(f"{name.upper()}: молчит и ничего не делает в этот круг.")
            lines.append("")
        return "\n".join(lines).strip()

    # --- сцена мастера ----------------------------------------------------

    async def _scene_gm(self, briefing: bool = False) -> None:
        hint = (
            "Вводная сцена. Восемь-двенадцать предложений."
            if briefing else "Твоя сцена."
        )
        chunks: list[str] = []
        annotations: dict[str, str] = {}
        annotations_attacks: dict[str, str] = {}
        limit = int(self.config.get("max_gm_replies_per_round", 8))
        # Сцена мастера может состоять из нескольких ответов: он останавливается
        # на проверке и продолжает после броска. Расход суммируем по всем.
        usage = {"cost": 0.0, "latency_ms": 0, "tokens": {}, "model": None,
                  "context": 0}

        for step in range(1, limit + 1):
            reply = await self.ask(GM, hint)
            hint = "Продолжай сцену."
            text = reply.text.strip()

            # Мастер тоже может отказаться вести сцену. Его отказ за стол не
            # уходит — иначе нравоучение попадёт в реплику и увидят все игроки.
            # Круг остаётся без сцены мастера, и это громко видно в логе.
            if "отказ" in reply.tags:
                self.refusals.append({
                    "round": self.round, "who": GM, "provider": reply.provider,
                    "model": reply.model_actual or reply.model,
                    "quote": " ".join(text.split())[:300],
                })
                self.logbook.anomaly(
                    round=self.round, who=GM, tag="отказ",
                    details=" ".join(text.split())[:300],
                )
                break

            chunks.append(text)

            usage["cost"] += reply.cost or 0.0
            usage["latency_ms"] += reply.latency_ms
            usage["context"] = max(usage["context"], self._context.get(GM, 0))
            usage["model"] = reply.model_actual or reply.model
            for key, value in (reply.tokens or {}).items():
                if isinstance(value, int):
                    usage["tokens"][key] = usage["tokens"].get(key, 0) + value

            notes: list[str] = []
            fragments = parse.self_rolls(text, self.issued_numbers)
            if fragments:
                real = self.dice.fake()
                self.issued_numbers.add(real)
                self.counters[GM]["self_rolls"] += 1
                self.logbook.anomaly(round=self.round, who=GM, tag="самоброс",
                                     details="; ".join(fragments))
                notes.append(
                    "Бросок недействителен, его не называют сами. "
                    f"Настоящий результат: {real}."
                )

            checks, anomalies = parse.checks(text, self.names)
            attack_lines, anomalies_combat = parse.attacks(text, self.names)
            for tag in [mk for mk in anomalies + anomalies_combat if mk != "проверка_за_нпс"]:
                self.logbook.anomaly(round=self.round, who=GM, tag=tag,
                                     details=text[:400])

            lines: list[str] = []
            for check in checks:
                roll = self.dice.roll(
                    check.character, check.skill, check.difficulty, check.base
                )
                self.logbook.roll(round=self.round, roll=roll.as_dict(),
                                   tags=check.tags + roll.tags)
                self.issued_numbers.update({roll.rolled, roll.target})
                if check.character in self.player_failures and not roll.success:
                    self.player_failures[check.character] += 1
                # Судьба сильнее Удачи: если заявлены обе, число берётся
                # выбранное, и перебрасывать уже нечего.
                roll, substituted = self._choice_for_fate(check, roll)
                if not substituted:
                    roll = self._reroll_for_fortune(check, roll)
                annotations[check.line] = roll.short()
                lines.append(roll.as_line())

            lines_combat = self._resolve_attacks(attack_lines, annotations_attacks)

            if notes:
                self.deliver(GM, "\n".join(notes), "скрипт")
            if lines:
                self.deliver(GM, "Результаты бросков:\n" + "\n".join(lines), "скрипт")
            if lines_combat:
                self.deliver(GM, "Результаты атак:\n" + "\n".join(lines_combat),
                               "скрипт")
            if not lines and not notes and not lines_combat:
                break
            if step == limit:
                self.logbook.anomaly(round=self.round, who=GM,
                                     tag="предел_ответов_мастера", details="")

        full = "\n\n".join(d for d in chunks if d)
        self.counters[GM]["turns"] += 1
        self.counters[GM]["chars"] += len(full)

        mode, had_tag = parse.mode(full, self.config.get("default_mode", protocol.DEFAULT_MODE))
        # На финальной сцене тег режима уже не нужен, аномалией это не считаем.
        not_tag = not had_tag and not parse.declared_finale(full)
        if not_tag:
            self.logbook.anomaly(round=self.round, who=GM, tag="тег_режима_пропущен",
                                 details="")
        self.mode = mode

        public, private = parse.split_private(full)
        public = parse.annotate_checks(parse.without_control_tags(public), annotations)
        public = parse.annotate_attacks(public, annotations_attacks)
        for_players = parse.without_lines_attacks(public)

        self.logbook.turn(
            round=self.round, mode=self.mode, speaker=GM, order_in_round=0,
            visibility="всем", text=public,
            tags=(["тег_режима_пропущен"] if not_tag else []) + (["вводная"] if briefing else []),
            provider=self.config["gm"]["provider"],
            model=usage["model"] or self.config["gm"]["model"],
            latency_ms=usage["latency_ms"] or None,
            tokens=usage["tokens"] or None,
            cost=round(usage["cost"], 6) or None,
            replies_in_scene=len(chunks),
            context_chars=usage["context"] or None,
        )
        for name in self.names:
            self.deliver(name, f"МАСТЕР:\n{for_players}", GM)

        for to, chunk in private.items():
            chunk = parse.without_control_tags(chunk)
            target = self._identify(to)
            if target is None:
                self.logbook.anomaly(round=self.round, who=GM, tag="личный_блок_без_адресата",
                                     details=to)
                continue
            self.logbook.turn(
                round=self.round, mode=self.mode, speaker=GM, order_in_round=0,
                visibility=f"только {target}", text=chunk, tags=["личный_блок"],
            )
            self.deliver(target, f"ТОЛЬКО ДЛЯ ТЕБЯ:\n{chunk}", GM)

        self._parse_resolve(full)
        self._parse_down(full)
        # Заявки на Удачу живут один круг: не сработала — сгорела вместе с ним.
        self.claims_fortune.clear()
        self.claims_fate.clear()

        if parse.declared_finale(full):
            self.stop = "финал"
            self.logbook.write("финал", round=self.round, speaker=GM)
        elif not self.active:
            self.stop = "все игроки выбыли"
            self.logbook.write("остановка", round=self.round,
                                 text="все игроки выбыли")

    def _choice_for_fate(self, check, roll):
        """Судьба в безнадёжном положении: игрок берёт число сам.

        Возвращает бросок и признак того, что подмена случилась. Настоящий
        бросок уже записан в лог выше — сравнение «выпало бы столько, взял
        столько» показывает, чего стоила трата, и пойдёт в разбор.
        """
        name = check.character
        value = self.claims_fate.get(name)
        if value is None or not self.combat or name not in self.combat.combatants:
            return roll, False

        self.claims_fate.pop(name, None)
        total = self.combat.spend_fate(name)
        if not total["success"]:
            self._write_resource({
                "round": self.round, "who": name, "resource": "fate", "variant": "трата",
                "claimed": True, "confirmed": False, "reason": total["reason"],
                "before": total["before"], "after": total["after"],
            })
            return roll, False

        fresh = self.dice.substitute(roll, value, "выбрано_за_судьбу")
        self.logbook.roll(round=self.round, roll=fresh.as_dict(),
                           tags=check.tags + fresh.tags)
        self.issued_numbers.update({fresh.rolled, fresh.target})
        self._write_resource({
            "round": self.round, "who": name, "resource": "fate", "variant": "трата",
            "on_what": "выбор значения", "claimed": True, "confirmed": True,
            "reason": "", "before": total["before"], "after": total["after"],
            "roll_before": {"rolled": roll.rolled, "target": roll.target,
                          "success": roll.success},
            "roll_after": {"rolled": fresh.rolled, "target": fresh.target,
                             "success": fresh.success},
            "helped": bool(fresh.success and not roll.success),
        })
        self.deliver(
            GM,
            f"СЛУЖЕБНОЕ. {name} потратил Судьбу и взял {fresh.rolled} вместо "
            f"выпавшего {roll.rolled}. Судьбы осталось "
            f"{total['after']['fate']}, потолок Удачи теперь "
            f"{total['after']['fortune_max']}. Опиши, ЧЕМ это далось: Судьба "
            f"тратится навсегда и должна оставить след — не «повезло», "
            f"а какой ценой.",
            "скрипт",
        )
        return fresh, True

    def _reroll_for_fortune(self, check, roll):
        """Заявленная Удача срабатывает на первом же проваленном броске круга.

        Оба результата остаются в логе: сравнение «было — стало» показывает,
        что трата действительно что-то изменила, и пойдёт в ролик.
        """
        name = check.character
        if roll.success or self.claims_fortune.get(name) != "переброс":
            return roll
        if not self.combat or name not in self.combat.combatants:
            return roll

        total = self.combat.spend(name, "fortune")
        self.claims_fortune.pop(name, None)
        if not total["success"]:
            self._write_resource({
                "round": self.round, "who": name, "resource": "fortune",
                "claimed": True, "confirmed": False,
                "reason": total["reason"], "before": total["before"],
                "after": total["after"],
            })
            return roll

        fresh = self.dice.roll(check.character, check.skill,
                                    check.difficulty, check.base)
        self.logbook.roll(round=self.round, roll=fresh.as_dict(),
                           tags=check.tags + fresh.tags + ["переброс_за_удачу"])
        self.issued_numbers.update({fresh.rolled, fresh.target})
        self._write_resource({
            "round": self.round, "who": name, "resource": "fortune", "on_what": "переброс",
            "claimed": True, "confirmed": True, "reason": "",
            "before": total["before"], "after": total["after"],
            "roll_before": {"rolled": roll.rolled, "target": roll.target,
                          "success": roll.success},
            "roll_after": {"rolled": fresh.rolled, "target": fresh.target,
                             "success": fresh.success},
            "helped": bool(fresh.success),
        })
        self.deliver(
            GM,
            f"СЛУЖЕБНОЕ. {name} потратил Удачу на переброс: было {roll.rolled} "
            f"из {roll.target} (провал), стало {fresh.rolled} из {fresh.target} "
            f"({'успех' if fresh.success else 'снова провал'}). Опиши это как "
            f"случайность или всплеск сил, не называя механики.",
            "скрипт",
        )
        return fresh

    def _parse_resolve(self, text: str) -> None:
        """Мастер начисляет Решимость за игру по Мотивации. Только он."""
        if not self.combat:
            return
        for entry in parse.awards_resolve(text, self.names):
            name = entry["name"]
            if name not in self.names or name not in self.combat.combatants:
                self.logbook.anomaly(round=self.round, who=GM,
                                     tag="решимость_неизвестному",
                                     details=entry["line"])
                continue
            total = self.combat.award_resolve(name, entry["amount"])
            self._write_resource({
                "round": self.round, "who": name, "resource": "resolve",
                "variant": "начисление", "awarded": total["added"],
                "claimed": True, "confirmed": total["success"],
                "reason": entry["reason"], "refusal": total["reason"],
                "before": total["before"], "after": total["after"],
            })
            if total["success"]:
                self.deliver(
                    name,
                    f"СЛУЖЕБНОЕ. Тебе начислена Решимость (+{total['added']}) "
                    f"за игру по своей цели: {entry['reason']}. "
                    f"Теперь у тебя Решимости {total['after']['resolve']} из "
                    f"{total['after']['resolve_max']}.",
                    "скрипт",
                )

    def _parse_down(self, text: str) -> None:
        """Теги [ВЫБЫЛ] и [ВЕРНУЛСЯ]. Выбывшего больше не спрашивают."""
        for name, reason in parse.down_events(text, self.names):
            if name not in self.names:
                self.logbook.anomaly(round=self.round, who=GM,
                                     tag="выбытие_неизвестного", details=name)
                continue
            if name in self.down:
                continue
            self.down[name] = {"reason": reason, "round": self.round}
            self.queue[name] = []   # недоставленное выбывшему пропадает
            self.logbook.write(
                "выбытие", round=self.round, speaker=name, text=reason,
                rounds_played=self.played.get(name, 0),
            )

        for name in parse.comebacks(text, self.names):
            if name in self.down:
                data = self.down.pop(name)
                self.logbook.write(
                    "возвращение", round=self.round, speaker=name,
                    text=f"отсутствовал с круга {data['round']} ({data['reason']})",
                )

    # --- бой ---------------------------------------------------------------

    def _resolve_attacks(self, attack_lines, annotations: dict) -> list[str]:
        """Считает скрипт: встречная проверка, локация, броня, раны, криты."""
        if not self.combat or not attack_lines:
            return []

        lines: list[str] = []
        for attack in attack_lines:
            total = self.combat.attack(attack.who, attack.target_name, attack.weapon, attack.defence)
            self.logbook.write(
                "атака", round=self.round, speaker=attack.who, visibility="всем",
                attack=total.mapping(), tags=total.tags + attack.tags,
            )
            self.issued_numbers.update(
                {total.roll_attacks, total.roll_defence, total.target_attacks,
                 total.target_defence, total.wounds_dealt, total.wounds_left}
            )
            annotations[attack.line] = total.as_line()
            lines.append(total.as_line())

            if total.doubles:
                when = {
                    "doubles_crit": "критическое ранение, опиши как это выглядит",
                    "fumble": "фумбл, опиши осложнение",
                    "doubles_without_consequences":
                        "дубль, но атака не прошла — последствий нет",
                }
                mark = next((tx for mk, tx in when.items() if mk in total.tags), "")
                if mark:
                    lines.append(f"  дубль {total.roll_attacks}: {mark}")
            if total.awaiting_fate:
                self.fate_queue.append(total.target)
            elif total.death:
                lines.append(self.combat.apply_fate(total.target, False))
        return lines

    async def _ask_fate(self) -> list[str]:
        """Судьбу тратит игрок и только своим словом. Ни скрипт, ни мастер."""
        replies: list[str] = []
        while self.fate_queue:
            name = self.fate_queue.pop(0)
            if name not in self.names:
                replies.append(self.combat.apply_fate(name, False))
                continue

            sheet = self.combat.sheet(name)
            self.deliver(
                name,
                "СЛУЖЕБНОЕ. Твой персонаж получил смертельное ранение. Судьба "
                f"заменит смерть на беспамятство, у тебя её осталось {sheet.fate}. "
                "Тратишь? Ответь первым словом ДА или НЕТ, дальше можешь коротко "
                "отыграть.",
                "скрипт",
            )
            try:
                reply = await self.ask(name, "Тратишь Судьбу?")
                spending = bool(re.match(r"\s*\W*да\b", reply.text, re.IGNORECASE))
                text_reply = reply.text.strip()
            except RunStopped:
                # Агент не ответил — Судьбу за него не тратим, это его решение.
                spending, text_reply = False, "(агент не ответил)"

            total = self.combat.apply_fate(name, spending)
            self.logbook.write(
                "судьба", round=self.round, speaker=name, text=text_reply,
                spent=spending, left=self.combat.sheet(name).fate,
            )
            replies.append(total)
            if not spending:
                self.down.setdefault(name, {"reason": "погиб", "round": self.round})
        return replies

    async def _bleeding(self) -> list[str]:
        """На нуле ран истекающий кровью проверяется каждый раунд. Сам, без мастера."""
        lines: list[str] = []
        for name in self.combat.bleeding():
            total = self.combat.check_bleeding(name)
            self.logbook.write(
                "кровотечение", round=self.round, speaker=name,
                roll=total, tags=["без_сознания"] if not total["success"] else [],
            )
            self.issued_numbers.update({total["rolled"], total["target"]})
            lines.append(
                f"{name}: кровотечение, Выносливость — выпало {total['rolled']} "
                f"из {total['target']}, {total['consequence']}"
            )
        return lines

    def _identify(self, raw: str) -> str | None:
        key = raw.strip().lower()
        for name in self.names:
            if key.startswith(name.lower()[:4]):
                return name
        return None

    # --- итоги ------------------------------------------------------------

    def _collate_providers(self) -> dict:
        """Сводит счёт сессии со счётчиками самих адаптеров.

        Сессия видит токены и деньги, адаптер — повторы, паузы и обрывы по
        пределу. В отчёт нужно и то и другое, поэтому склеиваем здесь.
        """
        summary = {name: dict(data) for name, data in self.provider_totals.items()}
        for name, provider in self.providers.items():
            usage = getattr(provider, "usage", None)
            row = summary.setdefault(name, {})
            if usage is not None:
                row["retries"] = usage.retries
                row["truncations_by_limit"] = usage.truncations
                row["seconds_in_requests"] = round(usage.seconds, 1)
            observed = getattr(provider, "observed_discrepancies", None)
            if observed:
                row["discrepancies_resolved_on_the_fly"] = list(observed)
            # Доля попаданий в кэш: влияет и на цену, и на скорость, и на статью.
            total_of_input = row.get("input", 0) + row.get("cache_read", 0)
            if total_of_input:
                row["share_cache"] = round(row.get("cache_read", 0) / total_of_input, 4)
            if row.get("output"):
                row["share_reasoning"] = round(
                    row.get("reasoning", 0) / row["output"], 4
                )
            if "деньги_usd" in row:
                row["cost_usd"] = round(row["cost_usd"], 6)
            # У Claude денег по подписке не списывается: SDK отдаёт пересчёт.
            if name == "claude":
                row["caveat"] = ("подписка: сумма расчётная, счёта за неё нет")
        return summary

    def _collate_resources(self) -> dict:
        """Что заявлено, что подтверждено и с чем закончили.

        Судья читает транскрипт и не видит состояния скрипта, поэтому разницу
        между «сказал, что тратит» и «потратил» ему надо подать числом. Без
        этого он снова засчитает за трату слова — как в прошлых прогонах.
        """
        summary: dict[str, dict] = {}
        for name in self.names:
            row = {"claimed": {}, "confirmed": {}, "resolve_awarded": 0,
                      "failures_could_before_reroll": 0}
            if self.combat and name in self.combat.combatants:
                row["on_finish"] = self.combat.resources(name)
                row["motivation"] = self.combat.sheet(name).motivation
            summary[name] = row

        for event in self.resources_events:
            row = summary.get(event["who"])
            if row is None:
                continue
            resource = event["resource"]
            # Начисление опознаём по виду события, а не по величине: неудавшееся
            # начисление приходит с нулём, и по «if начислено» оно проваливалось
            # в ветку трат — мастерская щедрость засчитывалась игроку за попытку
            # что-то потратить.
            if event.get("variant") == "начисление":
                row["resolve_awarded"] += event.get("awarded") or 0
                continue
            row["claimed"][resource] = row["claimed"].get(resource, 0) + 1
            if event.get("confirmed"):
                row["confirmed"][resource] = row["confirmed"].get(resource, 0) + 1

        # Сколько раз у игрока был провал, который можно было перебросить.
        # Без этого числа упрёк «доиграл с полными руками» несправедлив: может,
        # и случая не было.
        for name, amount in self.player_failures.items():
            if name in summary:
                summary[name]["failures_could_before_reroll"] = amount
        return summary

    def _totals(self) -> None:
        mean = {
            name: round(data["chars"] / data["turns"], 1) if data["turns"] else 0
            for name, data in self.counters.items()
        }
        self.logbook.write(
            "итог",
            rounds=self.round,
            stop=self.stop,
            minutes=round(self.logbook.elapsed_seconds / 60, 1),
            counters=self.counters,
            rounds_played=self.played,
            down={name: data for name, data in self.down.items()} or None,
            mean_length_lines=mean,
            tokens=self.token_totals,
            provider_totals=self._collate_providers() or None,
            refusal_count=len(self.refusals) or None,
            refusals=self.refusals or None,
            actual_models=self.actual_models,
            cost_usd=round(self.cost, 4),
            resources=self._collate_resources() or None,
            corruption_language=self.corruption_language or None,
            rolls=self.dice.rolls,
            seed=self.dice.seed,
            rigged_rolls=self.dice.rigged or None,
            combat=self.combat.totals() if self.combat else None,
        )
