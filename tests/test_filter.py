"""Базовый фильтр монитора: он режет 94% потока, поэтому граничные
значения важнее всего остального."""

import asyncio
import json
import time

import pytest

from src.models import Config, FilterConfig, Token
from src.monitor import CURVE_COMPLETION_SOL, LaunchMonitor, parse_create_event, passes_filter


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
    """Нет ни имени, ни metadata_uri — отказ. Картинка сама по себе не считается."""
    ok, reason = passes_filter(make_token(name=None, image_uri=None, metadata_uri=None), cfg)
    assert not ok and reason == "no_metadata"


def test_name_or_metadata_uri_counts(cfg):
    ok, reason = passes_filter(make_token(image_uri=None), cfg)
    assert ok and reason == "ok"
    ok, reason = passes_filter(
        make_token(name=None, image_uri=None, metadata_uri="https://ipfs.io/ipfs/x"), cfg
    )
    assert ok and reason == "ok"


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
    token = make_token(name=None, image_uri=None, metadata_uri=None, created_timestamp=time.time())
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
    """Плоская форма docs: name+symbol+uri, без image — не no_metadata."""
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


def test_parse_maps_nested_and_aliased_identity_fields():
    """Поля могут лежать в token/data и называться tokenSymbol / metadataUri."""
    token = parse_create_event({
        "txType": "create",
        "mint": "NestedMint",
        "token": {
            "tokenName": "Nested Cat",
            "tokenSymbol": "NCAT",
            "metadataUri": "https://ipfs.io/ipfs/QmNested",
        },
    })
    assert token is not None
    assert token.name == "Nested Cat"
    assert token.symbol == "NCAT"
    assert token.metadata_uri == "https://ipfs.io/ipfs/QmNested"
    assert token.has_metadata


def test_mint_only_create_leaves_symbol_empty():
    """Так выглядели skip JSONL: mint есть, symbol пустой — parse не нашёл тикер."""
    token = parse_create_event({
        "txType": "create",
        "mint": "MintOnly",
        "traderPublicKey": "Creator1",
        "vSolInBondingCurve": 30.0,
        "timestamp": (time.time() - 300) * 1000,
    })
    assert token is not None
    assert token.symbol is None
    assert token.name is None
    assert token.metadata_uri is None
    assert not token.has_metadata


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
    assert "subscribeTokenTrade" in methods       # подписались на сделки лонча
    assert "unsubscribeTokenTrade" in methods     # и отписались, когда отдали


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
    coins: list[str] = []

    async def fake_fetch(uri: str, request_timeout: float = 10.0, client=None):
        called.append(uri)
        return {}

    async def fake_coin(mint: str, rest_url: str, request_timeout: float = 10.0, client=None):
        coins.append(mint)
        return {}

    import src.monitor as monitor_module

    monkeypatch.setattr(monitor_module, "fetch_offchain_metadata", fake_fetch)
    monkeypatch.setattr(monitor_module, "fetch_public_coin", fake_coin)

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
    assert coins == []


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


async def test_mint_only_create_is_enriched_from_public_coin(monkeypatch):
    """Провод: только mint. Карточка /coins/{mint} без API-ключа даёт имя и uri."""
    created = (time.time() - 300) * 1000
    seen: list[tuple[str, str]] = []

    async def fake_coin(mint: str, rest_url: str, request_timeout: float = 10.0, client=None):
        seen.append((mint, rest_url))
        return {
            "name": "From REST",
            "symbol": "RST",
            "metadata_uri": "https://ipfs.io/ipfs/QmFromRest",
            "image_uri": "https://img/rest.png",
        }

    async def fake_uri(uri: str, request_timeout: float = 10.0, client=None):
        raise AssertionError("при name+uri с REST офчейн не нужен")

    import src.monitor as monitor_module

    monkeypatch.setattr(monitor_module, "fetch_public_coin", fake_coin)
    monkeypatch.setattr(monitor_module, "fetch_offchain_metadata", fake_uri)

    skips: list = []
    mon = make_monitor(skips)
    mon.config.data.api_key = "paid-key-must-not-be-used"
    mon.handle_event({
        "txType": "create", "mint": "A", "traderPublicKey": "creator",
        "timestamp": created,
    })
    await mon.enrich_identity(mon.pending["A"])
    out = None
    for wallet in ("w1", "w2", "w3"):
        out = mon.handle_event({"txType": "buy", "mint": "A", "traderPublicKey": wallet})
    assert out is not None
    assert out.symbol == "RST"
    assert out.name == "From REST"
    assert out.has_metadata
    assert skips == []
    assert seen == [("A", mon.config.data.rest_url)]


async def test_mint_only_stays_no_metadata_if_public_coin_fails(monkeypatch):
    """Нет name/symbol/uri на проводе и карточка молчит — отказ, не пропуск фильтра."""
    created = (time.time() - 300) * 1000

    async def fake_coin(mint: str, rest_url: str, request_timeout: float = 10.0, client=None):
        return {}

    import src.monitor as monitor_module

    monkeypatch.setattr(monitor_module, "fetch_public_coin", fake_coin)

    skips: list = []
    mon = make_monitor(skips)
    mon.handle_event(
        {"txType": "create", "mint": "A", "timestamp": created}
    )
    await mon.enrich_identity(mon.pending["A"])
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
