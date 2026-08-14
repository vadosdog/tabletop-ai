#!/usr/bin/env python3
"""Шлёт транскрипт в телеграм кругами, по одному сообщению на круг.

    python3 scripts/транскрипт-в-тг.py runs/X/отчёт/транскрипт.md

У телеграма предел 4096 знаков на сообщение, а транскрипт — под двести тысяч.
Режем по кругам, а не по знакам: круг — цельная единица чтения, и рвать его
посередине реплики значит испортить то, ради чего это шлётся. Слишком длинный
круг доливается вторым сообщением по границе абзаца.
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
from pathlib import Path

LIMIT = 3800          # с запасом от 4096: заголовок и разметка тоже считаются
PAUSE = 1.5            # чтобы не упереться в частотный лимит бота
SCRIPT = Path(__file__).resolve().parent / "tg.sh"


def chunks(text: str, limit: int = LIMIT) -> list[str]:
    """Режет по пустым строкам, не разрывая абзац."""
    when_fits = [text] if len(text) <= limit else []
    if when_fits:
        return when_fits

    parts: list[str] = []
    current = ""
    for paragraph in text.split("\n\n"):
        if len(current) + len(paragraph) + 2 > limit and current:
            parts.append(current.rstrip())
            current = ""
        # Абзац сам по себе длиннее предела — режем по строкам, иначе никак.
        while len(paragraph) > limit:
            boundary = paragraph.rfind("\n", 0, limit)
            boundary = boundary if boundary > limit // 2 else limit
            parts.append(paragraph[:boundary].rstrip())
            paragraph = paragraph[boundary:].lstrip()
        current += paragraph + "\n\n"
    if current.strip():
        parts.append(current.rstrip())
    return parts


ATTEMPTS = 5


def send(text: str, index: str = "") -> None:
    """С повторами: тридцать сообщений подряд — тридцать шансов словить обрыв.

    Первая версия падала целиком от одного сбойного рукопожатия TLS на первом
    же сообщении. Сеть здесь ровно такая же ненадёжная, как у провайдеров, и
    лечится тем же — экспоненциальной паузой.
    """
    last: Exception | None = None
    for attempt in range(1, ATTEMPTS + 1):
        try:
            subprocess.run([str(SCRIPT)], input=text.encode("utf-8"), check=True)
            return
        except subprocess.CalledProcessError as error:
            last = error
            pause = 2.0 * attempt
            print(f"  ! {index} не ушло (код {error.returncode}), "
                  f"попытка {attempt} из {ATTEMPTS}, ждём {pause:.0f} с", flush=True)
            time.sleep(pause)
    raise RuntimeError(f"сообщение {index} не отправилось за {ATTEMPTS} попыток: {last!r}")


def main(path: Path) -> int:
    text = path.read_text(encoding="utf-8")
    # Шапка до первого круга — состав и кто за кого.
    blocks = re.split(r"(?=^## Круг )", text, flags=re.MULTILINE)

    sent = 0
    for index, block in enumerate(blocks):
        block = block.strip()
        if not block:
            continue
        parts = chunks(block)
        for n, part in enumerate(parts, 1):
            header = ""
            if len(parts) > 1:
                heading = block.splitlines()[0].lstrip("# ").strip()
                header = f"[{heading} — часть {n} из {len(parts)}]\n\n"
            send(header + part, f"блок {index}, часть {n}")
            sent += 1
            time.sleep(PAUSE)
        print(f"блок {index}: {len(parts)} сообщ.", flush=True)

    send(f"— конец транскрипта, {sent} сообщений —")
    print(f"итого отправлено: {sent + 1}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("нужен путь к транскрипт.md")
    sys.exit(main(Path(sys.argv[1])))
