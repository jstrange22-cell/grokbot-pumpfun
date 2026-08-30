"""Анализатор: три параллельных запроса, разбор ответов и метрики.

Транспорт замокан, в сеть тесты не ходят.
"""

import time

import httpx
import pytest

from src.analyzer import (
    Analyzer,
    apply_offchain_metadata,
    compute_metrics,
    enrich_token,
    fetch_offchain_metadata,
    parse_holder,
    parse_trade,
    resolve_metadata_url,
)
from src.models import Config, Holder, Token, Trade


@pytest.fixture
def config() -> Config:
    cfg = Config()
    cfg.data.api_key = "data-key"
    cfg.filter.max_risk_score = 7.0
    return cfg


def token(**overrides) -> Token:
    base = {
        "mint": "Mint1",
        "name": "Cat",
        "symbol": "CAT",
        "image_uri": "https://i",
        "creator": "Creator1",
        "created_timestamp": time.time() - 600,
    }
    base.update(overrides)
    return Token(**base)


def client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url="http://test", transport=httpx.MockTransport(handler))


# --- сеть -----------------------------------------------------------------


async def test_fetch_hits_three_endpoints(config):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path.endswith("/holders"):
            return httpx.Response(200, json=[{"address": "h1", "share": 0.1}])
        if "/trades/all/" in request.url.path:
            return httpx.Response(200, json=[{"user": "w1", "txType": "buy", "solAmount": 0.5}])
        return httpx.Response(200, json={"description": "кот"})

    analyzer = Analyzer(config, client(handler))
    info, holders, trades = await analyzer.fetch("Mint1")
    assert sorted(seen) == ["/coins/Mint1", "/coins/Mint1/holders", "/trades/all/Mint1"]
    assert info["description"] == "кот"
    assert holders[0].address == "h1"
    assert trades[0].wallet == "w1"


async def test_failed_request_degrades_to_empty(config):
    """Провайдер молчит — метрики считаются по тому, что есть, а не падение."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "нет"})

    analyzer = Analyzer(config, client(handler))
    info, holders, trades = await analyzer.fetch("Mint1")
    assert (info, holders, trades) == ({}, [], [])


async def test_analyze_rejects_token_without_trades(config):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[] if request.url.path != "/coins/Mint1" else {})

    analyzer = Analyzer(config, client(handler))
    metrics = await analyzer.analyze(token())
    ok, reason = analyzer.passes(metrics)
    assert not ok and reason == "no_trade_data"


async def test_empty_rest_trades_pass_when_ws_buyers_sufficient(config):
    """v3 /trades и /holders — 404 без JWT. Монитор уже набрал 5 покупателей."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "/trades/" in request.url.path or request.url.path.endswith("/holders"):
            return httpx.Response(404, json={"error": "no jwt"})
        return httpx.Response(200, json={})

    analyzer = Analyzer(config, client(handler))
    tok = token(unique_buyers=5, sol_in_curve=35.0, market_cap_sol=40.0)
    metrics = await analyzer.analyze(tok)
    ok, reason = analyzer.passes(metrics, tok)
    assert ok, reason
    assert metrics.trade_count >= 5
    assert metrics.curve_liquidity_sol >= config.market.min_curve_liquidity_sol


async def test_empty_rest_trades_without_buyers_still_no_trade_data(config):
    def handler(request: httpx.Request) -> httpx.Response:
        if "/trades/" in request.url.path or request.url.path.endswith("/holders"):
            return httpx.Response(404)
        return httpx.Response(200, json={})

    analyzer = Analyzer(config, client(handler))
    tok = token(unique_buyers=0, sol_in_curve=35.0)
    metrics = await analyzer.analyze(tok)
    ok, reason = analyzer.passes(metrics, tok)
    assert not ok and reason == "no_trade_data"


async def test_empty_rest_coin_uses_token_sol_in_curve(config):
    """Пустая карточка не делает curve_too_thin, если сокет уже видел SOL."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "/trades/" in request.url.path:
            return httpx.Response(200, json=[
                {"user": "w1", "txType": "buy", "solAmount": 0.4},
            ])
        if request.url.path.endswith("/holders"):
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={})

    analyzer = Analyzer(config, client(handler))
    tok = token(unique_buyers=0, sol_in_curve=35.0, market_cap_sol=0.0)
    metrics = await analyzer.analyze(tok)
    ok, reason = analyzer.passes(metrics, tok)
    assert reason != "curve_too_thin"
    assert metrics.curve_liquidity_sol >= config.market.min_curve_liquidity_sol
    assert ok or reason != "curve_too_thin"


async def test_fetch_falls_back_to_v3_when_primary_is_530(config):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "frontend-api.pump.fun":
            return httpx.Response(530, text="error code: 1016")
        if request.url.host == "frontend-api-v3.pump.fun":
            if "/trades/" in request.url.path or request.url.path.endswith("/holders"):
                return httpx.Response(404)
            return httpx.Response(200, json={
                "virtual_sol_reserves": 45_000_000_000,
                "virtual_token_reserves": 715_333_460_666_667,
                "market_cap": 40.0,
                "complete": False,
            })
        return httpx.Response(500)

    config.data.rest_url = "https://frontend-api.pump.fun"
    analyzer = Analyzer(
        config, httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    info, holders, trades = await analyzer.fetch("Mint1")
    assert info["virtual_sol_reserves"] == 45_000_000_000
    assert holders == []
    assert trades == []


async def test_analyzer_does_not_send_data_api_key_as_jwt(config):
    seen_auth: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_auth.append(request.headers.get("authorization", ""))
        return httpx.Response(200, json={"description": "x"})

    analyzer = Analyzer(config, client(handler))
    await analyzer.fetch("Mint1")
    assert seen_auth
    assert all(value == "" for value in seen_auth)


def test_client_outside_context_is_an_error(config):
    with pytest.raises(RuntimeError):
        _ = Analyzer(config).client


# --- разбор ---------------------------------------------------------------


def test_parse_holder_variants():
    assert parse_holder({"address": "a", "amount": 5, "share": 0.2}).share == 0.2
    assert parse_holder({"wallet": "a", "percentage": 25}).share == 0.25
    assert parse_holder({"owner": "a", "balance": 3, "isCreator": True}).is_creator


def test_parse_trade_normalizes_milliseconds():
    trade = parse_trade({"user": "w", "txType": "sell", "solAmount": 1.5,
                         "timestamp": 1_800_000_000_000})
    assert trade.timestamp == 1_800_000_000
    assert not trade.is_buy
    assert trade.sol_amount == 1.5


def test_enrich_fills_only_missing_fields():
    tok = token(description="уже есть")
    enrich_token(tok, {"description": "из сети", "twitter": "https://x.com/c",
                       "virtual_sol_reserves": 30_000_000_000})
    assert tok.description == "уже есть"
    assert tok.twitter == "https://x.com/c"
    assert tok.sol_in_curve == 30.0


def test_resolve_ipfs_uri():
    assert resolve_metadata_url("ipfs://QmCid/meta.json") == "https://ipfs.io/ipfs/QmCid/meta.json"
    assert resolve_metadata_url("https://arweave.net/x") == "https://arweave.net/x"


def test_apply_offchain_metadata_fills_name_and_image():
    tok = token(name=None, symbol=None, image_uri=None)
    apply_offchain_metadata(tok, {
        "name": "From URI",
        "symbol": "URI",
        "image": "https://img/from-uri.png",
        "description": "метаданные с ipfs",
    })
    assert tok.name == "From URI"
    assert tok.symbol == "URI"
    assert tok.image_uri == "https://img/from-uri.png"
    assert tok.has_metadata


async def test_fetch_offchain_metadata_has_no_api_key():
    """Монитор не должен тащить data.api_key на публичный JSON по uri."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization", ""))
        assert request.url.path == "/ipfs/QmMeta"
        return httpx.Response(200, json={"name": "Offchain", "image": "https://i"})

    data = await fetch_offchain_metadata(
        "ipfs://QmMeta",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    assert data["name"] == "Offchain"
    assert seen == [""]


# --- метрики --------------------------------------------------------------


def healthy_trades(count: int = 30) -> list[Trade]:
    start = time.time() - 600
    return [
        Trade(wallet=f"w{i}", is_buy=True, sol_amount=0.3 + i * 0.02,
              timestamp=start + i * 20)
        for i in range(count)
    ]


def test_healthy_token_has_low_risk():
    holders = [Holder(address=f"h{i}", share=0.02) for i in range(20)]
    metrics = compute_metrics(token(twitter="t", telegram="tg", website="w",
                                    description="описание длиннее двадцати символов"),
                              holders, healthy_trades())
    assert metrics.risk_score < 7.0
    assert metrics.unique_wallets == 30
    assert metrics.quality > 0.3


def test_creator_holding_half_supply_is_disqualifying():
    holders = [Holder(address="Creator1", share=0.5, is_creator=True)]
    metrics = compute_metrics(token(), holders, healthy_trades())
    assert metrics.creator_share == 0.5
    assert metrics.risk_score == 10.0


def test_concentrated_top5_is_vetoed():
    holders = [Holder(address=f"h{i}", share=0.18) for i in range(5)]
    metrics = compute_metrics(token(), holders, healthy_trades())
    assert metrics.top5_share == pytest.approx(0.9)
    assert metrics.risk_score == 10.0
    assert metrics.quality == 0.0


def test_veto_boundaries():
    """Вето срабатывает ровно на пороге, а на волосок ниже — обычный счёт."""
    at_threshold = compute_metrics(
        token(), [Holder(address="Creator1", share=0.25, is_creator=True)], healthy_trades()
    )
    assert at_threshold.risk_score == 10.0

    below = compute_metrics(
        token(), [Holder(address="Creator1", share=0.24, is_creator=True)], healthy_trades()
    )
    assert below.risk_score < 10.0


def test_veto_ignores_good_metrics():
    """Хорошая кривая и живые соцсети не выкупают создателя на четверти."""
    holders = [Holder(address="Creator1", share=0.4, is_creator=True)]
    metrics = compute_metrics(
        token(twitter="t", telegram="tg", website="w",
              description="описание длиннее двадцати символов"),
        holders, healthy_trades(),
    )
    assert metrics.curve_health > 0.5
    assert metrics.risk_score == 10.0


def test_snipers_counted_in_first_seconds():
    created = time.time() - 600
    trades = [Trade(wallet=f"s{i}", is_buy=True, sol_amount=1.0, timestamp=created + 2)
              for i in range(8)]
    metrics = compute_metrics(token(created_timestamp=created), [], trades)
    assert metrics.sniper_count == 8


def test_single_wallet_kills_diversity():
    trades = [Trade(wallet="one", is_buy=True, sol_amount=1.0, timestamp=time.time())
              for _ in range(10)]
    metrics = compute_metrics(token(), [], trades)
    assert metrics.wallet_diversity == 0.0


def test_thin_data_penalized():
    few = compute_metrics(token(), [], healthy_trades(count=3))
    many = compute_metrics(token(), [], healthy_trades(count=30))
    assert few.risk_score > many.risk_score


def test_socials_lower_risk():
    bare = compute_metrics(token(), [], healthy_trades())
    social = compute_metrics(
        token(twitter="t", telegram="tg", website="w",
              description="описание длиннее двадцати символов"),
        [], healthy_trades(),
    )
    assert social.risk_score < bare.risk_score
