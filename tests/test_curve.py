"""Математика кривой. Здесь проверяются тождества, а не примеры.

Если этот модуль ошибается, ошибается всё: цена входа, PnL, подбор весов
по логу и решение включать live. Поэтому тесты написаны как инварианты
постоянного произведения, а не как «на таких числах получилось столько».
"""

import math

import pytest

from src.curve import (
    CURVE_COMPLETION_SOL,
    DEFAULT_TRADE_FEE_PCT,
    INITIAL_VIRTUAL_SOL,
    TOTAL_SUPPLY,
    CurveState,
    buy_quote,
    max_sol_for_impact,
    price_from_reserves,
    progress_from_sol,
    round_trip_cost_pct,
    sanity_check,
    sell_quote,
    state_from_any,
)


def curve(real_sol: float = 15.0) -> CurveState:
    """Кривая, собравшая `real_sol` настоящих SOL. Произведение сохранено."""
    fresh = CurveState()
    sol = INITIAL_VIRTUAL_SOL + real_sol
    return CurveState(sol_reserves=sol, token_reserves=fresh.k / sol)


# --- инварианты -----------------------------------------------------------


@pytest.mark.parametrize("real_sol", [0.0, 1.0, 15.0, 60.0])
@pytest.mark.parametrize("sol_in", [0.01, 0.1, 0.5, 2.0])
def test_product_is_preserved_on_buy(real_sol, sol_in):
    state = curve(real_sol)
    quote = buy_quote(state, sol_in, fee_pct=0.0)
    assert quote.ok
    assert quote.state_after.k == pytest.approx(state.k, rel=1e-12)


@pytest.mark.parametrize("real_sol", [0.0, 15.0, 60.0])
def test_product_is_preserved_on_sell(real_sol):
    state = curve(real_sol)
    bought = buy_quote(state, 0.5, fee_pct=0.0)
    sold = sell_quote(bought.state_after, bought.tokens, fee_pct=0.0)
    assert sold.ok
    assert sold.state_after.k == pytest.approx(state.k, rel=1e-12)


def test_round_trip_without_fees_returns_everything():
    """На постоянном произведении проскальзывание входа компенсируется
    выходом: без комиссии круг обязан быть бесплатным."""
    state = curve()
    assert round_trip_cost_pct(state, 0.5, fee_pct=0.0) == pytest.approx(0.0, abs=1e-9)


def test_round_trip_cost_is_exactly_two_fees():
    """С комиссией f круг стоит ровно 1-(1-f)², и ни процентом больше."""
    for fee in (0.5, 1.0, 2.0):
        expected = (1.0 - (1.0 - fee / 100.0) ** 2) * 100.0
        assert round_trip_cost_pct(curve(), 0.3, fee_pct=fee) == pytest.approx(expected, abs=1e-9)


def test_bigger_order_pays_worse_price():
    state = curve()
    prices = [buy_quote(state, size).avg_price for size in (0.1, 0.5, 1.0, 3.0)]
    assert prices == sorted(prices)
    assert all(price > state.spot_price for price in prices)


def test_seller_receives_below_spot():
    state = curve()
    bought = buy_quote(state, 0.5)
    sold = sell_quote(bought.state_after, bought.tokens)
    assert sold.avg_price < bought.state_after.spot_price


def test_two_small_buys_beat_one_big():
    """Дробление заявки уменьшает влияние — иначе математика неверна."""
    state = curve()
    big = buy_quote(state, 1.0)
    first = buy_quote(state, 0.5)
    second = buy_quote(first.state_after, 0.5)
    assert first.tokens + second.tokens == pytest.approx(big.tokens, rel=1e-12)


# --- потолок по влиянию ---------------------------------------------------


@pytest.mark.parametrize("real_sol", [0.0, 15.0, 80.0])
@pytest.mark.parametrize("target", [2.0, 3.0, 10.0])
def test_impact_cap_is_exact(real_sol, target):
    state = curve(real_sol)
    cap = max_sol_for_impact(state, target)
    assert buy_quote(state, cap).impact_pct == pytest.approx(target, abs=1e-9)


def test_impact_cap_zero_when_fee_eats_allowance():
    """Допуск не больше комиссии — покупать нельзя вообще, а не «чуть-чуть»."""
    assert max_sol_for_impact(curve(), max_impact_pct=1.0, fee_pct=1.0) == 0.0
    assert max_sol_for_impact(curve(), max_impact_pct=0.5, fee_pct=1.0) == 0.0


def test_impact_cap_grows_with_liquidity():
    thin = max_sol_for_impact(curve(0.0), 3.0)
    thick = max_sol_for_impact(curve(80.0), 3.0)
    assert thick > thin * 2


# --- восстановление состояния --------------------------------------------


@pytest.mark.parametrize("real_sol", [0.0, 5.0, 40.0, 85.0])
def test_state_recovered_from_spot_price(real_sol):
    state = curve(real_sol)
    back = CurveState.from_spot_price(state.spot_price)
    assert back is not None
    assert back.sol_reserves == pytest.approx(state.sol_reserves, rel=1e-9)
    assert back.token_reserves == pytest.approx(state.token_reserves, rel=1e-9)


def test_state_from_api_converts_units():
    state = CurveState.from_api({
        "virtual_sol_reserves": 45_000_000_000,        # лампорты
        "virtual_token_reserves": 715_333_460_666_667,  # шесть знаков
    })
    assert state is not None
    assert state.sol_reserves == pytest.approx(45.0)
    assert state.token_reserves == pytest.approx(715_333_460.666667, rel=1e-9)
    assert state.real_sol == pytest.approx(15.0)


def test_state_from_api_rejects_junk():
    assert CurveState.from_api({}) is None
    assert CurveState.from_api({"virtual_sol_reserves": 0, "virtual_token_reserves": 1}) is None
    assert CurveState.from_api({"virtual_sol_reserves": "нет", "virtual_token_reserves": 1}) is None


def test_state_from_any_falls_back_to_market_cap():
    state = state_from_any({}, market_cap_sol=60.0)
    assert state is not None
    assert state.spot_price == pytest.approx(60.0 / TOTAL_SUPPLY)


def test_state_from_any_gives_up_without_data():
    assert state_from_any({}, market_cap_sol=0.0) is None


def test_state_from_any_falls_back_to_sol_in_curve():
    from src.curve import INITIAL_VIRTUAL_SOL, real_sol_from_hint, state_from_sol_in_curve

    state = state_from_any({}, market_cap_sol=0.0, sol_in_curve=35.0)
    assert state is not None
    assert state.real_sol == pytest.approx(5.0)
    assert real_sol_from_hint(35.0) == pytest.approx(5.0)
    assert real_sol_from_hint(5.0) == pytest.approx(5.0)
    small = state_from_sol_in_curve(5.0)
    assert small is not None
    assert small.sol_reserves == pytest.approx(INITIAL_VIRTUAL_SOL + 5.0)


# --- прогресс -------------------------------------------------------------


def test_progress_excludes_virtual_reserve():
    assert progress_from_sol(INITIAL_VIRTUAL_SOL) == 0.0
    assert progress_from_sol(INITIAL_VIRTUAL_SOL + CURVE_COMPLETION_SOL) == 1.0
    assert progress_from_sol(INITIAL_VIRTUAL_SOL + 34.0) == pytest.approx(0.4)


def test_progress_clamped():
    assert progress_from_sol(0.0) == 0.0
    assert progress_from_sol(-5.0) == 0.0
    assert progress_from_sol(10_000.0) == 1.0


def test_state_progress_matches_helper():
    state = curve(34.0)
    assert state.progress == pytest.approx(progress_from_sol(state.sol_reserves))


# --- отказы ---------------------------------------------------------------


def test_zero_and_negative_orders_refused():
    state = curve()
    for size in (0.0, -1.0):
        assert not buy_quote(state, size).ok
        assert not sell_quote(state, size).ok


def test_broken_state_refuses_everything():
    broken = CurveState(sol_reserves=0.0, token_reserves=0.0)
    assert not broken.is_valid
    assert not buy_quote(broken, 1.0).ok
    assert not sell_quote(broken, 1.0).ok
    assert max_sol_for_impact(broken, 3.0) == 0.0
    assert round_trip_cost_pct(broken, 1.0) == 100.0


def test_fee_eating_whole_order_is_refused():
    assert not buy_quote(curve(), 1.0, fee_pct=100.0).ok


# --- разное ---------------------------------------------------------------


def test_price_from_reserves_prefers_reserves_over_market_cap():
    data = {"virtual_sol_reserves": 45_000_000_000,
            "virtual_token_reserves": 715_333_460_666_667,
            "market_cap": 999_999}
    assert price_from_reserves(data) == pytest.approx(45.0 / 715_333_460.666667, rel=1e-9)


def test_fresh_curve_market_cap_is_sane():
    """Новая кривая на pump.fun стоит около 28 SOL. Если константы уехали,
    это первое, что сломается."""
    assert CurveState().spot_price * TOTAL_SUPPLY == pytest.approx(28.0, abs=1.0)


def test_sanity_check_reports_numbers():
    numbers = sanity_check()
    assert numbers["spot_price"] > 0
    assert 0 < numbers["round_trip_0.5_sol"] < 5
    assert numbers["max_sol_for_3pct"] > 0
    assert math.isfinite(numbers["tokens_for_1_sol"])
    assert DEFAULT_TRADE_FEE_PCT == 1.0
