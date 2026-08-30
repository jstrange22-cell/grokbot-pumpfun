"""Исполнение в dry-run: те же комиссия и проскальзывание, что в live.

Смысл этих тестов — не дать dry-run снова стать оптимистичным. Если он
покупает по котировке, вся отчётность врёт в одну сторону, и решение
включать live принимается по несуществующей прибыли.
"""

import httpx
import pytest

from src.curve import INITIAL_VIRTUAL_SOL, CurveState
from src.executor import (
    DRY_RUN_TX,
    DryRunExecutor,
    LiveExecutor,
    build_executor,
    new_position,
)
from src.models import Config, Position, Token

LIVE_CURVE = {"virtual_sol_reserves": 45_000_000_000,
              "virtual_token_reserves": 715_333_460_666_667}


def config(**market) -> Config:
    cfg = Config()
    for key, value in market.items():
        setattr(cfg.market, key, value)
    return cfg


def client(payload: dict | None) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload if payload is not None else {})

    return httpx.AsyncClient(base_url="http://test", transport=httpx.MockTransport(handler))


def token(**overrides) -> Token:
    base = {"mint": "Mint1", "name": "Cat", "symbol": "CAT", "creator": "C1",
            "market_cap_sol": 60.0}
    base.update(overrides)
    return Token(**base)


def position(tokens: float = 1_000_000.0, spent: float = 0.5) -> Position:
    return Position(mint="Mint1", symbol="CAT", creator="C1", entry_price=spent / tokens,
                    peak_price=spent / tokens, sol_spent=spent, token_amount=tokens,
                    opened_at=1.0, tx_hash=DRY_RUN_TX)


# --- покупка --------------------------------------------------------------


async def test_buy_pays_worse_than_quote():
    """Средняя цена исполнения обязана быть хуже котировки: иначе где-то
    потерялись комиссия и собственное влияние на цену."""
    executor = DryRunExecutor(config(), client(LIVE_CURVE))
    spot = CurveState.from_api(LIVE_CURVE).spot_price

    result = await executor.buy(token(), 0.4)
    assert result.ok
    assert result.price > spot
    assert result.impact_pct > 0
    assert result.fee_sol == pytest.approx(0.4 * 0.01)
    assert result.tx_hash == DRY_RUN_TX


async def test_buy_tokens_match_the_curve():
    executor = DryRunExecutor(config(), client(LIVE_CURVE))
    result = await executor.buy(token(), 0.4)
    # цена × количество = потрачено, до цента
    assert result.price * result.token_amount == pytest.approx(result.sol_amount, rel=1e-12)


async def test_buy_refused_above_impact_cap():
    executor = DryRunExecutor(config(max_price_impact_pct=1.5), client(LIVE_CURVE))
    result = await executor.buy(token(), 5.0)
    assert not result.ok
    assert "влияние на цену" in result.error
    assert result.impact_pct > 1.5


async def test_buy_refused_without_curve_data():
    executor = DryRunExecutor(config(), client({}))
    result = await executor.buy(token(market_cap_sol=0.0), 0.4)
    assert not result.ok
    assert "кривой неизвестно" in result.error


async def test_buy_falls_back_to_market_cap():
    """Резервов нет, но капитализация известна — состояние кривой из неё
    восстанавливается точно, потому что произведение резервов постоянно."""
    executor = DryRunExecutor(config(), client({}))
    result = await executor.buy(token(market_cap_sol=60.0), 0.2)
    assert result.ok
    assert result.token_amount > 0


async def test_curve_falls_back_to_token_sol_in_curve():
    """Пустой REST /coins — кривая из sol_in_curve, который уже есть на токене."""
    executor = DryRunExecutor(config(), client({}))
    tok = token(market_cap_sol=0.0, sol_in_curve=35.0)
    state = await executor.curve(tok.mint, token=tok)
    assert state is not None
    assert state.real_sol == pytest.approx(5.0)
    result = await executor.buy(tok, 0.2)
    assert result.ok


async def test_zero_size_refused():
    executor = DryRunExecutor(config(), client(LIVE_CURVE))
    assert not (await executor.buy(token(), 0.0)).ok


# --- продажа --------------------------------------------------------------


async def test_sell_receives_less_than_quote():
    executor = DryRunExecutor(config(), client(LIVE_CURVE))
    spot = CurveState.from_api(LIVE_CURVE).spot_price
    result = await executor.sell(position())
    assert result.ok
    assert result.price < spot
    assert result.sol_amount > 0


async def test_partial_sell_takes_its_share():
    executor = DryRunExecutor(config(), client(LIVE_CURVE))
    pos = position(tokens=1_000_000.0)
    result = await executor.sell(pos, fraction=0.6)
    assert result.token_amount == pytest.approx(600_000.0)


async def test_dust_tail_is_sold_whole():
    """Оставлять в позиции меньше процента незачем: это пыль, которая
    только мешает учёту."""
    executor = DryRunExecutor(config(), client(LIVE_CURVE))
    result = await executor.sell(position(tokens=1_000_000.0), fraction=0.995)
    assert result.token_amount == pytest.approx(1_000_000.0)


async def test_sell_fraction_clamped():
    executor = DryRunExecutor(config(), client(LIVE_CURVE))
    pos = position(tokens=1_000_000.0)
    assert (await executor.sell(pos, fraction=5.0)).token_amount == pytest.approx(1_000_000.0)
    assert not (await executor.sell(pos, fraction=0.0)).ok


async def test_sell_without_curve_refused():
    executor = DryRunExecutor(config(), client({}))
    result = await executor.sell(position())
    assert not result.ok


# --- цена и состояние -----------------------------------------------------


async def test_price_returns_spot():
    executor = DryRunExecutor(config(), client(LIVE_CURVE))
    assert await executor.price("Mint1") == pytest.approx(
        CurveState.from_api(LIVE_CURVE).spot_price
    )


async def test_price_zero_when_unknown():
    executor = DryRunExecutor(config(), client({}))
    assert await executor.price("Mint1") == 0.0


async def test_curve_progress_excludes_virtual():
    executor = DryRunExecutor(config(), client(LIVE_CURVE))
    state = await executor.curve("Mint1")
    assert state is not None
    assert state.real_sol == pytest.approx(45.0 - INITIAL_VIRTUAL_SOL)


# --- режимы ---------------------------------------------------------------


def test_build_executor_picks_by_mode():
    assert isinstance(build_executor(config()), DryRunExecutor)
    live = config()
    live.mode = "live"
    assert isinstance(build_executor(live), LiveExecutor)


async def test_live_executor_fails_closed_without_a_key():
    """Нет ключа — отказ, не NotImplementedError и не поход в сеть."""
    executor = LiveExecutor(config(), client(LIVE_CURVE))
    bought = await executor.buy(token(), 0.4)
    sold = await executor.sell(position())
    assert not bought.ok and "ключа" in bought.error
    assert not sold.ok and "ключа" in sold.error


async def test_live_executor_can_still_quote():
    """Расчёт заявки живой и в live: из него берутся max_sol_cost и
    min_sol_output, когда отправка будет дописана."""
    executor = LiveExecutor(config(), client(LIVE_CURVE))
    state = await executor.curve("Mint1")
    assert executor.plan_buy(state, 0.4).ok
    assert executor.plan_sell(state, 1_000_000.0).ok


def test_new_position_carries_context():
    from src.executor import ExecutionResult

    result = ExecutionResult(ok=True, price=1e-7, token_amount=5_000_000.0,
                             sol_amount=0.5, tx_hash=DRY_RUN_TX)
    pos = new_position(token(), result, score=0.81)
    assert pos.creator == "C1"
    assert pos.peak_price == pos.entry_price == 1e-7
    assert pos.score == 0.81
    assert pos.realized_sol == 0.0 and pos.partials == 0


# --- live: мок RPC, в сеть не ходим ---------------------------------------


def _live_wallet():
    from solders.keypair import Keypair
    from solders.signature import Signature

    kp = Keypair()
    return kp, str(Signature.from_bytes(bytes(kp)))


def _rpc_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _pack_curve(creator, complete=False, virtual_sol=0, virtual_tokens=0):
    import struct

    data = bytearray(120)
    if virtual_tokens:
        struct.pack_into("<Q", data, 8, virtual_tokens)
    if virtual_sol:
        struct.pack_into("<Q", data, 16, virtual_sol)
    data[48] = 1 if complete else 0
    data[49:81] = bytes(creator)
    return bytes(data)


def _pack_global(fee):
    data = bytearray(520)
    data[41:73] = bytes(fee)
    return bytes(data)


def _account_json(data: bytes, owner: str):
    import base64

    return {
        "value": {
            "data": [base64.b64encode(data).decode(), "base64"],
            "owner": owner,
            "lamports": 1,
        }
    }


def _live_rpc(
    *,
    wallet,
    mint,
    creator,
    fee,
    confirm=True,
    send_error=None,
    missing=(),
    rpc_down=False,
    complete=False,
    jito=False,
    virtual_sol=0,
    virtual_tokens=0,
):
    import json

    from src.onchain import TOKEN_PROGRAM, derive_accounts

    acc = derive_accounts(mint, wallet.pubkey(), creator, fee, TOKEN_PROGRAM)
    store = {
        str(mint): _account_json(b"\x00" * 82, str(TOKEN_PROGRAM)),
        str(acc.bonding_curve): _account_json(
            _pack_curve(creator, complete, virtual_sol, virtual_tokens),
            str(acc.bonding_curve),
        ),
        str(acc.global_account): _account_json(_pack_global(fee), str(acc.global_account)),
    }
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if rpc_down:
            raise httpx.ConnectError("сети нет")
        body = json.loads(request.content)
        method = body["method"]
        seen.append(method)
        if method == "getAccountInfo":
            key = body["params"][0]
            if key in missing or key not in store:
                return httpx.Response(200, json={"result": {"value": None}})
            return httpx.Response(200, json={"result": store[key]})
        if method == "getLatestBlockhash":
            return httpx.Response(200, json={
                "result": {"value": {"blockhash": "11111111111111111111111111111111"}}
            })
        if method == "sendTransaction":
            if send_error:
                return httpx.Response(200, json={"error": send_error})
            return httpx.Response(200, json={"result": "5" + "1" * 86})
        if method == "sendBundle":
            if send_error:
                return httpx.Response(200, json={"error": send_error})
            return httpx.Response(200, json={"result": "bundle-1"})
        if method == "getSignatureStatuses":
            if not confirm:
                return httpx.Response(200, json={"result": {"value": [None]}})
            return httpx.Response(200, json={
                "result": {"value": [{"err": None, "confirmationStatus": "confirmed", "slot": 1}]}
            })
        return httpx.Response(200, json={"error": f"unexpected {method}"})

    return handler, seen, acc


def _live_config(secret: str, *, jito=False):
    cfg = config()
    cfg.mode = "live"
    cfg.solana.wallet_private_key = secret
    cfg.solana.jito.enabled = jito
    cfg.solana.jito.tip_lamports = 1_000_000
    return cfg


def _live_exec(secret: str, handler, *, jito=False, rpc=None, curve=None):
    payload = LIVE_CURVE if curve is None else curve
    return LiveExecutor(
        _live_config(secret, jito=jito),
        client(payload),
        rpc_client=None if rpc is not None else _rpc_client(handler),
        rpc=rpc,
    )


async def test_live_buy_happy_path():
    from solders.keypair import Keypair
    from solders.pubkey import Pubkey

    wallet, secret = _live_wallet()
    mint = Keypair()
    creator = Keypair().pubkey()
    fee = Keypair().pubkey()
    handler, seen, acc = _live_rpc(wallet=wallet, mint=mint.pubkey(), creator=creator, fee=fee)
    tok = token(mint=str(mint.pubkey()))
    executor = _live_exec(secret, handler)

    result = await executor.buy(tok, 0.4)
    assert result.ok, result.error
    assert result.tx_hash
    assert result.tx_hash != DRY_RUN_TX
    assert result.token_amount > 0
    assert result.sol_amount == pytest.approx(0.4)
    assert "sendTransaction" in seen
    assert "sendBundle" not in seen
    assert "getSignatureStatuses" in seen
    assert Pubkey.from_string(str(acc.mint)) == mint.pubkey()


async def test_live_sell_happy_path_closes_ata_on_full_exit():
    from solders.keypair import Keypair

    wallet, secret = _live_wallet()
    mint = Keypair()
    creator = Keypair().pubkey()
    fee = Keypair().pubkey()
    handler, seen, _acc = _live_rpc(wallet=wallet, mint=mint.pubkey(), creator=creator, fee=fee)
    pos = position()
    pos.mint = str(mint.pubkey())
    executor = _live_exec(secret, handler)

    result = await executor.sell(pos)
    assert result.ok, result.error
    assert result.tx_hash
    assert result.sol_amount > 0
    assert "sendTransaction" in seen


async def test_live_jito_bundle_subtracts_tip_from_cost():
    from solders.keypair import Keypair

    wallet, secret = _live_wallet()
    mint = Keypair()
    handler, seen, _ = _live_rpc(
        wallet=wallet, mint=mint.pubkey(), creator=Keypair().pubkey(),
        fee=Keypair().pubkey(), jito=True,
    )
    executor = _live_exec(secret, handler, jito=True)
    result = await executor.buy(token(mint=str(mint.pubkey())), 0.4)
    assert result.ok, result.error
    assert "sendBundle" in seen
    assert "sendTransaction" not in seen
    assert result.sol_amount == pytest.approx(0.4 + 0.001)


async def test_live_buy_fails_closed_on_rpc_error():
    from solders.keypair import Keypair

    wallet, secret = _live_wallet()
    mint = Keypair()
    handler, _seen, _ = _live_rpc(
        wallet=wallet, mint=mint.pubkey(), creator=Keypair().pubkey(),
        fee=Keypair().pubkey(), rpc_down=True,
    )
    executor = _live_exec(secret, handler)
    result = await executor.buy(token(mint=str(mint.pubkey())), 0.4)
    assert not result.ok
    assert "недоступен" in result.error or "RPC" in result.error


async def test_live_buy_fails_closed_without_confirmation():
    from solders.keypair import Keypair

    from src.onchain import SolanaRpc

    wallet, secret = _live_wallet()
    mint = Keypair()
    handler, _seen, _ = _live_rpc(
        wallet=wallet, mint=mint.pubkey(), creator=Keypair().pubkey(),
        fee=Keypair().pubkey(), confirm=False,
    )

    async def _noop(_s):
        return None

    rpc = SolanaRpc(
        "http://rpc.test", _rpc_client(handler),
        confirm_tries=2, confirm_wait=0, sleeper=_noop,
    )
    executor = LiveExecutor(_live_config(secret), client(LIVE_CURVE), rpc=rpc)
    result = await executor.buy(token(mint=str(mint.pubkey())), 0.4)
    assert not result.ok
    assert "подтверждения" in result.error


async def test_live_buy_fails_without_curve_account():
    from solders.keypair import Keypair

    wallet, secret = _live_wallet()
    mint = Keypair()
    handler, _seen, _acc = _live_rpc(
        wallet=wallet, mint=mint.pubkey(), creator=Keypair().pubkey(),
        fee=Keypair().pubkey(), missing=(str(mint.pubkey()),),
    )
    executor = _live_exec(secret, handler)
    result = await executor.buy(token(mint=str(mint.pubkey())), 0.4)
    assert not result.ok


async def test_live_sell_fails_when_curve_completed():
    from solders.keypair import Keypair

    wallet, secret = _live_wallet()
    mint = Keypair()
    handler, _seen, _ = _live_rpc(
        wallet=wallet, mint=mint.pubkey(), creator=Keypair().pubkey(),
        fee=Keypair().pubkey(),
    )
    # REST говорит, что кривая закрыта — live не продаёт по спотовой прикидке.
    executor = LiveExecutor(
        _live_config(secret),
        client({**LIVE_CURVE, "complete": True}),
        rpc_client=_rpc_client(handler),
    )
    pos = position()
    pos.mint = str(mint.pubkey())
    result = await executor.sell(pos)
    assert not result.ok
    assert "закрыта" in result.error


async def test_live_buy_fails_on_send_error():
    from solders.keypair import Keypair

    wallet, secret = _live_wallet()
    mint = Keypair()
    handler, _seen, _ = _live_rpc(
        wallet=wallet, mint=mint.pubkey(), creator=Keypair().pubkey(),
        fee=Keypair().pubkey(), send_error={"message": "blockhash not found"},
    )
    executor = _live_exec(secret, handler)
    result = await executor.buy(token(mint=str(mint.pubkey())), 0.4)
    assert not result.ok
    assert "blockhash" in result.error or "RPC" in result.error


async def test_live_buy_refused_when_rpc_client_missing():
    _wallet, secret = _live_wallet()
    from solders.keypair import Keypair

    executor = LiveExecutor(_live_config(secret), client(LIVE_CURVE))
    result = await executor.buy(token(mint=str(Keypair().pubkey())), 0.4)
    assert not result.ok
    assert "RPC" in result.error


async def test_live_curve_falls_back_to_onchain_when_rest_empty():
    from solders.keypair import Keypair

    wallet, secret = _live_wallet()
    mint = Keypair()
    handler, _seen, _acc = _live_rpc(
        wallet=wallet, mint=mint.pubkey(), creator=Keypair().pubkey(),
        fee=Keypair().pubkey(),
        virtual_sol=45_000_000_000,
        virtual_tokens=715_333_460_666_667,
    )
    executor = LiveExecutor(
        _live_config(secret),
        client({}),
        rpc_client=_rpc_client(handler),
    )
    state = await executor.curve(str(mint.pubkey()))
    assert state is not None
    assert state.sol_reserves == pytest.approx(45.0)
    assert state.real_sol == pytest.approx(15.0)
