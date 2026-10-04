# Изменения

Формат: что изменилось и почему это имело значение. Версии — по смыслу, а
не по расписанию.

## 0.4.6 — 2026-08-30

- **Live buy failed Instruction 3 custom 0x17ae (6062 BuybackFeeRecipientMissing).**
  Classic `buy`/`sell` keys ended at `fee_config` + FEE_PROGRAM. The Apr/May
  2026 pump.fun upgrade requires two trailing remaining accounts on both
  sides: `bonding_curve_v2` PDA (readonly, seeds `[b"bonding-curve-v2", mint]`)
  and buyback fee recipient `5YxQFdt3Tr9zJLvkFccqXVUwhdTWJQc1fFg2YPbxvxeD`
  (writable). Existing account order is unchanged. Envelope unchanged:
  0.05 SOL/clip, 1 seat, Grok veto off, 0.1 SOL/day max loss.

## 0.4.5 — 2026-08-30

- **Four Grok agents per coin burned the daily xAI budget at 0 fills.**
  v3 `/trades` and `/holders` 404 without a site JWT; the 0.4.4 coin-card
  promote still sent every weak launch to auditor + narrative + timing +
  checker. Restart restored `grok_calls=2000` from `pipeline.json`, every
  agent failed `daily call budget exhausted`, and the book sat at 0 buys.
  Entry is now mechanical: WS buyers, bonding curve, public `/coins/{mint}`.
  A 0.05 clip can fire with Grok off. `ops.grok_entry_veto` (default
  **false**) is at most one checker call after a mechanical pass. Exhausted
  budget or an open breaker skips the veto and still trades. Envelope
  unchanged: 0.05 SOL/clip, 1 seat, 0.1 SOL/day max loss.
- **`daily_loss_limit_sol: 0` is not unlimited.** `halted` is
  `daily_loss >= limit`, so 0 >= 0 stops the book. 0 is coerced to the
  envelope 0.1 SOL/day. Negative values still fail startup.
- Coin-card inferred buyers (`last_trade` / reserve) no longer count as
  `ws_buyers`. Mechanical traction haircuts that path so a 404 tape does
  not look like 12 organic wallets.

## 0.4.4 — 2026-08-30

- **Monitor still never promoted after 0.4.3.** `unique_buyers` stayed 0
  because paid PumpPortal `subscribeTokenTrade` is off (it billed wallet
  HA8 ~0.01 SOL every ~2 minutes) and v3 `/trades/all/{mint}` 404s without
  a pump.fun site JWT. The public v3 coin card
  (`GET /coins/{mint}`) returns `last_trade_timestamp`,
  `real_sol_reserves`, `virtual_sol_reserves`, `market_cap` — not
  `unique_buyers`. The 0.4.3 analyzer `no_trade_data` bypass never fired:
  nothing reached Grok, `grok_tokens_in` stayed 0.
  REST refresh now treats a card with `last_trade_timestamp` set **or**
  `real_sol_reserves > 0.3 SOL` (lamports / 1e9) as traction and sets
  `unique_buyers` to at least `min_unique_buyers`. A brand-new card with
  no last trade and ~0 real SOL stays `few_buyers`. Paid
  `subscribeTokenTrade` stays off; `data.api_key` is still not sent as
  `Authorization`. Gates stay `min_unique_buyers=5`, `min_age_seconds=120`,
  `max_sol_per_trade=0.05`, `max_open_positions=1`.
  `daily_loss_limit_sol` is not restored to 0.1.

## 0.4.3 — 2026-08-30

- **Analyzer skipped every promote as `no_trade_data`.** After 0.4.2 the
  monitor correctly promoted tokens with 5–161 WebSocket buyers, then the
  analyzer called `frontend-api.pump.fun` (`/coins/{mint}`, holders,
  `/trades/all/{mint}`). That host is Cloudflare 1016 / HTTP 530.
  `frontend-api-v3.pump.fun /coins/{mint}` is public and returns
  `virtual_sol_reserves` / `market_cap` / `complete`. v3 `/trades` and
  `/holders` are 404 without a pump.fun site JWT — PumpPortal
  `data.api_key` is not that JWT and is no longer sent as `Authorization`.
  Default `data.rest_url` is now v3. A 530/429 on the configured host hops
  to the other known frontends. Empty REST trades no longer veto when
  `token.unique_buyers >= filter.min_unique_buyers`. Empty REST coin no
  longer forces `curve_too_thin` if the token already has enough
  `sol_in_curve` / `market_cap_sol`. Live (and dry-run via the shared
  helper) restore the curve from those fields; live also reads on-chain
  bonding-curve reserves when the card is thin, so a REST outage cannot
  block a fill after Grok approves.
- Monitor gates and ATLAS risk caps are unchanged
  (`min_unique_buyers=5`, `min_age_seconds=120`, `max_curve_progress=0.40`,
  `max_sol_per_trade=0.05`, `max_open_positions=1`, daily 0.1).

## 0.4.2 — 2026-08-30

- **Dry-run monitor never promoted.** After the metadata fix, ~10k
  `stage=monitor` skips were still all terminal: `stale_no_traction` with
  `few_buyers=0` / `too_young=0`. Buyer counts only came from per-mint
  `subscribeTokenTrade`. PumpPortal replaces the key list on each call, so
  one-mint subscribe left the rest of the buffer at `unique_buyers=0` until
  the 900s TTL. The same method is also metered and wants `?api-key=`.
  The monitor now resubscribes the **whole pending buffer** in one message,
  attaches a real `data.api_key` to the socket URL (never a placeholder,
  never logged), and REST-fills buyers from `data.rest_url` for age-ready
  tokens when the trade tape is silent. Gates stay
  `min_unique_buyers=5`, `min_age_seconds=120`, `max_curve_progress=0.40`,
  `require_metadata=true`. Image is still not required.
- Skip and promote JSONL lines now carry
  `detail: "buyers=N age=Xs curve=0.abc"`. `/healthz` treats create/skip
  as socket liveness, not only a promote — Docker HEALTHCHECK no longer
  goes `unhealthy` while the tape is flowing.

## 0.4.1 — 2026-08-30

- **PumpPortal create events were all skipped as `no_metadata`.**
  `Token.has_metadata` required `image_uri`, but `subscribeNewToken` typically
  sends `name`/`symbol`/`uri` and no separate image field. The picture lives
  in the metadata JSON at `uri`. A name plus `metadata_uri` now counts; the
  image is no longer required on the wire. `require_metadata` stays on.
- If the socket also omits `name`, the monitor fetches that JSON from `uri`
  (public HTTP/IPFS, no `data.api_key`) and fills identity fields. Fetch
  failure stays fail-closed: still `no_metadata`.

## 0.4.0 — 2026-08-30

ATLAS desk: dry-run remains the only default; live buy/sell sits behind
the existing gate.

- `LiveExecutor.buy` / `.sell` send a single-wallet pump.fun bonding-curve
  trade (solders Keypair, curve accounts, ATA, ComputeBudget, optional
  Jito bundle, confirmation). Fail closed on missing key, RPC error, or
  no confirmation. Tests mock RPC/Jito and never hit the network.
- Kill switch: file `KILL` or `$GROKBOT_KILL_FILE` blocks new buys;
  exits on open positions still run.
- `config.atlas.yaml` — committed ATLAS dry-run caps (placeholders only).
- English ATLAS ops in README/RUNBOOK. Promote is a human step.

## 0.3.0 — 2026-08-27

Версия про то, чтобы цифры в отчётах соответствовали тому, что произошло бы
на самом деле.

### Найденные ошибки

- **Прогресс кривой считался вместе с виртуальным резервом.** Монитор брал
  `vSolInBondingCurve` целиком, а в нём с рождения токена лежат 30
  виртуальных SOL. Фильтр «кривая заполнена меньше чем на 40%» на деле
  отрезал всё, что собрало больше ~4 реальных SOL вместо 34 — в восемь раз
  строже задуманного. Поток кандидатов был выморожен на ровном месте.
- **Покупка при неизвестной цене** создавала позицию с `entry_price = 0`:
  на такой позиции не срабатывает ни одно правило выхода, и она висела бы
  открытой вечно. Теперь это отказ от сделки.
- **Формула потолка заявки по влиянию на цену** была выведена неверно
  (использовала `1/(1−x)` вместо `1+x`) и давала заниженный результат.
  Выведена заново, совпадение с целью проверяется тестом до 1e-9.
- **Обработчик сигналов на Windows** захватывал переменную цикла: оба
  сигнала докладывали об одном и том же.

### Исполнение стало честным

- `src/curve.py`: постоянное произведение виртуальных резервов, комиссия,
  проскальзывание, влияние своей заявки, стоимость входа-выхода,
  восстановление резервов по спотовой цене.
- Цена входа в логе — средняя цена исполнения, а не котировка.
- Отсечки по торгуемости: тонкая кривая, дорогой круг, потолок размера
  позиции по ликвидности.

### Управление позицией

- Четыре правила выхода с приоритетом: стоп-лосс, take-profit (можно
  частичный), трейлинг от пика, лимит удержания. Пик переживает рестарт.
- Переезд токена на Raydium — отдельная причина выхода: кривой больше нет,
  и правила, считающие по ней, ослепли бы в лучший момент позиции.
- Потолок общей экспозиции: три позиции по потолку — одна большая ставка.
- Позиция без котировок несколько проходов подряд помечается слепой:
  ошибка в лог, `degraded` в `/healthz`, уведомление.

### Решения

- Память о создателях (`src/reputation.py`): адрес, чей токен уже
  сложился, отсекается до единого запроса к Grok. Строится по собственным
  закрытым сделкам, не по спискам извне.
- Пульс рынка (`src/market.py`): агент-тайминг получает измеренные
  наблюдения вместо внутренних счётчиков пайплайна.
- Версии промптов пишутся в лог покупки: без них подбор весов сравнивает
  решения разных ботов как одного.

### Эксплуатация

- `grokbot doctor` — предполётная проверка окружения. Модель не
  вызывается, токены не тратятся.
- `grokbot` — единая команда: `run`, `check`, `doctor`, `replay`,
  `dashboard`, `tune`, `curve`.
- Уведомления во внешний webhook (`src/alerts.py`), выключены по умолчанию.
- `intent` в логе перед отправкой заявки: смерть процесса между
  исполнением и учётом больше не оставляет незаметную позицию.
- Запись в лог не бросает исключений: кончившееся место на диске не должно
  бросать открытые позиции без присмотра.
- `scripts/tune.py` — подбор весов и порога по собственному логу, с прямо
  напечатанным ограничением: чем кончились бы отсеянные токены, лог не
  знает.

### Тесты

- Инварианты кривой вместо примеров.
- Симуляция торгового дня с проверкой сходимости денег после каждого шага.

## 0.2.0 — 2026-08-26

- Состояние переживает рестарт: позиции, дневные лимиты, расход Grok.
- Аккуратная остановка по SIGTERM, `/healthz` и `/metrics`, heartbeat.
- Три ограничителя расхода Grok: частота, дневной бюджет, предохранитель.
- Секреты как `SecretStr`, переменные окружения важнее файла, валидация
  конфига до старта.
- Ротация JSONL, ограниченная память монитора.
- CI на 3.11–3.13, Dockerfile, Makefile, RUNBOOK.

## 0.1.0 — 2026-08-26

Первая сборка по ТЗ: монитор, анализатор, четыре агента на Grok,
скоринг-матрица, риск-менеджер, dry-run, JSONL-лог, реплей, дашборд.
Исполнение транзакций оставлено заглушкой намеренно.
