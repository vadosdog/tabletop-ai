"""Промпты берутся из документа проекта, а не из копий.

Единственный источник правды — vypusk-1-wfrp-desyatina.md. Копии в репозитории
разъезжаются с документом на второй же правке, поэтому блоки вытаскиваются из
него на старте прогона.

Всё, чего документ не содержит, но требует оркестратор, лежит в
prompts/overrides/. Эта папка и есть список правок, которые надо вернуть в
документ проекта.
"""

from __future__ import annotations

import re
from pathlib import Path

HEADINGS = {
    "сеттинг": "## 0. Блок сеттинга",
    "партия": "## 0.5. Блок партии",
    "round_zero": "## 0.7. Круг ноль: вечер накануне",
    "gm": "## 1. Промпт мастеру",
    "игрок_общее": "## 2. Промпт игроку. Общая часть",
}

CARDS = {
    "Ханна": "### Игрок первый",
    "Ансельм": "### Игрок второй",
    "Курт": "### Игрок третий",
    "Лизель": "### Игрок четвёртый",
}


class PromptError(RuntimeError):
    pass


def _block_after(document: str, heading: str) -> str:
    """Первый fenced-блок после указанного заголовка."""
    position = document.find(heading)
    if position < 0:
        raise PromptError(f"в документе нет заголовка {heading!r}")

    tail = document[position + len(heading):]
    matched = re.search(r"^```[^\n]*\n(?P<тело>.*?)^```", tail, re.DOTALL | re.MULTILINE)
    if not matched:
        raise PromptError(f"после {heading!r} не нашёлся блок в тройных кавычках")
    return matched.group("тело").strip()


def _extras(root: Path, to: str) -> list[tuple[str, str]]:
    folder = root / "prompts" / "overrides" / to
    if not folder.is_dir():
        return []
    return [(fn.name, fn.read_text(encoding="utf-8").strip())
            for fn in sorted(folder.glob("*.txt"))]


class Prompts:
    def __init__(self, path_document: str | Path, root: str | Path):
        path = Path(path_document)
        if not path.is_file():
            raise PromptError(f"документ проекта не найден: {path}")
        document = path.read_text(encoding="utf-8")

        self.root = Path(root)
        self.blocks = {name: _block_after(document, hdr) for name, hdr in HEADINGS.items()}
        self.cards = {name: _block_after(document, hdr) for name, hdr in CARDS.items()}
        self.extras_gm = _extras(self.root, "gm")
        self.extras_player = _extras(self.root, "player")

    @property
    def round_zero(self) -> str:
        return self.blocks["round_zero"]

    def gm(self) -> str:
        parts = [self.blocks["сеттинг"], self.blocks["партия"], self.blocks["gm"]]
        parts += [text for _, text in self.extras_gm]
        return "\n\n---\n\n".join(parts)

    def player(self, character: str) -> str:
        if character not in self.cards:
            raise PromptError(f"нет карточки для {character!r}")
        parts = [
            self.blocks["сеттинг"],
            self.blocks["партия"],
            self.blocks["игрок_общее"],
            self.cards[character],
        ]
        parts += [text for _, text in self.extras_player]
        return "\n\n---\n\n".join(parts)

    def summary(self) -> dict:
        """Что и откуда взято — уходит в шапку лога."""
        return {
            "blocks": {name: len(text) for name, text in self.blocks.items()},
            "cards": {name: len(text) for name, text in self.cards.items()},
            "extras_gm": [name for name, _ in self.extras_gm],
            "extras_player": [name for name, _ in self.extras_player],
        }
