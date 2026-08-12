"""Основа для всех, кто говорит на OpenAI-совместимом /chat/completions.

Этим языком говорят xAI напрямую и OpenRouter как прослойка. Разница между ними
— адрес, заголовки и несколько дополнительных полей в теле, а форма сообщений,
разбор ответа и вытаскивание usage у них общие и лежат здесь.

Отдельно от openai.py нарочно: тот работает через Responses API, у него другая
форма и запроса, и ответа, и usage. Общего у них меньше, чем кажется по имени.
"""

from __future__ import annotations

from .общий import ПровайдерHTTP, Разбор, СессияHTTP, Токены


class ПровайдерЧата(ПровайдерHTTP):
    """Один запрос — вся история заново. Историю держит адаптер."""

    # Имя поля с уровнем размышления: у разных прослоек оно разное.
    поле_размышления = "reasoning_effort"

    # Входит ли размышление в completion_tokens. Общего правила нет, и ошибка
    # здесь стоит дорого: не сложить — выход и деньги занижены, сложить лишний
    # раз — завышены. Проверяется арифметикой самого вендора, см. _токены.
    размышление_отдельно = False

    def _url(self) -> str:
        return self.базовый_url

    def _сообщения(self, сессия: СессияHTTP) -> list[dict]:
        сообщения = [{"role": "system", "content": сессия.системный_промпт}]
        сообщения += [
            {"role": шаг["роль"], "content": шаг["текст"]} for шаг in сессия.история
        ]
        return сообщения

    def _тело(self, сессия: СессияHTTP) -> dict:
        тело: dict = {
            "model": сессия.модель,
            "messages": self._сообщения(сессия),
            "max_tokens": self.параметры.предел_ответа,
        }
        уровень = self.параметры.уровень(self.имя)
        if уровень and self.поле_размышления not in self.отброшенные:
            тело[self.поле_размышления] = уровень
        if self.параметры.температура is not None and "temperature" not in self.отброшенные:
            тело["temperature"] = self.параметры.температура
        if self.параметры.top_p is not None and "top_p" not in self.отброшенные:
            тело["top_p"] = self.параметры.top_p
        return тело

    def _размышление_вне_выхода(self, usage: dict) -> bool:
        """Спрашиваем у самого вендора, а не гадаем: сходится ли total.

        Если prompt + completion + reasoning == total, значит размышление лежит
        рядом с выходом, а не внутри него. Проверка по сумме надёжнее любого
        умолчания: вендор вправе поменять формат, и тогда мы это заметим сразу,
        а не через тридцать кругов в таблице расходов.
        """
        всего = usage.get("total_tokens")
        вход = usage.get("prompt_tokens")
        выход = usage.get("completion_tokens")
        разм = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
        if None in (всего, вход, выход) or not разм:
            return self.размышление_отдельно
        if вход + выход + разм == всего:
            return True
        if вход + выход == всего:
            return False
        return self.размышление_отдельно

    def _токены(self, usage: dict) -> Токены:
        размышление = int(
            (usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0
        )
        выход = int(usage.get("completion_tokens") or 0)
        if размышление and self._размышление_вне_выхода(usage):
            # Приводим к общему виду: выход всегда включает размышление, потому
            # что вендор берёт за него цену выхода.
            выход += размышление

        токены = Токены(
            вход=int(usage.get("prompt_tokens") or 0),
            выход=выход,
            размышление=размышление,
            кэш_чтение=int(
                (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
            ),
            кэш_запись=int(
                (usage.get("prompt_tokens_details") or {}).get("cache_write_tokens") or 0
            ),
            сырое=usage,
        )
        # prompt_tokens считает кэшированные вместе с обычными: без вычета вход
        # задваивается, и доля кэша выходит бессмысленной.
        токены.вход = max(0, токены.вход - токены.кэш_чтение)
        return токены

    def _разобрать(self, данные: dict) -> Разбор:
        выборы = данные.get("choices") or []
        первый = выборы[0] if выборы else {}
        сообщение = первый.get("message") or {}

        текст = (сообщение.get("content") or "").strip()
        сигнал = None
        if сообщение.get("refusal"):
            сигнал = "refusal"
            текст = текст or str(сообщение["refusal"])

        причина = первый.get("finish_reason")
        if причина == "content_filter":
            сигнал = сигнал or "content_filter"

        return Разбор(
            текст=текст,
            токены=self._токены(данные.get("usage") or {}),
            модель_факт=данные.get("model"),
            сигнал_отказа=сигнал,
            оборван=причина == "length",
        )
