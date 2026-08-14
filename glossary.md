# Глоссарий перехода на латиницу

Рабочий документ рефакторинга. Договорённость об именах принимается **до**
переименований: слепой перенос слепляет разные понятия, и это видно ниже в
разделе «Ловушки».

После рефакторинга остаётся справочником: русская игровая терминология слева,
имена в коде справа.

## Правило границы

| Что | Язык | Почему |
|---|---|---|
| Идентификаторы, ключи лога, ключи `config.json` | латиница | код читает посторонний |
| Промпты, карточки, брифы | русский | содержание эксперимента |
| Игровые теги `[РЕЖИМ: …]`, `[ФИНАЛ]`, `[ВЫБЫЛ]`, `[РЕШИМОСТЬ: …]` | русский | уходят в контекст модели |
| Таблица сложностей, названия навыков и характеристик | русский | парсятся из русского текста мастера |
| Имена персонажей, значения режима `РАЗГОВОР`/`ДЕЙСТВИЕ` | русский | игровые данные |
| Ключи ответа судьи: `критерии`, `балл`, `круг`, `цитата`, `пояснение` | русский | схему диктует `scoring/rubric.md`, это протокол с моделью |
| Текст отчётов — `отчёт.md`, `транскрипт.md`, заголовки `баллы.csv` | русский | материал для статьи |
| Комментарии и docstring | русский | по договорённости |

## Ловушки

Места, где один русский корень покрывает два разных понятия. Развести обязательно.

| Русское | Разводится на | Где |
|---|---|---|
| `Проверка` / `проверить` | `SkillCheck` — испытание навыка WFRP<br>`validate` / `check` — проверка в коде | `parse.py` vs `проверка.py` |
| `Состояние` / `состояния` | `ResumeState` — состояние для продолжения прогона<br>`conditions` — состояния бойца по WFRP | `restore.py` vs `combat.py` |
| `итог` (311 вхождений) | `summary` — итоговое событие, сводка<br>`total` — сумма чисел | везде |
| `цель` | `target_number` — целевое число броска<br>`target` — цель атаки, боец | `dice.py` vs `combat.py` |
| `имя` (536 вхождений) | `name` по умолчанию<br>`character` / `provider` / `filename` там, где ясно что именно | везде |
| `Результат` | `AttackResult` — слишком общее само по себе | `combat.py` |
| `деньги` / `стоимость` | оба → `cost`, свести к одному | `общий.py`, `base.py` |

Сокращения: `уу` (уровни успеха) → `sl` (success levels, термин WFRP);
`дубль` → `doubles` (дубль на кубах по WFRP).

## Классы

| Было | Стало | Файл |
|---|---|---|
| `Атака` | `Attack` | `parse.py` |
| `Боевка` | `Combat` | `combat.py` |
| `Боец` | `Combatant` | `combat.py` |
| `Бросок` | `Roll` | `dice.py` |
| `Журнал` | `Logbook` | `logbook.py` |
| `Кубики` | `Dice` | `dice.py` |
| `КубикиПоСписку` | `ScriptedDice` | `test_combat.py` |
| `ОстановкаПрогона` | `RunStopped` | `session.py` |
| `Ответ` | `Reply` | `base.py` |
| `Отказ` | `Refusal` | `отказы.py` |
| `ОтчётОКлючах` | `KeyReport` | `настройки.py` |
| `ОшибкаПровайдера` | `ProviderError` | `общий.py` |
| `ОшибкаПромптов` | `PromptError` | `prompts.py` |
| `Параметры` | `GenerationParams` | `настройки.py` |
| `ПодложнаяСессия` | `FakeSession` | `test_openrouter.py` |
| `Провайдер` | `Provider` | `base.py` |
| `ПровайдерClaude` | `ClaudeProvider` | `claude.py` |
| `ПровайдерGemini` | `GeminiProvider` | `gemini.py` |
| `ПровайдерHTTP` | `HttpProvider` | `общий.py` |
| `ПровайдерOpenAI` | `OpenAIProvider` | `openai.py` |
| `ПровайдерOpenRouter` | `OpenRouterProvider` | `openrouter.py` |
| `ПровайдерXAI` | `XAIProvider` | `xai.py` |
| `ПровайдерЗаглушка` | `StubProvider` | `stub.py` |
| `ПровайдерОтказчик` | `RefusingProvider` | `test_отказ_в_прогоне.py` |
| `ПровайдерЧата` | `ChatProvider` | `чат.py` |
| `Проверка` | `SkillCheck` | `parse.py` |
| `Прогон` | `Run` | `session.py` |
| `Промпты` | `Prompts` | `prompts.py` |
| `Разбор` | `ParsedResponse` | `общий.py` |
| `Расход` | `Usage` | `общий.py` |
| `Результат` | `AttackResult` | `combat.py` |
| `Сессия` | `Session` | `base.py` |
| `СессияClaude` | `ClaudeSession` | `claude.py` |
| `СессияHTTP` | `HttpSession` | `общий.py` |
| `СессияЗаглушка` | `StubSession` | `stub.py` |
| `СессияОтказчик` | `RefusingSession` | `test_отказ_в_прогоне.py` |
| `Состояние` | `ResumeState` | `restore.py` |
| `Токены` | `Tokens` | `общий.py` |
| `Ход` | `Turn` | `session.py` |

Порядок слов переворачивается: `ПровайдерClaude` → `ClaudeProvider`.

## Поля датаклассов = ключи лога

`как_словарь()` отдаёт `asdict(self)`, поэтому имя поля попадает в `лог.jsonl`
буквально. Переименование поля переименовывает ключ лога — это и нужно.

### `Roll` (`Бросок`)

| Было | Стало |
|---|---|
| `персонаж` | `character` |
| `навык` | `skill` |
| `характеристика` | `characteristic` |
| `сложность` | `difficulty` |
| `база` | `base` |
| `надбавка` | `advances` |
| `модификатор` | `modifier` |
| `цель` | `target_number` |
| `выпало` | `rolled` |
| `успех` | `success` |
| `уровни_успеха` | `success_levels` |
| `авто` | `auto` |
| `номер` | `index` |
| `зерно` | `seed` |
| `метки` | `tags` |
| `дубль` | `doubles` |

### `Combatant` (`Боец`)

| Было | Стало |
|---|---|
| `имя` | `name` |
| `характеристики` | `characteristics` |
| `размер` | `size` |
| `раны_макс` | `wounds_max` |
| `раны` | `wounds` |
| `броня` | `armour` |
| `оружие` | `weapon` |
| `состояния` | `conditions` |
| `травмы` | `injuries` |
| `криты_по_локациям` | `crits_by_location` |
| `штраф` | `penalty` |
| `судьба` | `fate` |
| `удача` | `fortune` |
| `удача_макс` | `fortune_max` |
| `стойкость` | `resilience` |
| `решимость` | `resolve` |
| `решимость_макс` | `resolve_max` |
| `мотивация` | `motivation` |
| `иммунитет_кругов` | `immune_rounds` |
| `без_сознания` | `unconscious` |
| `мёртв` | `dead` |
| `потерянные_конечности` | `lost_limbs` |

### `AttackResult` (`Результат`)

| Было | Стало |
|---|---|
| `атакующий` | `attacker` |
| `цель` | `target` |
| `оружие` | `weapon` |
| `бросок_атаки` | `attack_roll` |
| `цель_атаки` | `attack_target` |
| `уу_атаки` | `attack_sl` |
| `бросок_защиты` | `defence_roll` |
| `цель_защиты` | `defence_target` |
| `уу_защиты` | `defence_sl` |
| `разница` | `margin` |
| `попадание` | `hit` |
| `локация` | `hit_location` |
| `урон_до_брони` | `damage_before_armour` |
| `защита` | `defence` |
| `ран_снято` | `wounds_dealt` |
| `ран_осталось` | `wounds_left` |
| `дубль` | `doubles` |
| `крит` | `crit` |
| `состояния` | `conditions` |
| `травма` | `injury` |
| `смерть` | `death` |
| `ждёт_судьбу` | `awaiting_fate` |
| `метки` | `tags` |
| `раны_макс_цели` | `target_wounds_max` |

### `Turn` (`Ход`)

| Было | Стало |
|---|---|
| `персонаж` | `character` |
| `текст` | `text` |
| `тайно` | `secret` |
| `порядок` | `order` |
| `метки` | `tags` |
| `самобросок` | `self_roll` |
| `отказ` | `refusal` |

### `Reply` (`Ответ`) и `ParsedResponse` (`Разбор`)

| Было | Стало |
|---|---|
| `текст` | `text` |
| `провайдер` | `provider` |
| `модель` | `model` |
| `латентность_мс` | `latency_ms` |
| `токены` | `tokens` |
| `стоимость`, `деньги` | `cost` |
| `модель_факт` | `model_actual` |
| `метки` | `tags` |
| `сигнал_отказа` | `refusal_signal` |
| `оборван` | `truncated` |
| `апстрим` | `upstream` |

### `Usage` (`Расход`) и `Tokens` (`Токены`)

| Было | Стало |
|---|---|
| `вход` | `input` |
| `выход` | `output` |
| `размышление` | `reasoning` |
| `кэш_чтение` | `cache_read` |
| `кэш_запись` | `cache_write` |
| `сырое` | `raw` |
| `запросов` | `requests` |
| `повторов` | `retries` |
| `отказов` | `refusals` |
| `обрывов` | `truncations` |
| `секунд` | `seconds` |

### Остальные

| Класс | Было | Стало |
|---|---|---|
| `SkillCheck` | `персонаж`, `навык`, `сложность`, `строка`, `метки`, `база` | `character`, `skill`, `difficulty`, `line`, `tags`, `base` |
| `Attack` | `кто`, `по_кому`, `чем`, `защита`, `строка`, `метки` | `attacker`, `target`, `weapon`, `defence`, `line`, `tags` |
| `Refusal` | `уверенность`, `причина`, `цитата` | `confidence`, `reason`, `quote` |
| `ResumeState` | `последний_круг`, `режим`, `разговорных_кругов`, `выданные_числа`, `бросков`, `источник` | `last_round`, `mode`, `talk_rounds`, `issued_numbers`, `rolls`, `source` |
| `GenerationParams` | `температура`, `предел_ответа`, `размышление`, `таймаут_с`, `попыток`, `пауза_с`, `предел_паузы_с` | `temperature`, `max_output`, `reasoning`, `timeout_s`, `attempts`, `pause_s`, `max_pause_s` |
| `KeyReport` | `найденные`, `без_ключа` | `found`, `missing` |

## Типы событий лога

Проверено: в промпты не попадают ни разу, только внутренние.

| Было | Стало |
|---|---|
| `старт` | `start` |
| `продолжение` | `resume` |
| `ход` | `turn` |
| `доставка` | `delivery` |
| `бросок` | `roll` |
| `аномалия` | `anomaly` |
| `круг` | `round` |
| `финал` | `finale` |
| `итог` | `summary` |
| `остановка` | `stop` |
| `ресурс` | `resource` |

Общие поля события: `н` → `n`, `время` → `time`,
`секунд_от_старта` → `seconds_from_start`, `тип_события` → `event_type`,
`кто` → `who`, `кому` → `to`, `от_кого` → `from`, `символов` → `chars`,
`говорящий` → `speaker`, `видимость` → `visibility`,
`порядок_в_круге` → `order_in_round`, `причина` → `reason`,
`было`/`стало` → `before`/`after`, `заявлено` → `claimed`,
`подтверждено` → `confirmed`.

## Ключи `config.json`

| Было | Стало |
|---|---|
| `документ` | `document` |
| `зерно` | `seed` |
| `предел_кругов` | `max_rounds` |
| `финал_с_круга` | `finale_from_round` |
| `жёсткий_финал_с_круга` | `hard_finale_from_round` |
| `предел_ответов_мастера_за_круг` | `max_gm_replies_per_round` |
| `попыток_на_агента` | `attempts_per_agent` |
| `предел_стоимости_usd_на_агента` | `cost_limit_usd_per_agent` |
| `базовый_порядок` | `base_order` |
| `порядок_круга_ноль` | `round_zero_order` |
| `режим_по_умолчанию` | `default_mode` |
| `мастер` | `gm` |
| `игроки` | `players` |
| `вендоры_игроков` | `player_vendors` |
| `апстримы_openrouter` | `openrouter_upstreams` |
| `контрольный_игрок` | `control_player` |
| `контрольная_модель` | `control_model` |
| `параметры_генерации` | `generation_params` |
| `цены_за_миллион_usd` | `price_per_million_usd` |
| `судья` | `judge` |
| `персонажи` | `characters` |
| `надбавка_за_обученный_навык` | `trained_skill_advances` |
| `база_по_умолчанию` | `default_base` |
| `навыки_на_характеристики` | `skill_to_characteristic` |
| `персонажи_мира` | `npcs` |
| `оружие` | `weapons` |
| `размеры` | `sizes` |
| `состояния` | `conditions` |
| `история_раздач` | `draw_history` |
| `прошлая_раздача` | `last_draw` |

Значения не меняются: `"мастер": {"провайдер": "claude"}` → `"gm": {"provider": "claude"}`,
но `"базовый_порядок": ["Курт", …]` → `"base_order": ["Курт", …]` — имена персонажей русские.

## Имена файлов

| Было | Стало |
|---|---|
| `orchestrator/src/providers/настройки.py` | `settings.py` |
| `orchestrator/src/providers/общий.py` | `http.py` |
| `orchestrator/src/providers/отказы.py` | `refusals.py` |
| `orchestrator/src/providers/чат.py` | `chat.py` |
| `orchestrator/проверка.py` | `preflight.py` |
| `orchestrator/tests/test_отказы.py` | `test_refusals.py` |
| `orchestrator/tests/test_отказ_в_прогоне.py` | `test_refusal_in_run.py` |
| `orchestrator/tests/test_ресурсы.py` | `test_resources.py` |
| `orchestrator/криты.json` | `crits.json` |
| `scoring/раскрытие.json` | `reveal.json` |
| `scripts/сводка-прогона.py` | `run_summary.py` |
| `scripts/собрать-для-notion.py` | `build_for_notion.py` |
| `scripts/транскрипт-в-тг.py` | `transcript_to_tg.py` |

Имена папок прогонов в `runs/` и файлы внутри них (`лог.jsonl`, `отчёт.md`,
`комментарий.md`) не трогаем — это готовый материал.

## Флаги командной строки

| Было | Стало |
|---|---|
| `--дубли` | `--doubles` |
| `--указание` | `--note` |

Остальные флаги уже латиницей.
