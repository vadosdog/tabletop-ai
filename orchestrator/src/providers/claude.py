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

from .base import Reply, register

# Инструменты не нужны вообще: агент только пишет реплики.
FORBIDDEN = [
    "Bash", "Read", "Write", "Edit", "NotebookEdit", "Glob", "Grep",
    "WebSearch", "WebFetch", "Task", "TodoWrite",
]


def _build_options(**params) -> tuple[ClaudeAgentOptions, list[str]]:
    """Отбрасывает поля, которых нет в установленной версии SDK.

    Версии SDK расходятся по составу полей; падать из-за незнакомого ключа
    посреди прогона нельзя.
    """
    known = {field.name for field in dataclasses.fields(ClaudeAgentOptions)}
    given = {d: zn for d, zn in params.items() if zn is not None}
    dropped = sorted(set(given) - known)
    usable = {d: zn for d, zn in given.items() if d in known}
    return ClaudeAgentOptions(**usable), dropped


class ClaudeSession:
    def __init__(self, agent: str, client: ClaudeSDKClient, model: str, sandbox: str):
        self.agent = agent
        self.model = model
        self._client = client
        self._sandbox = sandbox

    async def send(self, text: str) -> Reply:
        start = time.monotonic()
        chunks: list[str] = []
        model_actual: str | None = None
        tokens: dict | None = None
        cost: float | None = None
        tags: list[str] = []

        await self._client.query(text)
        async for message in self._client.receive_response():
            if isinstance(message, AssistantMessage):
                model_actual = getattr(message, "model", None) or model_actual
                for block in message.content:
                    chunk = getattr(block, "text", None)
                    if chunk:
                        chunks.append(chunk)
            elif isinstance(message, ResultMessage):
                raw = getattr(message, "usage", None)
                if raw:
                    tokens = raw if isinstance(raw, dict) else dict(raw)
                cost = getattr(message, "total_cost_usd", None)
                if getattr(message, "is_error", False):
                    tags.append("ошибка_агента")

        reply = "\n\n".join(d.strip() for d in chunks if d.strip()).strip()
        if not reply:
            tags.append("пустой_ответ")

        return Reply(
            text=reply,
            provider="claude",
            model=self.model,
            latency_ms=int((time.monotonic() - start) * 1000),
            tokens=tokens,
            cost=cost,
            model_actual=model_actual,
            tags=tags,
        )

    async def close(self) -> None:
        try:
            await self._client.disconnect()
        finally:
            shutil.rmtree(self._sandbox, ignore_errors=True)


class ClaudeProvider:
    name = "claude"

    # Каждая сессия — свой подпроцесс Claude Code. Их пятеро, и поднимаются они
    # не мгновенно: на умолчании SDK второй агент не успевал доложиться и валил
    # прогон с «Control request timeout: initialize» ещё до первого хода.
    STARTUP_MS = 120_000
    ATTEMPTS_CONNECT = 4

    def __init__(self, limit_cost: float | None = None, params=None,
                 players: set[str] | None = None, **_):
        self.sessions: list[ClaudeSession] = []
        self.dropped_options: list[str] = []
        self.limit_cost = limit_cost
        self.params = params
        # Кому выравнивать размышление. Только игрокам: они сравниваются между
        # собой. Мастер и судья вне сравнения, и урезать им размышление до общей
        # нижней ступени значило бы испортить сессию ради несуществующего
        # паритета — ровно то, чего мы добивались, переводя мастера на Opus.
        self.players = set(players or ())

    def what_given(self, agent: str = "проба") -> dict:
        """Из четырёх параметров генерации подписочный SDK умеет ровно один.

        Отдаём только его. Написать здесь «температура 1.0» было бы враньём:
        поля temperature в ClaudeAgentOptions нет, и значение вендорское.
        """
        level = self._reasoning(agent)
        return {"effort": level} if level else {}

    def _reasoning(self, agent: str) -> str | None:
        if agent not in self.players or self.params is None:
            return None
        return self.params.level(self.name)

    async def open(self, agent: str, system_prompt: str, model: str) -> ClaudeSession:
        sandbox = tempfile.mkdtemp(prefix=f"нри-{agent}-")

        options, dropped = _build_options(
            model=model,
            system_prompt=system_prompt,   # строкой, не пресетом claude_code
            tools=[],                         # без схем инструментов в промпте
            allowed_tools=[],
            disallowed_tools=FORBIDDEN,
            permission_mode="dontAsk",
            max_turns=1,
            cwd=sandbox,
            setting_sources=[],               # ни CLAUDE.md, ни настроек проекта
            max_budget_usd=self.limit_cost,
            load_timeout_ms=self.STARTUP_MS,
            # Единственный из четырёх параметров генерации, который подписочный
            # SDK вообще умеет. Температура, top_p и предел ответа у него не
            # задаются — это в известных расхождениях.
            effort=self._reasoning(agent),
        )
        if dropped and not self.dropped_options:
            self.dropped_options = dropped
            print(f"! SDK не знает опций: {', '.join(dropped)} — идём без них")

        # Подъём подпроцесса иногда обрывается на рукопожатии («Control request
        # timeout: initialize»), причём без всякой закономерности. Один такой
        # обрыв на пятерых агентах валил прогон до первого хода — тридцать
        # кругов за один заход этого не переживут. Пробуем снова.
        last: Exception | None = None
        for attempt in range(1, self.ATTEMPTS_CONNECT + 1):
            client = ClaudeSDKClient(options=options)
            try:
                await client.connect()
            except Exception as error:
                last = error
                try:
                    await client.disconnect()   # не оставлять висеть подпроцесс
                except Exception:
                    pass
                if attempt < self.ATTEMPTS_CONNECT:
                    pause = 3.0 * attempt
                    print(f"! {agent}: подключение сорвалось "
                          f"({type(error).__name__}), попытка {attempt} из "
                          f"{self.ATTEMPTS_CONNECT}, ждём {pause:.0f} с")
                    await asyncio.sleep(pause)
                continue

            session = ClaudeSession(agent, client, model, sandbox)
            self.sessions.append(session)
            return session

        shutil.rmtree(sandbox, ignore_errors=True)
        raise RuntimeError(
            f"агент {agent} не подключился за {self.ATTEMPTS_CONNECT} попыток: "
            f"{last!r}"
        )

    async def shutdown(self) -> None:
        for session in self.sessions:
            try:
                await session.close()
            except Exception as error:  # закрытие не должно ронять прогон
                print(f"! не закрылась сессия {session.agent}: {error}")


register("claude", ClaudeProvider)
