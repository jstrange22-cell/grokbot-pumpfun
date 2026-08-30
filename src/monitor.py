"""WebSocket-монитор новых лончей pump.fun.

Первая ступень пайплайна и самая грубая: фильтрует кодом, без LLM, и
отсеивает порядка 94% потока. Всё, что сюда не пролезло, дальше не идёт и
токенов Grok не тратит.

Свежесозданный токен не может пройти фильтр по возрасту, поэтому лончи
кладутся в буфер `pending`, накапливают сделки из того же сокета и
проверяются повторно, когда дорастут до `min_age_seconds`.

Покупателей берём из ленты сделок, не из create. `subscribeNewToken`
бесплатный и живой. `subscribeTokenTrade` у PumpPortal платный: на ATLAS
он снимал ~0.01 SOL с HA8 каждые пару минут, поэтому его не шлём.
v3 `/trades/all/{mint}` и `/holders` — 404 без JWT сайта; публичная
карточка `GET /coins/{mint}` отдаёт `last_trade_timestamp` и
`real_sol_reserves`, но не `unique_buyers`. Если сокет сделок молчит,
REST-добор читает эту карточку и при живой торговле поднимает
`unique_buyers` до порога фильтра — иначе ничего не доходит до Grok.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
import websockets

from .analyzer import (
    apply_offchain_metadata,
    enrich_token,
    fetch_json,
    fetch_offchain_metadata,
    parse_trade,
    rest_base_urls,
    rows_from_payload,
)
from .curve import CURVE_COMPLETION_SOL, progress_from_sol
from .models import Config, FilterConfig, Token, is_placeholder

log = logging.getLogger(__name__)

__all__ = [
    "COIN_CARD_TRACTION_SOL",
    "CURVE_COMPLETION_SOL",
    "LaunchMonitor",
    "coin_card_has_traction",
    "data_socket_url",
    "monitor_detail",
    "parse_create_event",
    "passes_filter",
]

# Сколько держать лонч в буфере, если он так и не набрал покупателей.
PENDING_TTL_SECONDS = 900.0

# Потолки памяти. Процесс живёт сутками, а лончей на pump.fun тысячи в час:
# без ограничения и буфер, и список уже виденных растут без конца.
MAX_PENDING = 2_000
MAX_REMEMBERED = 20_000

# Платный subscribeTokenTrade выключен. Константа оставлена, чтобы
# случайно не вернуть подписку пачкой «на всякий случай».
MAX_TRADE_SUBS = 0

# REST-добор, когда сокет сделок молчит. Только дозревшие, с паузой
# между опросами одного минта — иначе 200 pending съедят лимит.
REST_REFRESH_SECONDS = 30.0
REST_BATCH = 12
REST_TRADE_LIMIT = 80

# Публичная v3-карточка не отдаёт unique_buyers. Считаем, что торги
# уже были, если есть last_trade_timestamp или в кривой больше 0.3 SOL.
LAMPORTS_PER_SOL = 1_000_000_000
COIN_CARD_TRACTION_SOL = 0.3


RestFetch = Callable[[str], Awaitable[tuple[list[dict[str, Any]], dict[str, Any]]]]


class SeenSet:
    """Множество последних N ключей. Старые вытесняются, память не течёт."""

    def __init__(self, maxlen: int = MAX_REMEMBERED) -> None:
        self.maxlen = maxlen
        self._items: OrderedDict[str, None] = OrderedDict()

    def add(self, key: str) -> None:
        self._items[key] = None
        self._items.move_to_end(key)
        while len(self._items) > self.maxlen:
            self._items.popitem(last=False)

    def __contains__(self, key: object) -> bool:
        return key in self._items

    def __len__(self) -> int:
        return len(self._items)


def data_socket_url(config: Config) -> str:
    """URL сокета. Ключ PumpPortal — в query, не в лог и не в yaml.

    `subscribeTokenTrade` у них с 2026 требует `?api-key=`. Плейсхолдер
    не подставляем: это не ключ, а мусор в query.
    """
    url = config.data.ws_url
    key = config.data.key
    if not key or is_placeholder(key):
        return url
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    if query.get("api-key"):
        return url
    query["api-key"] = key
    return urlunsplit(parts._replace(query=urlencode(query)))


def coin_card_has_traction(info: dict[str, Any] | None) -> bool:
    """Публичная v3-карточка показывает, что по минту уже торговали.

    `unique_buyers` в ответе нет. Достаточно `last_trade_timestamp` или
    `real_sol_reserves` > 0.3 SOL (лампаорты / 1e9). Новорождённый лонч
    с нулевым резервом и без сделки — нет.
    """
    if not info:
        return False
    last_trade = info.get("last_trade_timestamp")
    if last_trade is None:
        last_trade = info.get("lastTradeTimestamp")
    if last_trade not in (None, "", 0, 0.0):
        return True
    raw = info.get("real_sol_reserves")
    if raw is None:
        raw = info.get("realSolReserves")
    if raw in (None, ""):
        return False
    try:
        sol = float(raw) / LAMPORTS_PER_SOL
    except (TypeError, ValueError):
        return False
    return sol > COIN_CARD_TRACTION_SOL


def monitor_detail(token: Token) -> str:
    """Почему монитор так решил — в одну строку для JSONL `detail`."""
    return (
        f"buyers={token.unique_buyers} "
        f"age={round(token.age_seconds)}s "
        f"curve={token.curve_progress:.3f}"
    )


def _event_mint(payload: dict[str, Any]) -> str | None:
    mint = payload.get("mint") or payload.get("mintAddress") or payload.get("ca")
    return str(mint) if mint else None


def _event_wallet(payload: dict[str, Any]) -> str | None:
    wallet = (
        payload.get("traderPublicKey")
        or payload.get("wallet")
        or payload.get("user")
        or payload.get("trader")
    )
    return str(wallet) if wallet else None


def _is_buy_event(payload: dict[str, Any]) -> bool:
    tx_type = str(payload.get("txType") or payload.get("tx_type") or "").lower()
    if tx_type == "buy":
        return True
    return payload.get("is_buy") is True or payload.get("isBuy") is True


def parse_create_event(payload: dict[str, Any]) -> Token | None:
    """Событие создания токена -> Token. None, если событие не про создание."""
    if payload.get("txType") not in ("create", "created"):
        return None
    mint = _event_mint(payload)
    if not mint:
        return None

    sol_in_curve = float(payload.get("vSolInBondingCurve") or 0.0)
    created = payload.get("timestamp") or payload.get("createdTimestamp")
    created_ts = (
        float(created) / 1000.0
        if created and float(created) > 1e11
        else float(created or time.time())
    )

    return Token(
        mint=mint,
        name=payload.get("name"),
        symbol=payload.get("symbol"),
        description=payload.get("description"),
        image_uri=payload.get("image") or payload.get("image_uri"),
        metadata_uri=payload.get("uri") or payload.get("metadata_uri"),
        twitter=payload.get("twitter"),
        telegram=payload.get("telegram"),
        website=payload.get("website"),
        creator=payload.get("traderPublicKey") or payload.get("creator"),
        created_timestamp=created_ts,
        sol_in_curve=sol_in_curve,
        market_cap_sol=float(payload.get("marketCapSol") or 0.0),
        # Резерв, который отдаёт сокет, включает 30 виртуальных SOL: они
        # лежат в кривой с рождения и прогрессом не являются.
        curve_progress=progress_from_sol(sol_in_curve),
    )


def passes_filter(token: Token, cfg: FilterConfig) -> tuple[bool, str]:
    """Базовый фильтр. Возвращает (прошёл, причина отказа или "ok").

    Причина возвращается всегда — она уходит в лог как `skip.reason`, иначе
    потом не понять, на чём именно осыпался поток.
    """
    # Сначала окончательные приговоры (метаданные, переполненная кривая),
    # потом временные — токен с ними ещё может дозреть в буфере монитора.
    if cfg.require_metadata and not token.has_metadata:
        return False, "no_metadata"
    if token.curve_progress >= cfg.max_curve_progress:
        return False, "curve_too_full"
    if token.age_seconds < cfg.min_age_seconds:
        return False, "too_young"
    if token.unique_buyers < cfg.min_unique_buyers:
        return False, "few_buyers"
    return True, "ok"


class LaunchMonitor:
    """Подписка на новые токены и их сделки с фильтрацией на лету."""

    def __init__(
        self,
        config: Config,
        on_skip: Callable[[Token, str], None] | None = None,
        rest_fetch: RestFetch | None = None,
    ) -> None:
        self.config = config
        self.filter = config.filter
        self.on_skip = on_skip
        self.pending: dict[str, Token] = {}
        self._buyers: dict[str, set[str]] = {}
        self._emitted = SeenSet()
        self._rest_fetch = rest_fetch
        self._last_rest: dict[str, float] = {}
        # 0 = событий ещё не было. Пайплайн считает stall по max(своего
        # таймера, этого): create/skip тоже живость, не только promote.
        self.last_message_at: float = 0.0

    # -- обработка событий -------------------------------------------------

    def handle_event(self, payload: dict[str, Any]) -> Token | None:
        """Одно сообщение из сокета. Возвращает токен, если он готов идти дальше."""
        tx_type = payload.get("txType")

        if tx_type in ("create", "created"):
            self.last_message_at = time.time()
            token = parse_create_event(payload)
            if token and token.mint not in self._emitted:
                self._evict_if_crowded()
                self.pending[token.mint] = token
                # Создателя в покупатели не записываем: нужен счётчик
                # посторонних кошельков, а не всех подряд.
                self._buyers[token.mint] = set()
            return None

        mint = _event_mint(payload)
        if not mint or mint not in self.pending:
            return None

        self.last_message_at = time.time()
        return self._apply_market_event(self.pending[mint], payload)

    def _apply_market_event(self, token: Token, payload: dict[str, Any]) -> Token | None:
        mint = token.mint
        wallet = _event_wallet(payload)
        if _is_buy_event(payload) and wallet and wallet != token.creator:
            self._buyers[mint].add(wallet)
        token.unique_buyers = len(self._buyers[mint])
        token.ws_buyers = token.unique_buyers
        token.buyers_inferred = False

        sol_in_curve = payload.get("vSolInBondingCurve")
        if sol_in_curve is not None:
            token.sol_in_curve = float(sol_in_curve)
            token.curve_progress = progress_from_sol(token.sol_in_curve)
        if payload.get("marketCapSol") is not None:
            token.market_cap_sol = float(payload["marketCapSol"])

        return self._promote(token)

    async def enrich_from_uri(self, token: Token) -> Token:
        """Если сокет не дал имя, взять его из JSON по `uri`.

        Без data.api_key: это публичный Metaplex-файл (часто IPFS), не
        платный REST провайдера. Падаем мягко — без имени фильтр всё
        равно отсечёт как no_metadata.
        """
        if token.name or not token.metadata_uri:
            return token
        info = await fetch_offchain_metadata(
            token.metadata_uri,
            request_timeout=self.config.data.request_timeout,
        )
        return apply_offchain_metadata(token, info)

    def _promote(self, token: Token) -> Token | None:
        """Проверить дозревший токен и вынуть его из буфера, если решение принято."""
        ok, reason = passes_filter(token, self.filter)
        if ok:
            self._forget(token.mint)
            self._emitted.add(token.mint)
            return token
        # too_young / few_buyers — ещё может дозреть, остальное окончательно
        if reason in ("too_young", "few_buyers"):
            return None
        self._forget(token.mint)
        self._emitted.add(token.mint)
        if self.on_skip:
            self.on_skip(token, reason)
        return None

    def sweep(self, now: float | None = None) -> list[Token]:
        """Пройтись по буферу: дозревшие — наружу, протухшие — вон."""
        now = now or time.time()
        ready: list[Token] = []
        for mint in list(self.pending):
            token = self.pending[mint]
            promoted = self._promote(token)
            if promoted is not None:
                ready.append(promoted)
            elif now - token.created_timestamp > PENDING_TTL_SECONDS:
                self._forget(mint)
                self._emitted.add(mint)
                if self.on_skip:
                    self.on_skip(token, "stale_no_traction")
        return ready

    async def refresh_from_rest(self, now: float | None = None) -> None:
        """Добрать покупателей и кривую, если сокет сделок молчит.

        Публичный `data.rest_url` (v3 /coins), без PumpPortal api-key и
        без JWT сайта. /trades на v3 — 404; тогда unique_buyers берём
        с карточки, если last_trade_timestamp задан или real_sol_reserves
        > 0.3 SOL. Только токены старше min_age, у которых ещё не
        набралось покупателей. Сбой — тишина, не промоут.
        """
        now = now or time.time()
        due = [
            token
            for token in self.pending.values()
            if token.age_seconds >= self.filter.min_age_seconds
            and token.unique_buyers < self.filter.min_unique_buyers
            and now - self._last_rest.get(token.mint, 0.0) >= REST_REFRESH_SECONDS
        ]
        due.sort(key=lambda token: token.created_timestamp)
        due = due[:REST_BATCH]
        if not due:
            return

        fetcher = self._rest_fetch or self._default_rest_fetch
        for token in due:
            self._last_rest[token.mint] = now
            try:
                trades_raw, info = await fetcher(token.mint)
            except Exception as exc:
                log.warning("REST-добор %s не удался: %s", token.mint[:8], exc)
                continue
            self._apply_rest_snapshot(token, trades_raw, info)

    def _apply_rest_snapshot(
        self,
        token: Token,
        trades_raw: list[dict[str, Any]] | None,
        info: dict[str, Any] | None,
    ) -> None:
        if info:
            enrich_token(token, info)
            if info.get("complete"):
                token.curve_progress = 1.0
            elif token.sol_in_curve:
                token.curve_progress = progress_from_sol(token.sol_in_curve)

        buyers = self._buyers.setdefault(token.mint, set())
        for raw in trades_raw or []:
            if not isinstance(raw, dict):
                continue
            trade = parse_trade(raw)
            if trade.is_buy and trade.wallet and trade.wallet != token.creator:
                buyers.add(trade.wallet)
        token.unique_buyers = len(buyers)
        token.ws_buyers = len(buyers)
        token.buyers_inferred = False
        # v3 /trades 404 без JWT: карточка всё равно показывает, что
        # торги уже были. Иначе unique_buyers=0 и лонч не доходит до входа.
        # Это не живые кошельки — ws_buyers не поднимаем.
        if (
            token.unique_buyers < self.filter.min_unique_buyers
            and coin_card_has_traction(info)
        ):
            token.unique_buyers = self.filter.min_unique_buyers
            token.buyers_inferred = True

    async def _default_rest_fetch(
        self, mint: str
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        hosts = rest_base_urls(self.config.data.rest_url)
        timeout = self.config.data.request_timeout
        headers = {"Accept": "application/json"}
        async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
            # /trades на v3 — 404 без JWT сайта. Карточка /coins живая.
            trades_body = await fetch_json(
                client, f"/trades/all/{mint}", hosts, limit=REST_TRADE_LIMIT,
            )
            info_body = await fetch_json(
                client, f"/coins/{mint}", hosts, retry_empty=True,
            )
        trades_raw = rows_from_payload(trades_body)
        info = info_body if isinstance(info_body, dict) else {}
        return trades_raw, info

    def _forget(self, mint: str) -> None:
        self.pending.pop(mint, None)
        self._buyers.pop(mint, None)
        self._last_rest.pop(mint, None)

    def _evict_if_crowded(self) -> None:
        """Буфер переполнен — выкидываем самые старые недозревшие лончи."""
        while len(self.pending) >= MAX_PENDING:
            oldest = min(self.pending, key=lambda mint: self.pending[mint].created_timestamp)
            token = self.pending[oldest]
            self._forget(oldest)
            self._emitted.add(oldest)
            if self.on_skip:
                self.on_skip(token, "buffer_overflow")

    # -- сокет -------------------------------------------------------------

    async def stream(self) -> AsyncIterator[Token]:
        """Бесконечный поток отфильтрованных токенов. Переподключается сам."""
        sweeper_delay = 10.0
        backoff = 1.0
        while True:
            try:
                socket_url = data_socket_url(self.config)
                async with websockets.connect(socket_url) as ws:
                    await ws.send(json.dumps({"method": "subscribeNewToken"}))
                    # subscribeTokenTrade платный — не шлём. Покупателей
                    # добирает публичная v3-карточка в refresh_from_rest.
                    await self._sync_trade_subs(ws)
                    log.info("монитор подключён к %s", self.config.data.ws_url)
                    backoff = 1.0
                    last_sweep = time.time()
                    while True:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=sweeper_delay)
                        except TimeoutError:
                            raw = None
                        if raw:
                            try:
                                payload = json.loads(raw)
                            except json.JSONDecodeError:
                                continue
                            if isinstance(payload, dict):
                                token = self.handle_event(payload)
                                if token is not None:
                                    yield token
                                elif payload.get("txType") in ("create", "created"):
                                    mint = _event_mint(payload)
                                    pending = self.pending.get(mint) if mint else None
                                    if pending is not None:
                                        await self.enrich_from_uri(pending)
                                    await self._sync_trade_subs(ws)
                                elif payload.get("message") or payload.get("error"):
                                    # Отказ PumpPortal (нет api-key и т.п.) —
                                    # не событие рынка, но его надо видеть.
                                    log.warning("сокет: %s", payload)
                        if time.time() - last_sweep >= sweeper_delay:
                            last_sweep = time.time()
                            await self.refresh_from_rest()
                            for token in self.sweep():
                                yield token
                            await self._sync_trade_subs(ws)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # обрыв сокета — ждём и переподключаемся
                log.warning("монитор отвалился (%s), переподключение через %.0fs", exc, backoff)
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    async def _sync_trade_subs(self, ws: Any) -> None:
        """Платный subscribeTokenTrade не шлём.

        На ATLAS 2026-08-30 он снимал ~0.01 SOL с HA8 каждые ~2 минуты.
        Новые лончи идут через бесплатный subscribeNewToken; traction —
        из публичной v3-карточки. `ws` оставлен в сигнатуре, чтобы
        вызовы из stream не трогать.
        """
        if MAX_TRADE_SUBS <= 0:
            return
