"""Claude через Agent SDK. Работает на подписке, API-ключ не нужен.

SDK тащит с собой бинарь Claude Code, а тот идёт по цепочке приоритета учёток:
подписочные OAuth-креды из `claude /login` стоят в ней последними и подхватываются,
если в окружении нет ANTHROPIC_API_KEY. Для запуска без браузера — `claude
setup-token` и CLAUDE_CODE_OAUTH_TOKEN.

Агент здесь не кодовый: инструментов нет, песочница пустая. Игрок не должен
дотянуться до файлов проекта — там лежит модуль со скрытой правдой и чужие тайны.
"""

from __future__ import annotations

import asyncio
import dataclasses
import shutil
import tempfile
import time

from claude_agent_sdk import (  # type: ignore
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
)

from .base import Ответ, зарегистрировать

# Инструменты не нужны вообще: агент только пишет реплики.
ЗАПРЕЩЁННЫЕ = [
    "Bash", "Read", "Write", "Edit", "NotebookEdit", "Glob", "Grep",
    "WebSearch", "WebFetch", "Task", "TodoWrite",
]


def _собрать_опции(**параметры) -> tuple[ClaudeAgentOptions, list[str]]:
    """Отбрасывает поля, которых нет в установленной версии SDK.

    Версии SDK расходятся по составу полей; падать из-за незнакомого ключа
    посреди прогона нельзя.
    """
    известные = {поле.name for поле in dataclasses.fields(ClaudeAgentOptions)}
    заданные = {к: з for к, з in параметры.items() if з is not None}
    отброшено = sorted(set(заданные) - известные)
    годные = {к: з for к, з in заданные.items() if к in известные}
    return ClaudeAgentOptions(**годные), отброшено


class СессияClaude:
    def __init__(self, агент: str, клиент: ClaudeSDKClient, модель: str, песочница: str):
        self.агент = агент
        self.модель = модель
        self._клиент = клиент
        self._песочница = песочница

    async def отправить(self, текст: str) -> Ответ:
        начало = time.monotonic()
        куски: list[str] = []
        модель_факт: str | None = None
        токены: dict | None = None
        стоимость: float | None = None
        метки: list[str] = []

        await self._клиент.query(текст)
        async for сообщение in self._клиент.receive_response():
            if isinstance(сообщение, AssistantMessage):
                модель_факт = getattr(сообщение, "model", None) or модель_факт
                for блок in сообщение.content:
                    кусок = getattr(блок, "text", None)
                    if кусок:
                        куски.append(кусок)
            elif isinstance(сообщение, ResultMessage):
                сырые = getattr(сообщение, "usage", None)
                if сырые:
                    токены = сырые if isinstance(сырые, dict) else dict(сырые)
                стоимость = getattr(сообщение, "total_cost_usd", None)
                if getattr(сообщение, "is_error", False):
                    метки.append("ошибка_агента")

        ответ = "\n\n".join(к.strip() for к in куски if к.strip()).strip()
        if not ответ:
            метки.append("пустой_ответ")

        return Ответ(
            текст=ответ,
            провайдер="claude",
            модель=self.модель,
            латентность_мс=int((time.monotonic() - начало) * 1000),
            токены=токены,
            стоимость=стоимость,
            модель_факт=модель_факт,
            метки=метки,
        )

    async def закрыть(self) -> None:
        try:
            await self._клиент.disconnect()
        finally:
            shutil.rmtree(self._песочница, ignore_errors=True)


class ПровайдерClaude:
    имя = "claude"

    # Каждая сессия — свой подпроцесс Claude Code. Их пятеро, и поднимаются они
    # не мгновенно: на умолчании SDK второй агент не успевал доложиться и валил
    # прогон с «Control request timeout: initialize» ещё до первого хода.
    ЗАПУСК_МС = 120_000
    ПОПЫТОК_ПОДКЛЮЧЕНИЯ = 4

    def __init__(self, предел_стоимости: float | None = None, параметры=None,
                 игроки: set[str] | None = None, **_):
        self.сессии: list[СессияClaude] = []
        self.отброшенные_опции: list[str] = []
        self.предел_стоимости = предел_стоимости
        self.параметры = параметры
        # Кому выравнивать размышление. Только игрокам: они сравниваются между
        # собой. Мастер и судья вне сравнения, и урезать им размышление до общей
        # нижней ступени значило бы испортить сессию ради несуществующего
        # паритета — ровно то, чего мы добивались, переводя мастера на Opus.
        self.игроки = set(игроки or ())

    def что_задано(self, агент: str = "проба") -> dict:
        """Из четырёх параметров генерации подписочный SDK умеет ровно один.

        Отдаём только его. Написать здесь «температура 1.0» было бы враньём:
        поля temperature в ClaudeAgentOptions нет, и значение вендорское.
        """
        уровень = self._размышление(агент)
        return {"effort": уровень} if уровень else {}

    def _размышление(self, агент: str) -> str | None:
        if агент not in self.игроки or self.параметры is None:
            return None
        return self.параметры.уровень(self.имя)

    async def открыть(self, агент: str, системный_промпт: str, модель: str) -> СессияClaude:
        песочница = tempfile.mkdtemp(prefix=f"нри-{агент}-")

        опции, отброшено = _собрать_опции(
            model=модель,
            system_prompt=системный_промпт,   # строкой, не пресетом claude_code
            tools=[],                         # без схем инструментов в промпте
            allowed_tools=[],
            disallowed_tools=ЗАПРЕЩЁННЫЕ,
            permission_mode="dontAsk",
            max_turns=1,
            cwd=песочница,
            setting_sources=[],               # ни CLAUDE.md, ни настроек проекта
            max_budget_usd=self.предел_стоимости,
            load_timeout_ms=self.ЗАПУСК_МС,
            # Единственный из четырёх параметров генерации, который подписочный
            # SDK вообще умеет. Температура, top_p и предел ответа у него не
            # задаются — это в известных расхождениях.
            effort=self._размышление(агент),
        )
        if отброшено and not self.отброшенные_опции:
            self.отброшенные_опции = отброшено
            print(f"! SDK не знает опций: {', '.join(отброшено)} — идём без них")

        # Подъём подпроцесса иногда обрывается на рукопожатии («Control request
        # timeout: initialize»), причём без всякой закономерности. Один такой
        # обрыв на пятерых агентах валил прогон до первого хода — тридцать
        # кругов за один заход этого не переживут. Пробуем снова.
        последняя: Exception | None = None
        for попытка in range(1, self.ПОПЫТОК_ПОДКЛЮЧЕНИЯ + 1):
            клиент = ClaudeSDKClient(options=опции)
            try:
                await клиент.connect()
            except Exception as ошибка:
                последняя = ошибка
                try:
                    await клиент.disconnect()   # не оставлять висеть подпроцесс
                except Exception:
                    pass
                if попытка < self.ПОПЫТОК_ПОДКЛЮЧЕНИЯ:
                    пауза = 3.0 * попытка
                    print(f"! {агент}: подключение сорвалось "
                          f"({type(ошибка).__name__}), попытка {попытка} из "
                          f"{self.ПОПЫТОК_ПОДКЛЮЧЕНИЯ}, ждём {пауза:.0f} с")
                    await asyncio.sleep(пауза)
                continue

            сессия = СессияClaude(агент, клиент, модель, песочница)
            self.сессии.append(сессия)
            return сессия

        shutil.rmtree(песочница, ignore_errors=True)
        raise RuntimeError(
            f"агент {агент} не подключился за {self.ПОПЫТОК_ПОДКЛЮЧЕНИЯ} попыток: "
            f"{последняя!r}"
        )

    async def завершить(self) -> None:
        for сессия in self.сессии:
            try:
                await сессия.закрыть()
            except Exception as ошибка:  # закрытие не должно ронять прогон
                print(f"! не закрылась сессия {сессия.агент}: {ошибка}")


зарегистрировать("claude", ПровайдерClaude)
