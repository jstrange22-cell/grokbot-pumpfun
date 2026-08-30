"""REST-анализатор метрик токена. Вторая ступень, тоже без LLM.

Три запроса к провайдеру данных идут параллельно через asyncio.gather:
карточка токена, топ-холдеры, последние сделки. Дальше всё считается кодом —
агенты дорогие, и отдавать им токен, у которого создатель держит половину
предложения, смысла нет.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import statistics
from typing import Any

import httpx

from .curve import CurveState, real_sol_from_hint, round_trip_cost_pct, state_from_any
from .models import Config, Holder, MarketConfig, Token, TokenMetrics, Trade

log = logging.getLogger(__name__)

# Покупка в первые N секунд жизни токена считается снайпом.
SNIPER_WINDOW_SECONDS = 15.0

# Сколько последних сделок тянем для анализа.
TRADE_LIMIT = 200
HOLDER_LIMIT = 50

# v1 (frontend-api) — Cloudflare 1016 / HTTP 530 на 2026-08-30.
# v3 /coins/{mint} живой и публичный. /trades и /holders на v3 отдают 404
# без JWT сайта; ключ PumpPortal — не этот JWT, его сюда не кладём.
PUMP_REST_HOSTS = (
    "https://frontend-api-v3.pump.fun",
    "https://frontend-api.pump.fun",
    "https://frontend-api-v2.pump.fun",
)
RETRYABLE_STATUS = frozenset({
    408, 425, 429, 500, 502, 503, 504,
    520, 521, 522, 523, 524, 525, 526, 530,
})

# Безусловные вето. Взвешенная сумма их размывает: токен с создателем на
# четверти предложения набирал приемлемый риск за счёт хорошей кривой и
# живых соцсетей. Такие условия не компенсируются ничем, поэтому они
# выставляют максимальный риск, а не прибавляют к нему.
CREATOR_SHARE_VETO = 0.25
TOP5_SHARE_VETO = 0.80


def rest_base_urls(primary: str) -> tuple[str, ...]:
    """Primary first, then the known public pump.fun frontends, no dupes."""
    ordered: list[str] = []
    for url in (primary, *PUMP_REST_HOSTS):
        cleaned = (url or "").strip().rstrip("/")
        if cleaned and cleaned not in ordered:
            ordered.append(cleaned)
    return tuple(ordered)


def client_primary_url(client: httpx.AsyncClient | None, configured: str) -> str:
    """Injected test client keeps its base_url; otherwise the config host."""
    if client is None:
        return configured
    raw = str(client.base_url or "").rstrip("/")
    if raw in ("", "http://", "https://"):
        return configured
    return raw


def rows_from_payload(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, list):
        return [row for row in raw if isinstance(row, dict)]
    if isinstance(raw, dict):
        rows = raw.get("trades") or raw.get("data") or raw.get("holders") or []
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    return []


async def fetch_json(
    client: httpx.AsyncClient,
    path: str,
    hosts: tuple[str, ...],
    *,
    retry_empty: bool = False,
    **params: Any,
) -> Any:
    """GET path on each host. Logs path and status only — never headers or keys.

    530/429/5xx hop to the next host. 404 is empty for this path (v3 /trades
    and /holders without a site JWT) and does not hop: the dead v1 host
    would only add latency. Empty `{}` on a coin card does hop.
    """
    suffix = path if path.startswith("/") else f"/{path}"
    for host in hosts:
        url = f"{host}{suffix}"
        try:
            resp = await client.get(url, params=params)
        except Exception as exc:
            log.warning("запрос %s не удался: %s", path, exc)
            continue
        if resp.status_code in RETRYABLE_STATUS:
            log.warning("запрос %s -> %s", path, resp.status_code)
            continue
        if resp.status_code == 404:
            log.warning("запрос %s -> 404", path)
            return None
        if not resp.is_success:
            log.warning("запрос %s -> %s", path, resp.status_code)
            continue
        try:
            data = resp.json()
        except Exception as exc:
            log.warning("запрос %s не JSON: %s", path, exc)
            continue
        if retry_empty and not data:
            log.warning("запрос %s пустой, пробуем запасной хост", path)
            continue
        return data
    return None


class Analyzer:
    """Тянет сырые данные и сводит их в TokenMetrics."""

    def __init__(self, config: Config, client: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self.data = config.data
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> Analyzer:
        if self._client is None:
            # Не Bearer: data.api_key — ключ PumpPortal, не JWT pump.fun.
            # На v3 он не открывает /trades, а в логах светить его незачем.
            self._client = httpx.AsyncClient(
                base_url=self.data.rest_url,
                timeout=self.data.request_timeout,
                headers={"Accept": "application/json"},
            )
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("Analyzer используется вне `async with`")
        return self._client

    # -- сеть --------------------------------------------------------------

    def _hosts(self) -> tuple[str, ...]:
        return rest_base_urls(client_primary_url(self._client, self.data.rest_url))

    async def _get(self, path: str, *, retry_empty: bool = False, **params: Any) -> Any:
        return await fetch_json(
            self.client, path, self._hosts(), retry_empty=retry_empty, **params,
        )

    async def fetch(self, mint: str) -> tuple[dict[str, Any], list[Holder], list[Trade]]:
        """Карточка, холдеры и сделки — тремя параллельными запросами."""
        info, holders_raw, trades_raw = await asyncio.gather(
            self._get(f"/coins/{mint}", retry_empty=True),
            self._get(f"/coins/{mint}/holders", limit=HOLDER_LIMIT),
            self._get(f"/trades/all/{mint}", limit=TRADE_LIMIT),
        )
        return (
            info if isinstance(info, dict) else {},
            [parse_holder(h) for h in rows_from_payload(holders_raw)],
            [parse_trade(t) for t in rows_from_payload(trades_raw)],
        )

    async def gather(
        self, token: Token,
    ) -> tuple[dict[str, Any], list[Holder], list[Trade], CurveState | None]:
        """Сеть плюс фолбэк кривой с полей токена, если карточка REST пустая."""
        info, holders, trades = await self.fetch(token.mint)
        enrich_token(token, info)
        curve = state_from_any(
            info, token.market_cap_sol, sol_in_curve=token.sol_in_curve,
        )
        return info, holders, trades, curve

    async def inspect(
        self, token: Token,
    ) -> tuple[list[Holder], list[Trade], CurveState | None, TokenMetrics]:
        """То, что пайплайну нужно до агентов: сырьё, кривая, метрики."""
        _info, holders, trades, curve = await self.gather(token)
        metrics = compute_metrics(
            token, holders, trades, curve, self.config.market,
            planned_sol=self.config.risk.max_sol_per_trade,
        )
        return holders, trades, curve, self.adopt_ws_buyers(token, metrics)

    async def analyze(self, token: Token) -> TokenMetrics:
        """Полный проход: сходить в сеть и посчитать метрики."""
        _holders, _trades, _curve, metrics = await self.inspect(token)
        return metrics

    def adopt_ws_buyers(self, token: Token, metrics: TokenMetrics) -> TokenMetrics:
        """Пустой REST-tape не обнуляет покупателей, которых уже посчитал монитор."""
        needed = self.config.filter.min_unique_buyers
        if metrics.trade_count == 0 and token.unique_buyers >= needed:
            metrics.trade_count = token.unique_buyers
            metrics.unique_wallets = max(metrics.unique_wallets, token.unique_buyers)
        return metrics

    def passes(self, metrics: TokenMetrics, token: Token | None = None) -> tuple[bool, str]:
        """Отсечка по риск-скору и торгуемости. Возвращает (прошёл, причина)."""
        market = self.config.market
        buyers = token.unique_buyers if token is not None else 0
        if metrics.trade_count == 0 and buyers < self.config.filter.min_unique_buyers:
            return False, "no_trade_data"
        liquidity = metrics.curve_liquidity_sol
        if liquidity < market.min_curve_liquidity_sol and token is not None:
            liquidity = max(liquidity, real_sol_from_hint(token.sol_in_curve))
        if liquidity < market.min_curve_liquidity_sol:
            # Из тонкой кривой не выйти: своя же продажа обвалит цену.
            return False, "curve_too_thin"
        if (metrics.round_trip_cost_pct
                and metrics.round_trip_cost_pct > market.max_round_trip_cost_pct):
            return False, "round_trip_too_expensive"
        if metrics.risk_score > self.config.filter.max_risk_score:
            return False, "risk_score_too_high"
        return True, "ok"


# --------------------------------------------------------------------------
# Разбор ответов провайдера
# --------------------------------------------------------------------------


def parse_holder(raw: dict[str, Any]) -> Holder:
    amount = float(raw.get("amount") or raw.get("balance") or 0.0)
    share = raw.get("share")
    if share is None:
        pct = raw.get("percentage")
        share = float(pct) / 100.0 if pct is not None else 0.0
    return Holder(
        address=str(raw.get("address") or raw.get("wallet") or raw.get("owner") or ""),
        amount=amount,
        share=float(share),
        is_creator=bool(raw.get("is_creator") or raw.get("isCreator")),
    )


def parse_trade(raw: dict[str, Any]) -> Trade:
    ts = float(raw.get("timestamp") or 0.0)
    if ts > 1e11:  # миллисекунды
        ts /= 1000.0
    is_buy = raw.get("is_buy")
    if is_buy is None:
        is_buy = str(raw.get("txType", "buy")).lower() == "buy"
    return Trade(
        signature=raw.get("signature") or raw.get("tx"),
        wallet=str(raw.get("user") or raw.get("wallet") or raw.get("traderPublicKey") or ""),
        is_buy=bool(is_buy),
        sol_amount=float(raw.get("sol_amount") or raw.get("solAmount") or 0.0),
        token_amount=float(raw.get("token_amount") or raw.get("tokenAmount") or 0.0),
        timestamp=ts,
        slot=raw.get("slot"),
    )


def resolve_metadata_url(uri: str) -> str:
    """ipfs://CID → публичный HTTP-шлюз. https остаётся как есть."""
    if uri.startswith("ipfs://"):
        cid = uri[len("ipfs://"):].lstrip("/")
        return f"https://ipfs.io/ipfs/{cid}"
    return uri


def apply_offchain_metadata(token: Token, info: dict[str, Any]) -> Token:
    """Поля из Metaplex-JSON по `uri`: имя, тикер, картинка, соцсети."""
    if not info:
        return token
    token.name = token.name or info.get("name")
    token.symbol = token.symbol or info.get("symbol")
    token.description = token.description or info.get("description")
    token.image_uri = token.image_uri or info.get("image_uri") or info.get("image")
    token.twitter = token.twitter or info.get("twitter")
    token.telegram = token.telegram or info.get("telegram")
    token.website = token.website or info.get("website")
    return token


async def fetch_offchain_metadata(
    uri: str,
    request_timeout: float = 10.0,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Скачать JSON по `uri`. Без API-ключа: это публичный файл, не data API."""
    url = resolve_metadata_url(uri)
    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=request_timeout)
    try:
        resp = await http.get(url)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        log.warning("офчейн-метаданные %s не прочитались: %s", uri, exc)
        return {}
    finally:
        if owns_client:
            await http.aclose()


def enrich_token(token: Token, info: dict[str, Any]) -> Token:
    """Дописать в токен то, чего не было в событии сокета."""
    if not info:
        return token
    token.name = token.name or info.get("name")
    token.symbol = token.symbol or info.get("symbol")
    token.description = token.description or info.get("description")
    token.image_uri = token.image_uri or info.get("image_uri") or info.get("image")
    token.twitter = token.twitter or info.get("twitter")
    token.telegram = token.telegram or info.get("telegram")
    token.website = token.website or info.get("website")
    token.creator = token.creator or info.get("creator")
    if info.get("market_cap") is not None:
        token.market_cap_sol = float(info["market_cap"])
    if info.get("virtual_sol_reserves") is not None:
        token.sol_in_curve = float(info["virtual_sol_reserves"]) / 1e9
    return token


# --------------------------------------------------------------------------
# Метрики (чистая функция, чтобы её можно было гонять без сети)
# --------------------------------------------------------------------------


def compute_metrics(
    token: Token,
    holders: list[Holder],
    trades: list[Trade],
    curve: CurveState | None = None,
    market: MarketConfig | None = None,
    planned_sol: float = 0.0,
) -> TokenMetrics:
    """Свести сырьё в метрики и риск-скор 0..10.

    Если известно состояние кривой, сюда же попадает стоимость входа и
    выхода: на тонкой кривой она съедает движение, ради которого сделка
    затевалась, и это надо видеть до решения, а не после.
    """
    buys = [t for t in trades if t.is_buy]
    sells = [t for t in trades if not t.is_buy]
    wallets = {t.wallet for t in trades if t.wallet}

    top5_share = sum(h.share for h in sorted(holders, key=lambda h: h.share, reverse=True)[:5])
    creator_share = next(
        (
            h.share
            for h in holders
            if h.is_creator or (token.creator and h.address == token.creator)
        ),
        0.0,
    )

    sniper_count = _count_snipers(token, buys)
    diversity = _wallet_diversity(buys)
    socials = _social_signals(token)
    curve_health = _curve_health(buys)

    buy_sell_ratio = len(buys) / len(sells) if sells else float(len(buys))

    risk = _risk_score(
        top5_share=top5_share,
        creator_share=creator_share,
        sniper_count=sniper_count,
        diversity=diversity,
        socials=socials,
        curve_health=curve_health,
        trade_count=len(trades),
    )
    veto = _veto_reason(creator_share, top5_share)
    if veto:
        log.info("%s отсечён безусловно: %s", token.mint[:8], veto)
        risk = 10.0

    liquidity = curve.real_sol if curve else 0.0
    cost = (
        round_trip_cost_pct(curve, planned_sol, (market or MarketConfig()).trade_fee_pct)
        if curve and planned_sol > 0
        else 0.0
    )

    return TokenMetrics(
        curve_liquidity_sol=round(liquidity, 4),
        round_trip_cost_pct=round(cost, 4),
        top5_share=round(min(1.0, top5_share), 4),
        creator_share=round(min(1.0, creator_share), 4),
        sniper_count=sniper_count,
        wallet_diversity=round(diversity, 4),
        social_signals=round(socials, 4),
        curve_health=round(curve_health, 4),
        buy_sell_ratio=round(buy_sell_ratio, 4),
        unique_wallets=len(wallets),
        trade_count=len(trades),
        risk_score=round(risk, 2),
    )


def _count_snipers(token: Token, buys: list[Trade]) -> int:
    if not token.created_timestamp:
        return 0
    cutoff = token.created_timestamp + SNIPER_WINDOW_SECONDS
    return len({t.wallet for t in buys if t.timestamp and t.timestamp <= cutoff})


def _wallet_diversity(buys: list[Trade]) -> float:
    """Доля уникальных кошельков среди покупок, со штрафом за концентрацию
    объёма в одном кошельке."""
    if not buys:
        return 0.0
    wallets = [t.wallet for t in buys if t.wallet]
    if not wallets:
        return 0.0
    uniqueness = len(set(wallets)) / len(wallets)

    volume: dict[str, float] = {}
    for t in buys:
        volume[t.wallet] = volume.get(t.wallet, 0.0) + t.sol_amount
    total = sum(volume.values())
    concentration = max(volume.values()) / total if total else 1.0
    return max(0.0, min(1.0, uniqueness * (1.0 - concentration)))


def _social_signals(token: Token) -> float:
    score = 0.0
    if token.twitter:
        score += 0.4
    if token.telegram:
        score += 0.3
    if token.website:
        score += 0.2
    if token.description and len(token.description) > 20:
        score += 0.1
    return min(1.0, score)


def _curve_health(buys: list[Trade]) -> float:
    """Ровный набор кривой лучше рывка: считаем разброс размеров покупок и
    равномерность интервалов между ними."""
    if len(buys) < 3:
        return 0.0
    amounts = [t.sol_amount for t in buys if t.sol_amount > 0]
    if len(amounts) < 3:
        return 0.0

    mean = statistics.fmean(amounts)
    spread = statistics.pstdev(amounts) / mean if mean else 1.0
    size_health = max(0.0, min(1.0, 1.0 - abs(spread - 0.6)))

    stamps = sorted(t.timestamp for t in buys if t.timestamp)
    if len(stamps) >= 3:
        gaps = [b - a for a, b in itertools.pairwise(stamps) if b > a]
        if gaps:
            gap_mean = statistics.fmean(gaps)
            gap_spread = statistics.pstdev(gaps) / gap_mean if gap_mean else 1.0
            pace_health = max(0.0, min(1.0, 1.0 - gap_spread / 2.0))
        else:
            pace_health = 0.0
    else:
        pace_health = 0.0

    return max(0.0, min(1.0, 0.6 * size_health + 0.4 * pace_health))


def _veto_reason(creator_share: float, top5_share: float) -> str | None:
    """Условие, при котором остальные метрики уже не важны."""
    if creator_share >= CREATOR_SHARE_VETO:
        return f"создатель держит {creator_share:.0%} предложения"
    if top5_share >= TOP5_SHARE_VETO:
        return f"топ-5 кошельков держат {top5_share:.0%}"
    return None


def _risk_score(
    *,
    top5_share: float,
    creator_share: float,
    sniper_count: int,
    diversity: float,
    socials: float,
    curve_health: float,
    trade_count: int,
) -> float:
    """0..10, чем выше — тем хуже. Веса подобраны так, чтобы любой одиночный
    красный флаг (создатель с половиной предложения, топ-5 под 80%) сам по
    себе уводил токен за порог отсечки."""
    risk = 0.0
    risk += min(3.0, top5_share * 3.75)          # >80% топ-5 -> 3.0
    risk += min(3.0, creator_share * 10.0)       # >30% у создателя -> 3.0
    risk += min(2.0, sniper_count * 0.25)        # 8 снайперов -> 2.0
    risk += (1.0 - diversity) * 1.5
    risk += (1.0 - curve_health) * 1.0
    risk += (1.0 - socials) * 0.5
    if trade_count < 10:
        risk += 1.0                              # данных мало, доверия меньше
    return max(0.0, min(10.0, risk))
