# Runbook

Что делать с работающим пайплайном: как запустить, за чем следить, что
означает каждая жалоба и как её чинить. Архитектура и смысл ступеней — в
[README](README.md), здесь только эксплуатация.

## ATLAS dry-run deploy (English)

This desk runs paper until a human writes **promote**. Do not set
`GROKBOT_MODE=live` in compose or in a committed file.

### First start

```bash
cp .env.example .env
# put GROKBOT_GROK_API_KEY in .env — nothing else is required for dry-run
mkdir -p config logs state
cp config.atlas.yaml config/config.yaml
docker compose up -d
docker compose logs -f
curl -s localhost:8080/healthz | jq
```

Bare metal: `cp config.atlas.yaml config.yaml`, export `GROKBOT_*`, then
`grokbot doctor` and `grokbot run`.

ATLAS caps in `config.atlas.yaml`: one open position, 0.05 SOL per trade
and total exposure, 0.1 SOL daily loss, 10 trades/day, health on
`127.0.0.1:8080`. Separate book from Kraken atlas-p1. Dedicated hot
wallet later — never a Phantom or SafePal seed.

### Kill switch

If the kill file exists, **new buys are refused**. Open positions still
get stop-loss / take-profit / trailing / max-hold.

| where | file | stop buys | resume |
|---|---|---|---|
| host | `$GROKBOT_KILL_FILE` or `./KILL` | `touch KILL` | `rm KILL` |
| Docker | `/app/state/KILL` (compose default) | `touch state/KILL` | `rm state/KILL` |

`/healthz` has `"killed": true` while the file is present. Doctor warns.
This is the fast stop; SIGTERM is the clean process stop.

### Promote (human step, not a default)

Promote is writing the word and meaning it — not checking a box in yaml.

1. Days of dry-run with `grokbot replay` you actually read.
2. Dedicated hot wallet, only the cash you can lose. Not the desk seed.
3. Real key via `GROKBOT_WALLET_PRIVATE_KEY`, never committed.
4. `mode: live` only in the local `config.yaml` or env.
5. Start with `--i-understand-the-risk`.
6. `grokbot doctor` must pass (live without a key still fails).

Live executor sends one wallet's bonding-curve buy/sell. No snipe-and-dump
helpers, no extra wallets, no wash. Fail closed: no key, RPC error, or
missing confirmation means no fill is recorded.

## Запуск

### Голая машина

```bash
make dev                      # venv + зависимости + линтер и тесты
cp config.example.yaml config.yaml
$EDITOR config.yaml           # или ключи через GROKBOT_* в окружении

grokbot doctor                # предполётная проверка: ключи, сеть, права, место
grokbot run                   # запуск (режим берётся из конфига)
```

`grokbot doctor` стоит гонять перед каждым запуском и после каждого
изменения окружения. Он не тратит токены модели: ключ Grok проверяется
списком моделей, а не вызовом.

### Docker

```bash
cp .env.example .env && $EDITOR .env
mkdir -p config logs state && cp config.example.yaml config/config.yaml
docker compose up -d
docker compose logs -f
```

Тома `logs/` и `state/` обязаны быть снаружи контейнера: в `state/` лежат
открытые позиции и дневные лимиты, и потеря этого файла означает, что после
перезапуска пайплайн забудет и то, и другое.

### systemd

```ini
[Unit]
Description=grokbot-pumpfun
After=network-online.target

[Service]
User=grokbot
WorkingDirectory=/opt/grokbot-pumpfun
Environment=GROKBOT_GROK_API_KEY=xai-...
Environment=GROKBOT_HEALTH_PORT=8080
ExecStart=/opt/grokbot-pumpfun/.venv/bin/python -m src.cli run --config config.yaml
Restart=always
RestartSec=10
KillSignal=SIGTERM
TimeoutStopSec=45            # больше, чем ops.shutdown_grace_seconds
[Install]
WantedBy=multi-user.target
```

### launchd (macOS)

`~/Library/LaunchAgents/com.grokbot.pumpfun.plist`, ключевое:
`ProgramArguments` — тот же вызов, `KeepAlive` — true, `EnvironmentVariables`
— `GROKBOT_GROK_API_KEY`. Останов через `launchctl unload` шлёт SIGTERM,
то есть штатную остановку с сохранением состояния.

## Первые сутки

Порядок, который экономит деньги:

1. `mode: dry-run`, сутки работы, потом `make replay`.
2. Смотреть конверсию: если куплено 0 из тысяч — порог задран или агенты
   отказывают; если куплено больше десятка в час — порог занижен.
3. В разбивке причин отсева проверить, что работают все ступени. Если весь
   отсев на одной — остальные не получают данных.
4. В средних по компонентам искать всегда-нулевой компонент: это молчащий
   агент, а не строгий агент.

Только после этого имеет смысл разговор про `live`.

## Что смотреть в первую очередь

```bash
grokbot doctor                              # окружение
grokbot replay logs/trades.jsonl            # воронка, выходы, PnL
grokbot dashboard logs/trades.jsonl --watch 5
grokbot tune logs/trades.jsonl              # что дали бы другие веса
grokbot curve                               # во что обходится сделка
```

Воронка в `replay` отвечает на главный вопрос эксплуатации: **какая
ступень фактически принимает решения**. Если девяносто процентов потока
кончается на мониторе — фильтр задран. Если всё доходит до чекера и там
умирает — порог скоринга занижен, и вы платите за grok-4 впустую.

## За чем следить

```bash
curl -s localhost:8080/healthz | jq        # состояние
curl -s localhost:8080/metrics             # счётчики для Prometheus
python scripts/dashboard.py logs/trades.jsonl --watch 5
```

`/healthz` отдаёт 200 при `status: ok` и 503 при `degraded` — на это можно
вешать рестарт-политику. Поля:

| поле | смысл | когда плохо |
|---|---|---|
| `status` | сводка | `degraded` = цепь разомкнута или поток встал |
| `stalled` | нет create/skip/promote дольше 10 минут | `true` — сокет мёртв |
| `breaker` | `closed` / `half-open` / `open` | `open` — Grok не отвечает |
| `grok_budget_remaining` | остаток дневных вызовов | 0 — до полуночи UTC агенты молчат |
| `halted` | дневной лимит убытка выбран | `true` — торговли сегодня не будет |
| `blind_positions` | позиции без котировок | больше 0 — выходы по ним не работают |
| `open_positions` | открытые позиции | больше `max_open_positions` быть не может |
| `in_flight` | токенов в разборе | стабильно на потолке — упёрлись в лимит Grok |

Строка `жив: ...` в логе раз в `heartbeat_seconds` — то же самое, но в
журнале, чтобы по нему можно было восстановить историю.

## Уведомления

При заданном `alerts.webhook_url` (лучше через `GROKBOT_ALERT_WEBHOOK` —
в URL обычно токен) события приходят во внешний канал: `started`,
`stopped`, `buy`, `close`, `rug`, `breaker`, `halted`, `stalled`. Набор
задаётся в `alerts.events`.

Состояния сообщаются **на переходе**: `breaker` приходит один раз при
размыкании и один раз при восстановлении, а не каждую минуту. Поток
ограничен `max_per_minute`; лишнее выбрасывается и считается в
`alerts.dropped` в `/healthz`, а не копится в очереди.

Молчание канала само по себе ничего не значит — проверять живость надо по
`/healthz`, а не по отсутствию писем. Счётчик `alerts.failed` в `/healthz`
как раз показывает, сколько уведомлений не ушло.

## Инциденты

### `breaker: open`, в логе «цепь Grok разомкнута»

Столько-то вызовов подряд не удались. Пайплайн перестал звонить в Grok на
`breaker_cooldown_seconds` и **всё это время не покупает** — все агенты
отдают пессимистичный результат, чекер отвечает отказом.

Проверить: ключ жив (`curl` к api.x.ai), не кончились ли деньги на счёте
xAI, нет ли 429. Цепь замкнётся сама после кулдауна, разведочный вызов
покажет, починилось ли.

### `grok_budget_remaining: 0`

Выбран `ops.max_grok_calls_per_day`. Это защита от того, чтобы всплеск
лончей не съел месячный бюджет за вечер. До полуночи UTC агенты не
вызываются. Если это штатная нагрузка — поднять потолок; если нет —
поднять `filter.min_total_score`, чтобы до агентов доходило меньше.

### `stalled: true`

Из сокета не приходило **сообщений** (create / skip / promote) дольше
десяти минут. Skip на мониторе тоже живость: иначе `/healthz` висел
`unhealthy`, пока хоть один лонч не доходил до Grok. Монитор
переподключается сам с нарастающей паузой; если `stalled` держится —
проверить `data.ws_url` и сеть. Открытые позиции при этом всё ещё под
присмотром стоп-лосса: он ходит по REST, а не по сокету.

### Monitor never promotes (all `stale_no_traction`)

If `trades.jsonl` is only `type=skip stage=monitor reason=stale_no_traction`
and `detail` shows `buyers=0`, the trade tape is not landing. Paid
PumpPortal `subscribeTokenTrade` stays **off** (it billed HA8). v3
`/trades` 404s without a site JWT. After 0.4.4 the monitor REST-fills
`unique_buyers` from the public v3 coin card when `last_trade_timestamp`
is set or `real_sol_reserves > 0.3 SOL`. A brand-new card with no last
trade and ~0 real SOL stays `few_buyers`. Gates are unchanged (5 buyers,
120s, curve under 40%).

**How to tell the fix worked** (dry-run, no wallet):

```json
{"type":"promote","mode":"dry-run","stage":"monitor","reason":"ok",
 "detail":"buyers=6 age=131s curve=0.041","symbol":"CAT",
 "token":{"unique_buyers":6,"age_seconds":131,"curve_progress":0.041}}
```

A later paper buy looks like:

```json
{"type":"buy","mode":"dry-run","tx_hash":"dry_run","size_sol":0.05,
 "scores":{"total":0.72},"token":{"unique_buyers":6,"age_seconds":140}}
```

`intent` then `buy` with `tx_hash=dry_run` is the paper fill. `mode` must
stay `dry-run`. `curve_too_full` skips are expected for bonding-curve
graduates — those are not the bug.

### Analyzer skips every promote as `no_trade_data` (0.4.3)

If the monitor never promotes (`buyers=0`), that is the 0.4.4 coin-card
path — this section is only after `unique_buyers` is already 5+.

Promotes are landing (monitor `unique_buyers` 5+) but analyzer writes
`stage=analyzer reason=no_trade_data` and `grok_tokens_in` stays 0.
`frontend-api.pump.fun` is dead (HTTP 530 / Cloudflare 1016).
`frontend-api-v3.pump.fun /coins/{mint}` is the public card; `/trades` and
`/holders` 404 without a site JWT. PumpPortal `data.api_key` is not that
JWT. After 0.4.3 default `data.rest_url` is v3; empty REST trades do not
veto when the monitor already met `min_unique_buyers`; empty REST coin
uses `token.sol_in_curve` / `market_cap_sol`; live buy can quote the
on-chain bonding curve if the card is thin.

**How to tell the fix worked** (after a crewvet rebuild, not Hostinger
Update):

```bash
# analyzer veto should drop; Grok should start spending
docker exec grokbot-pumpfun python - <<'PY'
import json
from collections import Counter
rows = [json.loads(l) for l in open("/app/logs/trades.jsonl") if l.strip()]
print("promotes", sum(1 for r in rows if r.get("type")=="promote"))
print("analyzer", Counter(r.get("reason") for r in rows if r.get("stage")=="analyzer"))
print("intent/buy", Counter(r.get("type") for r in rows if r.get("type") in ("intent","buy")))
PY
curl -s localhost:18080/healthz | jq '{status,pending_launches,grok_tokens_in,trades_today}'
```

Expect: `analyzer.no_trade_data` stops climbing on new promotes;
`grok_tokens_in` moves; then `intent` and `buy` (dry-run `tx_hash=dry_run`,
or a live signature after a human promote). A leftover
`no_trade_data` tail from 0.4.2 is history — watch only new lines.

```bash
# on the VPS, after a crewvet image rebuild (do NOT Hostinger Update/Start)
docker exec grokbot-pumpfun grep -E '"type": "promote"|"type": "buy"' /app/logs/trades.jsonl | tail
docker logs grokbot-pumpfun 2>&1 | grep -E 'разбираем|КУПЛЕНО'
curl -s localhost:18080/healthz | jq '{status,stalled,pending_launches,trades_today,grok_tokens_in}'
```

Hostinger **Update** / **Start** reclones GitHub and wipes env. Restart
after rebuild is `docker stop` / `docker rm` / the same `docker run` or
compose up against the already-built `grokbot-pumpfun:latest`, keeping
`/opt/grokbot/secrets.env` and the log/state volumes.

### `blind_positions` больше нуля

По стольким открытым позициям несколько проходов подряд не приходит цена.
Это значит, что **правила выхода по ним сейчас не работают**: ни стоп-лосс,
ни take-profit, ни трейлинг. Проверить провайдера данных (`data.rest_url`),
лимиты по ключу и сеть. Пока цены нет, позиция живёт сама по себе — это тот
случай, когда стоит вмешаться руками.

### Позиция закрылась по причине `graduated`

Токен уехал на Raydium. Это хорошая новость и одновременно конец
применимости всей математики проекта: кривой больше нет, цена оттуда не
приходит. Позиция закрывается сразу, а выручка в dry-run считается по
споту без проскальзывания — в логе это прикидка, а не котировка. Если
такое случается часто, имеет смысл поднять `take_profit_pct`: бот выходит
раньше, чем токен доезжает.

### В логе `intent` без последующей покупки

Процесс умер между отправкой заявки и учётом позиции. **На кошельке могут
быть токены, о которых бот не знает.** Проверьте кошелёк руками; если
позиция есть, её придётся закрывать вручную — восстанавливать её в
состоянии бота на глаз не стоит, себестоимость всё равно будет неверной.

### `cooldown_left_seconds` больше нуля

Серия убытков подряд включила паузу. Это защита, а не поломка: новые
покупки не открываются, открытые позиции продолжают вестись правилами
выхода. Пауза заканчивается сама. Если она включается по несколько раз в
день — дело не в паузе, а в отборе: смотрите воронку и `tune`.

### «состояние занято другим процессом»

Запущен второй бот на том же файле состояния — он откажется стартовать.
Так и задумано: два процесса на одном кошельке перезапишут позиции друг
друга. Проверьте, что старый экземпляр действительно остановлен
(`grokbot doctor` покажет, чей PID держит замок); замок от мёртвого
процесса перехватывается автоматически.

### `halted: true`

Дневной лимит убытка выбран. Ничего чинить не нужно, счётчики сбросятся в
полночь UTC. Открытые позиции продолжают вестись стоп-лоссом.

### «состояние не читается — отложено в .corrupt»

Файл состояния побился (обычно — диск кончился в момент записи). Пайплайн
стартовал с чистого листа: **он не знает про открытые позиции**. Открыть
`state/pipeline.json.corrupt`, вынуть из него список позиций и разобраться
с ними руками. Пока это не сделано, стоп-лосс по ним не работает.

### Позиции закрываются не тем правилом, что ожидалось

`make replay` печатает причины закрытия. Что это значит:

* почти всё в `max_hold` — рынок не даёт движения, либо `max_hold_seconds`
  слишком мал для выбранных токенов;
* почти всё в `trailing_stop` при мелком плюсе — `trailing_stop_pct` уже
  обычного шума мемкоина, откат ловится на первом же движении;
* `take_profit` не срабатывает никогда — порог выше, чем реально
  проходят отобранные токены; смотреть распределение `pnl_pct` в закрытых.

### Позиции остались открытыми после остановки

При штатной остановке это нормально и в лог пишется предупреждение:
пайплайн не продаёт всё подряд на выходе. Стоп-лосс по этим позициям не
работает, пока процесс не поднят снова. Поэтому долгий простой при
открытых позициях — риск, а не пауза.

### `execution_failed` в логе (live)

`LiveExecutor` отказался отправлять сделку: нет ключа, RPC/Jito не
ответил, нет подтверждения, кривая уже на Raydium. Это отказ ступени, не
покупка. Проверьте кошелёк, если в логе уже есть `intent`.

### `kill_switch` в логе

Лежит файл `KILL` (или `$GROKBOT_KILL_FILE`). Новые покупки закрыты,
открытые позиции продолжают вестись. Уберите файл, когда снова можно
покупать.

### Конфиг не принят на старте

Сообщение перечисляет все проблемы разом. Это не придирка: каждое из них —
либо нерабочая настройка (нулевой лимит), либо небезопасная (live без
ключа кошелька). `make check-config` показывает то же самое, не запуская
торговлю.

## Обновление

```bash
git pull
make check                # линтер, типы, тесты — до перезапуска, не после
systemctl restart grokbot # или docker compose up -d --build
```

Состояние переживает перезапуск: позиции восстановятся, счётчики дня и
расход Grok продолжатся с того же места. Формат состояния версионирован
(`version` в файле); при несовпадении версии счётчики дня сбрасываются, а
позиции читаются.

## Бэкап

Резервировать `state/pipeline.json` (позиции — это деньги) и `logs/*.jsonl`
(без них не посчитать результат). Конфиг с ключами не бэкапить в общие
хранилища — ключи проще перевыпустить.

## Чек-лист перед переходом в live

- [ ] сутки в `dry-run` отработаны, `replay` разобран
- [ ] human wrote "promote" — not a config default
- [ ] dedicated hot wallet, never Phantom/SafePal seed
- [ ] `risk.*` are the ATLAS caps (or tighter), not the upstream example
- [ ] `state/` на диске, который переживёт перезапуск машины
- [ ] `/healthz` заведён в мониторинг, алерт на 503 настроен
- [ ] kill file path is known (`touch` / `rm`) and tested in dry-run
- [ ] `--i-understand-the-risk` добавлен в unit-файл осознанно
