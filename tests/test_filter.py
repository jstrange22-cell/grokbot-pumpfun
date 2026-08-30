"""Базовый фильтр монитора: он режет 94% потока, поэтому граничные
значения важнее всего остального."""

import asyncio
import json
import time

import httpx
import pytest

from src.models import Config, FilterConfig, Token
from src.monitor import (
    COIN_CARD_TRACTION_SOL,
    CURVE_COMPLETION_SOL,
    LaunchMonitor,
    coin_card_has_traction,
    parse_create_event,
    passes_filter,
)


@pytest.fixture
def cfg() -> FilterConfig:
    return FilterConfig(
        min_unique_buyers=5,
        max_curve_progress=0.40,
        require_metadata=True,
        min_age_seconds=120.0,
    )


def make_token(**overrides) -> Token:
    base = {
        "mint": "Mint111",
        "name": "Doge Killer",
        "image_uri": "https://img/1.png",
        "created_timestamp": time.time() - 300,
        "unique_buyers": 10,
        "curve_progress": 0.15,
    }
    base.update(overrides)
    return Token(**base)


def test_healthy_token_passes(cfg):
    ok, reason = passes_filter(make_token(), cfg)
    assert ok and reason == "ok"


def test_missing_metadata_rejected(cfg):
    ok, reason = passes_filter(make_token(image_uri=None), cfg)
    assert not ok and reason == "no_metadata"

    ok, reason = passes_filter(make_token(name=None), cfg)
    assert not ok and reason == "no_metadata"


def test_metadata_ignored_when_not_required(cfg):
    cfg.require_metadata = False
    ok, _ = passes_filter(make_token(image_uri=None), cfg)
    assert ok


def test_too_young_rejected(cfg):
    ok, reason = passes_filter(make_token(created_timestamp=time.time() - 60), cfg)
    assert not ok and reason == "too_young"


def test_age_boundary_is_inclusive(cfg):
    """Ровно 120 секунд — уже проходит, 119.9 — нет."""
    ok, _ = passes_filter(make_token(created_timestamp=time.time() - 120.5), cfg)
    assert ok
    ok, reason = passes_filter(make_token(created_timestamp=time.time() - 119.0), cfg)
    assert not ok and reason == "too_young"


def test_buyers_boundary(cfg):
    ok, _ = passes_filter(make_token(unique_buyers=5), cfg)
    assert ok
    ok, reason = passes_filter(make_token(unique_buyers=4), cfg)
    assert not ok and reason == "few_buyers"


def test_curve_boundary(cfg):
    ok, _ = passes_filter(make_token(curve_progress=0.399), cfg)
    assert ok
    ok, reason = passes_filter(make_token(curve_progress=0.40), cfg)
    assert not ok and reason == "curve_too_full"


def test_terminal_reasons_win_over_temporary(cfg):
    """Порядок причин важен: безнадёжный токен не должен висеть в буфере
    как 'too_young' или 'few_buyers' — иначе он там до протухания."""
    token = make_token(image_uri=None, created_timestamp=time.time())
    ok, reason = passes_filter(token, cfg)
    assert not ok and reason == "no_metadata"

    token = make_token(curve_progress=0.9, unique_buyers=0, created_timestamp=time.time())
    ok, reason = passes_filter(token, cfg)
    assert not ok and reason == "curve_too_full"


# --- разбор события сокета ------------------------------------------------


def pumpportal_create(mint: str = "Mint111", **overrides) -> dict:
    """Живая форма subscribeNewToken: имя, тикер, uri — без отдельного image."""
    payload = {
        "txType": "create",
        "mint": mint,
        "name": "Cat Coin",
        "symbol": "CAT",
        "uri": "https://ipfs.io/ipfs/QmMetaExample",
        "traderPublicKey": "Creator1",
        "vSolInBondingCurve": 30.0,
        "marketCapSol": 5.0,
        "timestamp": (time.time() - 300) * 1000,
    }
    payload.update(overrides)
    return payload


def test_parse_create_event():
    token = parse_create_event(
        {
            "txType": "create",
            "mint": "Abc",
            "name": "Cat",
            "symbol": "CAT",
            "image": "https://img",
            "traderPublicKey": "Creator1",
            "vSolInBondingCurve": 38.5,
            "marketCapSol": 30.0,
        }
    )
    assert token is not None
    assert token.mint == "Abc"
    # 30 SOL в резерве виртуальные: реально собрано 8.5
    assert token.curve_progress == pytest.approx(8.5 / CURVE_COMPLETION_SOL)


def test_pumpportal_create_without_image_is_not_no_metadata(cfg):
    """Прод 2026-08-30: 163/163 лончей отсеялись как no_metadata, потому что
    PumpPortal шлёт name+symbol+uri и не кладёт отдельное поле image."""
    token = parse_create_event(pumpportal_create())
    assert token is not None
    assert token.image_uri is None
    assert token.metadata_uri == "https://ipfs.io/ipfs/QmMetaExample"
    assert token.has_metadata
    token.unique_buyers = 10
    ok, reason = passes_filter(token, cfg)
    assert ok and reason == "ok"


def test_pumpportal_launches_are_not_all_skipped_as_no_metadata():
    """Тот же инцидент через буфер монитора: require_metadata остаётся
    включённым, но пачка лончей без image не должна осыпаться целиком."""
    skips: list = []
    config = Config()
    config.filter = FilterConfig(
        min_unique_buyers=3,
        min_age_seconds=120.0,
        require_metadata=True,
    )
    mon = LaunchMonitor(config, on_skip=lambda t, r: skips.append((t.mint, r)))
    promoted = 0
    for index in range(20):
        mint = f"Mint{index}"
        mon.handle_event(pumpportal_create(mint))
        out = None
        for wallet in ("w1", "w2", "w3"):
            out = mon.handle_event({"txType": "buy", "mint": mint, "traderPublicKey": wallet})
        if out is not None:
            promoted += 1
    assert promoted == 20
    assert skips == []


def test_fresh_launch_has_zero_progress():
    """Резерв новорождённой кривой — 30 виртуальных SOL. Если считать их
    прогрессом, каждый лонч рождается с 35% и фильтр по кривой становится
    в разы строже задуманного."""
    token = parse_create_event({
        "txType": "create", "mint": "New", "name": "n", "image": "i",
        "vSolInBondingCurve": 30.0,
    })
    assert token is not None
    assert token.curve_progress == 0.0


def test_parse_ignores_trades():
    assert parse_create_event({"txType": "buy", "mint": "Abc"}) is None
    assert parse_create_event({"txType": "create"}) is None


# --- буфер монитора -------------------------------------------------------


def make_monitor(skips: list) -> LaunchMonitor:
    config = Config()
    config.filter = FilterConfig(min_unique_buyers=3, min_age_seconds=120.0)
    return LaunchMonitor(config, on_skip=lambda t, r: skips.append((t.mint, r)))


def test_new_launch_is_buffered_not_emitted():
    skips: list = []
    mon = make_monitor(skips)
    assert mon.handle_event({"txType": "create", "mint": "A", "name": "n", "image": "i"}) is None
    assert "A" in mon.pending


def test_token_emitted_once_it_matures():
    skips: list = []
    mon = make_monitor(skips)
    mon.handle_event(
        {
            "txType": "create",
            "mint": "A",
            "name": "n",
            "image": "i",
            "traderPublicKey": "creator",
            "timestamp": (time.time() - 300) * 1000,
        }
    )
    for wallet in ("w1", "w2", "w3"):
        out = mon.handle_event({"txType": "buy", "mint": "A", "traderPublicKey": wallet})
    assert out is not None  # отдан ровно на третьем уникальном покупателе
    assert out.mint == "A"
    assert "A" not in mon.pending
    assert skips == []


def test_same_wallet_does_not_inflate_buyer_count():
    skips: list = []
    mon = make_monitor(skips)
    mon.handle_event(
        {"txType": "create", "mint": "A", "name": "n", "image": "i",
         "timestamp": (time.time() - 300) * 1000}
    )
    for _ in range(10):
        out = mon.handle_event({"txType": "buy", "mint": "A", "traderPublicKey": "same"})
    assert out is None
    assert mon.pending["A"].unique_buyers == 1


def test_curve_overflow_skips_permanently():
    skips: list = []
    mon = make_monitor(skips)
    mon.handle_event(
        {"txType": "create", "mint": "A", "name": "n", "image": "i",
         "timestamp": (time.time() - 300) * 1000}
    )
    mon.handle_event(
        {"txType": "buy", "mint": "A", "traderPublicKey": "w1",
         "vSolInBondingCurve": CURVE_COMPLETION_SOL * 0.9}
    )
    assert "A" not in mon.pending
    assert skips == [("A", "curve_too_full")]


def test_sweep_drops_stale_launches():
    skips: list = []
    mon = make_monitor(skips)
    mon.handle_event(
        {"txType": "create", "mint": "A", "name": "n", "image": "i",
         "timestamp": (time.time() - 5000) * 1000}
    )
    ready = mon.sweep()
    assert ready == []
    assert skips == [("A", "stale_no_traction")]
    assert mon.pending == {}


def test_sweep_emits_matured_token():
    skips: list = []
    mon = make_monitor(skips)
    mon.handle_event(
        {"txType": "create", "mint": "A", "name": "n", "image": "i",
         "timestamp": (time.time() - 300) * 1000}
    )
    for wallet in ("w1", "w2", "w3", "w4"):
        mon.handle_event({"txType": "buy", "mint": "A", "traderPublicKey": wallet})
    # уже отдан на последней покупке, повторно не отдаётся
    assert mon.sweep() == []


# --- память монитора ------------------------------------------------------


def test_seen_set_forgets_oldest():
    """Процесс живёт сутками: список виденных минтов не должен расти вечно."""
    from src.monitor import SeenSet

    seen = SeenSet(maxlen=3)
    for mint in ("A", "B", "C", "D"):
        seen.add(mint)
    assert len(seen) == 3
    assert "A" not in seen
    assert "D" in seen


def test_pending_buffer_is_bounded(monkeypatch):
    import src.monitor as monitor_module

    monkeypatch.setattr(monitor_module, "MAX_PENDING", 3)
    skips: list = []
    mon = make_monitor(skips)
    for index in range(5):
        mon.handle_event({
            "txType": "create", "mint": f"M{index}", "name": "n", "image": "i",
            "timestamp": (time.time() - 1000 + index) * 1000,
        })
    assert len(mon.pending) <= 3
    assert skips and skips[0][1] == "buffer_overflow"


# --- поток из сокета ------------------------------------------------------


class FakeWebSocket:
    """Сокет, отдающий заготовленные сообщения, потом замолкающий."""

    def __init__(self, messages: list[dict]) -> None:
        self.messages = [json.dumps(m) for m in messages]
        self.sent: list[dict] = []

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    async def recv(self) -> str:
        if self.messages:
            return self.messages.pop(0)
        await asyncio.sleep(3600)          # дальше тишина
        raise AssertionError("недостижимо")

    async def __aenter__(self) -> "FakeWebSocket":
        return self

    async def __aexit__(self, *exc) -> bool:
        return False


def patch_socket(monkeypatch, sockets: list) -> list:
    """Подменить websockets.connect последовательностью сокетов."""
    import src.monitor as monitor_module

    opened: list = []

    def connect(url, **kwargs):
        opened.append(url)
        socket = sockets.pop(0)
        if isinstance(socket, Exception):
            raise socket
        return socket

    monkeypatch.setattr(monitor_module.websockets, "connect", connect)
    return opened


async def test_stream_yields_matured_token(monkeypatch):
    created = (time.time() - 300) * 1000
    ws = FakeWebSocket([
        {"txType": "create", "mint": "A", "name": "Cat", "image": "i", "timestamp": created},
        {"txType": "buy", "mint": "A", "traderPublicKey": "w1"},
        {"txType": "buy", "mint": "A", "traderPublicKey": "w2"},
        {"txType": "buy", "mint": "A", "traderPublicKey": "w3"},
    ])
    patch_socket(monkeypatch, [ws])

    mon = make_monitor([])
    stream = mon.stream()
    token = await asyncio.wait_for(stream.__anext__(), timeout=2)
    await stream.aclose()

    assert token.mint == "A"
    methods = [m["method"] for m in ws.sent]
    assert methods[0] == "subscribeNewToken"
    assert "subscribeTokenTrade" not in methods    # платный фид выключен


async def test_stream_reconnects_after_drop(monkeypatch):
    created = (time.time() - 300) * 1000
    good = FakeWebSocket([
        {"txType": "create", "mint": "B", "name": "Cat", "image": "i", "timestamp": created},
        {"txType": "buy", "mint": "B", "traderPublicKey": "w1"},
        {"txType": "buy", "mint": "B", "traderPublicKey": "w2"},
        {"txType": "buy", "mint": "B", "traderPublicKey": "w3"},
    ])
    opened = patch_socket(monkeypatch, [OSError("сокет отвалился"), good])
    real_sleep = asyncio.sleep
    # пауза перед переподключением нужна в проде, но не в тесте
    monkeypatch.setattr(asyncio, "sleep", lambda delay, *a, **k: real_sleep(0))

    mon = make_monitor([])
    stream = mon.stream()
    token = await asyncio.wait_for(stream.__anext__(), timeout=2)
    await stream.aclose()

    assert token.mint == "B"
    assert len(opened) == 2          # первый коннект упал, второй сработал


async def test_stream_does_not_fetch_when_name_is_present(monkeypatch):
    """Обычный PumpPortal-лонч (есть name+uri) не ходит в сеть за JSON."""
    created = (time.time() - 300) * 1000
    called: list[str] = []

    async def fake_fetch(uri: str, request_timeout: float = 10.0, client=None):
        called.append(uri)
        return {}

    import src.monitor as monitor_module

    monkeypatch.setattr(monitor_module, "fetch_offchain_metadata", fake_fetch)

    ws = FakeWebSocket([
        pumpportal_create("A", timestamp=created),
        {"txType": "buy", "mint": "A", "traderPublicKey": "w1"},
        {"txType": "buy", "mint": "A", "traderPublicKey": "w2"},
        {"txType": "buy", "mint": "A", "traderPublicKey": "w3"},
    ])
    patch_socket(monkeypatch, [ws])

    mon = make_monitor([])
    stream = mon.stream()
    token = await asyncio.wait_for(stream.__anext__(), timeout=2)
    await stream.aclose()

    assert token.mint == "A"
    assert token.name == "Cat Coin"
    assert called == []


async def test_stream_enriches_nameless_create_from_uri(monkeypatch):
    """Сокет без name, но с uri: монитор читает JSON по uri, не data.api_key."""
    created = (time.time() - 300) * 1000
    seen_uris: list[str] = []

    async def fake_fetch(uri: str, request_timeout: float = 10.0, client=None):
        seen_uris.append(uri)
        return {"name": "From URI", "symbol": "URI", "image": "https://img/from-uri.png"}

    import src.monitor as monitor_module

    monkeypatch.setattr(monitor_module, "fetch_offchain_metadata", fake_fetch)

    ws = FakeWebSocket([
        {"txType": "create", "mint": "A", "uri": "https://ipfs.io/ipfs/QmOnlyUri",
         "timestamp": created},
        {"txType": "buy", "mint": "A", "traderPublicKey": "w1"},
        {"txType": "buy", "mint": "A", "traderPublicKey": "w2"},
        {"txType": "buy", "mint": "A", "traderPublicKey": "w3"},
    ])
    patch_socket(monkeypatch, [ws])

    mon = make_monitor([])
    stream = mon.stream()
    token = await asyncio.wait_for(stream.__anext__(), timeout=2)
    await stream.aclose()

    assert token.mint == "A"
    assert token.name == "From URI"
    assert token.image_uri == "https://img/from-uri.png"
    assert seen_uris == ["https://ipfs.io/ipfs/QmOnlyUri"]


async def test_nameless_create_stays_no_metadata_if_uri_fetch_fails(monkeypatch):
    """Нет имени и JSON по uri не открылся — отказ, а не пропуск фильтра."""
    created = (time.time() - 300) * 1000

    async def fake_fetch(uri: str, request_timeout: float = 10.0, client=None):
        return {}

    import src.monitor as monitor_module

    monkeypatch.setattr(monitor_module, "fetch_offchain_metadata", fake_fetch)

    skips: list = []
    mon = make_monitor(skips)
    mon.handle_event(
        {"txType": "create", "mint": "A", "uri": "https://ipfs.io/ipfs/QmMissing",
         "timestamp": created}
    )
    await mon.enrich_from_uri(mon.pending["A"])
    for wallet in ("w1", "w2", "w3"):
        mon.handle_event({"txType": "buy", "mint": "A", "traderPublicKey": wallet})
    assert skips == [("A", "no_metadata")]


async def test_stream_survives_broken_json(monkeypatch):
    created = (time.time() - 300) * 1000

    class NoisyWebSocket(FakeWebSocket):
        async def recv(self) -> str:
            if self.messages:
                return self.messages.pop(0)
            await asyncio.sleep(3600)
            raise AssertionError("недостижимо")

    ws = NoisyWebSocket([
        {"txType": "create", "mint": "C", "name": "Cat", "image": "i", "timestamp": created},
        {"txType": "buy", "mint": "C", "traderPublicKey": "w1"},
        {"txType": "buy", "mint": "C", "traderPublicKey": "w2"},
        {"txType": "buy", "mint": "C", "traderPublicKey": "w3"},
    ])
    ws.messages.insert(1, "{битый json")
    patch_socket(monkeypatch, [ws])

    mon = make_monitor([])
    stream = mon.stream()
    token = await asyncio.wait_for(stream.__anext__(), timeout=2)
    await stream.aclose()
    assert token.mint == "C"


def test_data_socket_url_appends_real_key_only():
    from src.monitor import data_socket_url

    config = Config()
    config.data.ws_url = "wss://pumpportal.fun/api/data"
    config.data.api_key = "YOUR-DATA-PROVIDER-KEY"
    assert data_socket_url(config) == "wss://pumpportal.fun/api/data"

    config.data.api_key = "pp-live-key-123"
    assert data_socket_url(config) == "wss://pumpportal.fun/api/data?api-key=pp-live-key-123"


def test_monitor_detail_names_buyers_age_curve():
    from src.monitor import monitor_detail

    token = make_token(unique_buyers=3, curve_progress=0.12,
                       created_timestamp=time.time() - 180)
    detail = monitor_detail(token)
    assert "buyers=3" in detail
    assert "age=180s" in detail or "age=181s" in detail
    assert "curve=0.120" in detail


async def test_trade_subscribe_is_not_sent():
    """Платный subscribeTokenTrade на ATLAS снимал 0.01 SOL / ~2 мин. Не шлём."""
    skips: list = []
    mon = make_monitor(skips)
    created = (time.time() - 300) * 1000
    mon.handle_event(pumpportal_create("Old", timestamp=created))
    mon.handle_event(pumpportal_create("New", timestamp=created))

    sent: list[dict] = []

    class Recorder:
        async def send(self, raw: str) -> None:
            sent.append(json.loads(raw))

    await mon._sync_trade_subs(Recorder())
    assert sent == []


def test_buy_alias_fields_count_and_exclude_creator():
    skips: list = []
    mon = make_monitor(skips)
    mon.handle_event(pumpportal_create("A", timestamp=(time.time() - 300) * 1000))
    mon.handle_event({"txType": "buy", "mint": "A", "traderPublicKey": "Creator1"})
    mon.handle_event({"txType": "Buy", "ca": "A", "user": "w1"})
    mon.handle_event({"tx_type": "buy", "mintAddress": "A", "wallet": "w2", "isBuy": True})
    out = mon.handle_event({"is_buy": True, "mint": "A", "trader": "w3"})
    assert out is not None
    assert out.unique_buyers == 3


async def test_rest_snapshot_promotes_when_socket_trades_never_arrive():
    """Прод 2026-08-30: 10047/10580 skip = stale_no_traction, few_buyers=0.
    Сокет create живой, buy-событий нет. REST должен добрать покупателей."""
    skips: list = []
    config = Config()
    config.filter = FilterConfig(
        min_unique_buyers=5,
        min_age_seconds=120.0,
        require_metadata=True,
        max_curve_progress=0.40,
    )

    async def fake_rest(mint: str):
        trades = [{"user": f"w{i}", "txType": "buy"} for i in range(5)]
        return trades, {"virtual_sol_reserves": 32_000_000_000, "complete": False}

    mon = LaunchMonitor(config, on_skip=lambda t, r: skips.append((t.mint, r)),
                        rest_fetch=fake_rest)
    mon.handle_event(pumpportal_create("A", timestamp=(time.time() - 180) * 1000))
    assert mon.handle_event({"txType": "sell", "mint": "A", "wallet": "x"}) is None
    assert mon.pending["A"].unique_buyers == 0

    await mon.refresh_from_rest()
    ready = mon.sweep()
    assert [t.mint for t in ready] == ["A"]
    assert ready[0].unique_buyers == 5
    assert ready[0].has_metadata
    assert ready[0].image_uri is None
    assert skips == []


async def test_rest_does_not_promote_nameless_or_full_curve():
    skips: list = []
    config = Config()
    config.filter = FilterConfig(min_unique_buyers=5, min_age_seconds=120.0)

    async def fake_rest(mint: str):
        return [{"user": f"w{i}", "txType": "buy"} for i in range(8)], {"complete": True}

    mon = LaunchMonitor(config, on_skip=lambda t, r: skips.append((t.mint, r)),
                        rest_fetch=fake_rest)
    mon.handle_event(pumpportal_create("A", timestamp=(time.time() - 180) * 1000))
    await mon.refresh_from_rest()
    assert mon.sweep() == []
    assert skips == [("A", "curve_too_full")]


def test_atlas_filter_promotes_pumpportal_create_without_image():
    """Живая форма create + 5 чужих покупок. Картинки нет — это не no_metadata."""
    config = Config()
    config.filter = FilterConfig(
        min_unique_buyers=5,
        max_curve_progress=0.40,
        require_metadata=True,
        min_age_seconds=120.0,
    )
    mon = LaunchMonitor(config)
    mon.handle_event(pumpportal_create("A", timestamp=(time.time() - 200) * 1000))
    out = None
    for wallet in ("w1", "w2", "w3", "w4", "w5"):
        out = mon.handle_event({"txType": "buy", "mint": "A", "traderPublicKey": wallet})
    assert out is not None
    assert out.unique_buyers == 5
    assert out.image_uri is None
    assert out.has_metadata


def test_coin_card_has_traction_from_last_trade_or_real_sol():
    assert coin_card_has_traction({
        "last_trade_timestamp": 1_756_560_000,
        "real_sol_reserves": 0,
    })
    assert coin_card_has_traction({
        "real_sol_reserves": 350_000_000,  # 0.35 SOL
    })
    # ровно 0.3 SOL — ещё не traction
    assert not coin_card_has_traction({
        "real_sol_reserves": int(COIN_CARD_TRACTION_SOL * 1_000_000_000),
    })
    assert not coin_card_has_traction({
        "last_trade_timestamp": None,
        "real_sol_reserves": 0,
        "virtual_sol_reserves": 30_000_000_000,
    })
    assert not coin_card_has_traction({})
    assert not coin_card_has_traction(None)


def _atlas_monitor(skips: list, rest_fetch=None) -> LaunchMonitor:
    config = Config()
    config.data.api_key = "pp-must-not-appear"
    config.data.rest_url = "https://frontend-api-v3.pump.fun"
    config.filter = FilterConfig(
        min_unique_buyers=5,
        min_age_seconds=120.0,
        require_metadata=True,
        max_curve_progress=0.40,
    )
    return LaunchMonitor(
        config,
        on_skip=lambda t, r: skips.append((t.mint, r)),
        rest_fetch=rest_fetch,
    )


async def test_trades_404_coin_card_with_last_trade_promotes(monkeypatch):
    """v3 /trades 404 без JWT. Карточка с last_trade — достаточно для промоута."""
    seen_auth: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_auth.append(request.headers.get("authorization", ""))
        if "/trades/all/" in request.url.path:
            return httpx.Response(404, json={"error": "no jwt"})
        if "/coins/" in request.url.path:
            return httpx.Response(200, json={
                "name": "Cat Coin",
                "symbol": "CAT",
                "last_trade_timestamp": 1_756_560_000,
                "real_sol_reserves": 400_000_000,
                "virtual_sol_reserves": 30_400_000_000,
                "market_cap": 12.0,
                "reply_count": 3,
            })
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)

    class PatchedClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    monkeypatch.setattr("src.monitor.httpx.AsyncClient", PatchedClient)

    skips: list = []
    mon = _atlas_monitor(skips)
    mon.handle_event(pumpportal_create("A", timestamp=(time.time() - 180) * 1000))
    assert mon.pending["A"].unique_buyers == 0

    await mon.refresh_from_rest()
    ready = mon.sweep()
    assert [t.mint for t in ready] == ["A"]
    assert ready[0].unique_buyers == 5
    assert skips == []
    assert seen_auth
    assert all(value == "" for value in seen_auth)


async def test_coin_card_real_sol_alone_promotes_when_trades_empty():
    skips: list = []

    async def fake_rest(mint: str):
        return [], {
            "real_sol_reserves": 350_000_000,
            "virtual_sol_reserves": 30_350_000_000,
            "market_cap": 11.0,
        }

    mon = _atlas_monitor(skips, rest_fetch=fake_rest)
    mon.handle_event(pumpportal_create("A", timestamp=(time.time() - 180) * 1000))
    await mon.refresh_from_rest()
    ready = mon.sweep()
    assert [t.mint for t in ready] == ["A"]
    assert ready[0].unique_buyers == 5
    assert skips == []


async def test_brand_new_coin_card_stays_few_buyers(monkeypatch):
    """Нет last_trade и ~0 real SOL — newborn, unique_buyers не поднимаем."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "/trades/all/" in request.url.path:
            return httpx.Response(404)
        if "/coins/" in request.url.path:
            return httpx.Response(200, json={
                "name": "Newborn",
                "symbol": "NEW",
                "last_trade_timestamp": None,
                "real_sol_reserves": 0,
                "virtual_sol_reserves": 30_000_000_000,
                "market_cap": 4.0,
                "reply_count": 0,
            })
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)

    class PatchedClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    monkeypatch.setattr("src.monitor.httpx.AsyncClient", PatchedClient)

    skips: list = []
    mon = _atlas_monitor(skips)
    mon.handle_event(pumpportal_create("A", timestamp=(time.time() - 180) * 1000))
    await mon.refresh_from_rest()
    assert mon.pending["A"].unique_buyers == 0
    assert mon.sweep() == []
    ok, reason = passes_filter(mon.pending["A"], mon.filter)
    assert not ok and reason == "few_buyers"
    assert skips == []
