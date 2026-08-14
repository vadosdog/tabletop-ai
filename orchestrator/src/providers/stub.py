"""Заглушки вместо моделей: сухой прогон без единого токена.

Сценарий подобран так, чтобы задеть все ветки маршрутизации: смена режима,
пропущенный тег, проверка с остановкой мастера, тайный ход, блок «ТОЛЬКО ДЛЯ»,
самоброски у мастера и у игрока, финал.
"""

from __future__ import annotations

import json
import re

from .base import Reply, register


class StubSession:
    def __init__(self, agent: str, role: str, model: str):
        self.agent = agent
        self.role = role
        self.model = model
        self.calls = 0
        self.scenes = 0

    async def send(self, text: str) -> Reply:
        self.calls += 1
        if self.role == "судья":
            reply = self._judge(text)
        elif self.role == "мастер":
            reply = self._gm(text)
        else:
            reply = self._player(text)
        return Reply(
            text=reply, provider="stub", model=self.model,
            latency_ms=0, tokens={"input": len(text), "output": len(reply)},
            cost=0.0,
        )

    async def close(self) -> None:
        return None

    # --- мастер -----------------------------------------------------------
    # Расписание режимов по сценам: первые шесть разговорных, чтобы сухой прогон
    # задел ротацию стартового игрока, дальше действие.
    SCHEDULE = ["РАЗГОВОР"] * 6 + ["ДЕЙСТВИЕ"] * 9

    def _gm(self, incoming: str) -> str:
        resume = any(s in incoming for s in
                          ("Результаты бросков", "Результаты атак", "недействителен"))
        if not resume:
            self.scenes += 1
        scene = self.scenes
        mode = self.SCHEDULE[min(scene, len(self.SCHEDULE)) - 1]
        tag = f"\n\n[РЕЖИМ: {mode}]"

        if resume:
            if "Результаты атак" in incoming:
                return "Тварь отшатывается в темноту, но не уходит." + tag
            return (
                "Грязь на подоконнике чёрная и жирная, пахнет стоячей водой. "
                "Ханна вытирает пальцы о штаны и молчит." + tag
            )

        if scene == 1:
            return (
                "Утро пришло тихое, дождь кончился, и дом стоит как нежилой. "
                "Отто Гримм лежит на полу своей комнаты, дверь была заперта изнутри. "
                "Сундук на месте, печать цела, внутри свинец в тряпье. "
                "К полудню на мост придёт патруль." + tag
            )
        if scene == 2:  # проверка: мастер обязан остановиться и ждать бросок
            return "ПРОВЕРКА: Ханна, Восприятие, испытание"
        if scene == 3:
            return "Гретель ставит кружку на стол и смотрит мимо." + tag
        if scene == 4:  # личный блок
            return (
                "Йорг качается на лавке и считает пальцы.\n\n"
                "ТОЛЬКО ДЛЯ Лизель\n"
                "Ты замечаешь, что сержант на мосту сверяет бумаги по списку." + tag
            )
        if scene == 5:  # самоброс мастера
            return (
                "Где-то под полом капает вода. Бросок вышел 77, и Гретель "
                "отводит глаза."
            )
        if scene == 6:
            return "Лестница в погреб пахнет стоячей водой." + tag
        if scene == 7:  # тега режима нет вообще — проверка умолчания
            return "Никто не хочет идти первым."
        if scene == 8:  # две проверки в одной сцене
            return "ПРОВЕРКА: Курт, Сила воли, трудно\nПРОВЕРКА: Лизель, Ловкость, легко"
        if scene == 9:  # выбытие
            return (
                "Тварь достаёт Ханну когтем поперёк груди, и она оседает в воду "
                "лицом вниз.\n[ВЫБЫЛ: Ханна, без сознания]" + tag
            )
        if scene == 10:  # третий ДЕЙСТВИЕ подряд — должен сработать сторожок
            return "Вода прибывает, свет фонаря дрожит." + tag
        if scene == 11:  # возвращение
            return (
                "Ханна кашляет и переворачивается на бок — жива.\n"
                "[ВЕРНУЛСЯ: Ханна]" + tag
            )
        if scene == 12:  # бой: атака по бронированному и по голому
            return (
                "Тварь выходит из воды.\n"
                "АТАКА: Тварь → Курт, когти\n"
                "АТАКА: Ансельм → Тварь, молот" + tag
            )
        if scene == 13:  # ответная свалка
            return (
                "Свалка у лаза.\n"
                "АТАКА: Курт → Тварь, меч\n"
                "АТАКА: Тварь → Лизель, когти" + tag
            )
        if scene == 14:  # добиваем: кто-то дойдёт до нуля и получит крит
            return (
                "Тварь бьёт наотмашь.\n"
                "АТАКА: Тварь → Лизель, когти\n"
                "АТАКА: Тварь → Лизель, когти" + tag
            )
        if scene == 15:
            return (
                "Ещё удар.\n"
                "АТАКА: Тварь → Лизель, когти\n"
                "АТАКА: Тварь → Ханна, когти" + tag
            )
        return "Патруль уходит по мосту, и дорога остаётся пустой.\n\n[ФИНАЛ]"

    # --- игроки -----------------------------------------------------------
    def _player(self, incoming: str) -> str:
        request = ""
        foreign = re.findall(r"ХОД ИГРОКА — ([А-ЯЁа-яё]+)", incoming)
        if foreign:
            request = f'"{foreign[-1]}, помолчи," — говорит {self.agent}. '

        if self.agent == "Лизель" and self.calls == 3:
            return f"ТАЙНО {self.agent} проверяет, на месте ли грамота, и отходит к двери."
        # Траты ресурсов: сухой прогон должен задевать и этот путь целиком —
        # заявку, списание, переброс и попытку потратить пустое.
        if self.agent == "Ханна" and self.calls == 2:
            return "ТРАЧУ УДАЧУ. Ханна вцепляется в кладку и лезет вниз."
        if self.agent == "Ансельм" and self.calls == 3:
            return "ТРАЧУ РЕШИМОСТЬ. Ансельм разжимает зубы и поднимает голову."
        if self.agent == "Ансельм" and self.calls == 5:
            # Решимость уже потрачена — вторая заявка должна быть отклонена.
            return "ТРАЧУ РЕШИМОСТЬ. Ансельм пробует снова, но сил не осталось."
        if self.agent == "Курт" and self.calls == 4:
            return f"{self.agent} кидает на силу воли, выпало 42, и держится."
        if "Тратишь Судьбу?" in incoming:
            # Заглушка отвечает согласием: проверяем ветку с отменой смерти.
            return "ДА. Лизель цепляется за жизнь и уходит в темноту."
        return (
            f"{request}{self.agent} смотрит в кружку и говорит коротко, "
            f"ход номер {self.calls}."
        )

    # --- судья ------------------------------------------------------------
    CRITERIA = ["сеттинг", "правила", "цель", "тайна", "драма"]

    def _judge(self, incoming: str) -> str:
        """Разбирает поданный транскрипт и выдаёт JSON с настоящими цитатами.

        Баллы берутся из хеша, а не из смысла: заглушка проверяет конвейер
        скоринга, а не качество игры.
        """
        lines: dict[str, list[tuple[int, str]]] = {}
        round = 0
        for line in incoming.splitlines():
            header = re.match(r"##\s*Круг\s+(\d+)", line)
            if header:
                round = int(header.group(1))
                continue
            reply_match = re.match(r"([А-ЯЁ]+)(?:\s*\[[^\]]*\])?:\s*(.+)", line)
            if reply_match and reply_match.group(1) != "МАСТЕР":
                name = reply_match.group(1).capitalize()
                lines.setdefault(name, []).append((round, reply_match.group(2).strip()))

        characters = {}
        # Номер прохода задаёт разброс: без него медиана и разброс не проверяются.
        index = int((re.search(r"(\d+)$", self.agent) or re.match("1", "1")).group(1))
        offset = (index - 1) % 3 - 1
        for name, found in lines.items():
            criteria = {}
            for n, criterion in enumerate(self.CRITERIA):
                round_c, quote = found[min(n, len(found) - 1)]
                score = 1 + (sum(map(ord, name)) + n) % 5
                score = max(1, min(5, score + (offset if n == 0 else 0)))
                criteria[criterion] = {
                    "score": score, "round": round_c,
                    "quote": " ".join(quote.split()[:8]),
                    "пояснение": "заглушка",
                }
            characters[name] = {"criteria": criteria, "штрафы": []}
        return json.dumps({"characters": characters}, ensure_ascii=False)


class StubProvider:
    name = "stub"

    def __init__(self, **_):
        self.sessions: list[StubSession] = []

    async def open(self, agent: str, system_prompt: str, model: str) -> StubSession:
        if agent.startswith("Судья"):
            role = "судья"
        else:
            role = "мастер" if agent == "Мастер" else "игрок"
        session = StubSession(agent, role, model)
        self.sessions.append(session)
        return session

    async def shutdown(self) -> None:
        for session in self.sessions:
            await session.close()


register("stub", StubProvider)
register("заглушка", StubProvider)
