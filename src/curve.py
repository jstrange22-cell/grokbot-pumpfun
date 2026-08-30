"""Математика бондинговой кривой pump.fun.

До этого модуля пайплайн считал, что покупка происходит по цене котировки.
Это неправда сразу трижды: биржа берёт комиссию, покупка двигает цену
против покупателя, и на выходе всё повторяется в обратную сторону. На
кривой с несколькими десятками SOL резерва заявка на 0.5 SOL — это заметная
доля ликвидности, а не пылинка.

Практический смысл: без этих поправок dry-run показывает прибыль, которой
в live не будет, а именно по dry-run принимается решение включать live.
Лучше пусть цифры будут скучнее, но настоящие.

Кривая — постоянное произведение на виртуальных резервах:

    k = sol_reserves * token_reserves = const

Покупка добавляет SOL и забирает токены, продажа наоборот. Комиссия
снимается с входящей стороны при покупке и с исходящей при продаже.

ОГОВОРКА: константы ниже — параметры программы pump.fun, какими они были
на момент написания. Программа обновляется. Перед включением live сверьте
их с ончейн-состоянием `global`-аккаунта, а не доверяйте этому файлу.
"""

from __future__ import annotations

import math
from typing import Any

from pydantic import BaseModel, Field

# Стартовые виртуальные резервы новой кривой.
INITIAL_VIRTUAL_SOL = 30.0
INITIAL_VIRTUAL_TOKENS = 1_073_000_191.0

# Полный выпуск и та его часть, что реально продаётся на кривой.
TOTAL_SUPPLY = 1_000_000_000.0
CURVE_TOKEN_SUPPLY = 793_100_000.0

# Столько реальных SOL набирает кривая к моменту переезда на Raydium.
CURVE_COMPLETION_SOL = 85.0

# Комиссия площадки с каждой сделки, процентов.
DEFAULT_TRADE_FEE_PCT = 1.0


class CurveState(BaseModel):
    """Виртуальные резервы кривой. Всё в SOL и целых токенах."""

    sol_reserves: float = INITIAL_VIRTUAL_SOL
    token_reserves: float = INITIAL_VIRTUAL_TOKENS
    # Кривая закончилась, токен уехал на Raydium. Вся математика этого
    # модуля с этого момента к нему неприменима.
    complete: bool = False

    @property
    def is_valid(self) -> bool:
        return self.sol_reserves > 0 and self.token_reserves > 0

    @property
    def k(self) -> float:
        return self.sol_reserves * self.token_reserves

    @property
    def spot_price(self) -> float:
        """Цена бесконечно малой сделки. Ею и торговали до этого модуля."""
        if not self.is_valid:
            return 0.0
        return self.sol_reserves / self.token_reserves

    @property
    def real_sol(self) -> float:
        """Сколько настоящих SOL уже внесено в кривую."""
        return max(0.0, self.sol_reserves - INITIAL_VIRTUAL_SOL)

    @property
    def progress(self) -> float:
        """Заполнение кривой, 0..1. По реальным SOL, а не по виртуальным."""
        return max(0.0, min(1.0, self.real_sol / CURVE_COMPLETION_SOL))

    @classmethod
    def from_spot_price(cls, spot: float) -> CurveState | None:
        """Резервы из одной лишь спотовой цены.

        Произведение резервов на кривой постоянно, поэтому пара
        (sol, tokens) восстанавливается однозначно:
            sol = sqrt(k * spot),  tokens = sqrt(k / spot).
        Это не приближение, а тождество — пока токен не уехал на Raydium.
        """
        if spot <= 0:
            return None
        k = INITIAL_VIRTUAL_SOL * INITIAL_VIRTUAL_TOKENS
        return cls(sol_reserves=math.sqrt(k * spot), token_reserves=math.sqrt(k / spot))

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> CurveState | None:
        """Резервы из ответа провайдера. None, если их там нет.

        Провайдеры отдают их в лампортах и в «сырых» единицах токена
        (6 знаков), поэтому приводим к человеческим числам.
        """
        sol_raw = data.get("virtual_sol_reserves")
        token_raw = data.get("virtual_token_reserves")
        if not sol_raw or not token_raw:
            return None
        try:
            state = cls(
                sol_reserves=float(sol_raw) / 1e9,
                token_reserves=float(token_raw) / 1e6,
                complete=bool(data.get("complete") or data.get("raydium_pool")),
            )
        except (TypeError, ValueError):
            return None
        return state if state.is_valid else None


class Quote(BaseModel):
    """Что на самом деле получится, если отправить эту заявку сейчас."""

    ok: bool = True
    reason: str = ""

    sol_in: float = 0.0          # для покупки — сколько SOL уходит всего
    sol_out: float = 0.0         # для продажи — сколько SOL приходит на руки
    tokens: float = 0.0          # сколько токенов получено или продано
    fee_sol: float = 0.0

    spot_price: float = 0.0      # цена до сделки
    avg_price: float = 0.0       # средняя цена исполнения — по ней и считаем PnL
    price_after: float = 0.0
    impact_pct: float = 0.0      # насколько средняя цена хуже котировки

    state_after: CurveState = Field(default_factory=CurveState)


def buy_quote(
    state: CurveState,
    sol_in: float,
    fee_pct: float = DEFAULT_TRADE_FEE_PCT,
) -> Quote:
    """Покупка на `sol_in` SOL: сколько токенов и по какой средней цене.

    Комиссия снимается со входящих SOL: в кривую попадает меньше, чем
    списано с кошелька, а средняя цена считается по списанному — именно
    она определяет, в плюсе позиция или нет.
    """
    if sol_in <= 0:
        return Quote(ok=False, reason="нулевая заявка")
    if not state.is_valid:
        return Quote(ok=False, reason="резервы кривой неизвестны")

    fee = sol_in * max(0.0, fee_pct) / 100.0
    sol_to_curve = sol_in - fee
    if sol_to_curve <= 0:
        return Quote(ok=False, reason="комиссия съедает заявку целиком")

    new_sol = state.sol_reserves + sol_to_curve
    new_tokens = state.k / new_sol
    tokens_out = state.token_reserves - new_tokens
    if tokens_out <= 0:
        return Quote(ok=False, reason="кривая не отдаёт токены на такую заявку")

    after = CurveState(sol_reserves=new_sol, token_reserves=new_tokens)
    avg_price = sol_in / tokens_out
    spot = state.spot_price
    return Quote(
        sol_in=sol_in,
        tokens=tokens_out,
        fee_sol=fee,
        spot_price=spot,
        avg_price=avg_price,
        price_after=after.spot_price,
        impact_pct=(avg_price / spot - 1.0) * 100.0 if spot > 0 else 0.0,
        state_after=after,
    )


def sell_quote(
    state: CurveState,
    tokens_in: float,
    fee_pct: float = DEFAULT_TRADE_FEE_PCT,
) -> Quote:
    """Продажа `tokens_in` токенов: сколько SOL останется после комиссии."""
    if tokens_in <= 0:
        return Quote(ok=False, reason="нулевая заявка")
    if not state.is_valid:
        return Quote(ok=False, reason="резервы кривой неизвестны")

    new_tokens = state.token_reserves + tokens_in
    new_sol = state.k / new_tokens
    gross = state.sol_reserves - new_sol
    if gross <= 0:
        return Quote(ok=False, reason="кривая не отдаёт SOL на такую заявку")

    fee = gross * max(0.0, fee_pct) / 100.0
    net = gross - fee
    after = CurveState(sol_reserves=new_sol, token_reserves=new_tokens)
    avg_price = net / tokens_in
    spot = state.spot_price
    return Quote(
        sol_out=net,
        tokens=tokens_in,
        fee_sol=fee,
        spot_price=spot,
        avg_price=avg_price,
        price_after=after.spot_price,
        impact_pct=(1.0 - avg_price / spot) * 100.0 if spot > 0 else 0.0,
        state_after=after,
    )


def max_sol_for_impact(
    state: CurveState,
    max_impact_pct: float,
    fee_pct: float = DEFAULT_TRADE_FEE_PCT,
) -> float:
    """Самая крупная покупка, укладывающаяся в заданное влияние на цену.

    Выводится точно, без перебора. Пусть S и T — резервы, f — доля
    комиссии, s — то, что доходит до кривой. Тогда

        tokens_out = T - k/(S+s) = T·s/(S+s)
        avg = sol_in/tokens_out = (S+s) / (T·(1-f))
        avg/spot = (1 + s/S) / (1-f)

    отсюда предельная доля резерва s/S = (1+impact)·(1-f) - 1, а сама
    заявка получается делением на (1-f): комиссия до кривой не доходит,
    но в среднюю цену входит.
    """
    if not state.is_valid or max_impact_pct <= 0:
        return 0.0
    fee_share = max(0.0, min(0.99, fee_pct / 100.0))

    share = (1.0 + max_impact_pct / 100.0) * (1.0 - fee_share) - 1.0
    if share <= 0:
        return 0.0          # одна комиссия уже съедает весь допуск
    return state.sol_reserves * share / (1.0 - fee_share)


def round_trip_cost_pct(
    state: CurveState,
    sol_in: float,
    fee_pct: float = DEFAULT_TRADE_FEE_PCT,
) -> float:
    """Во сколько процентов обойдётся вход и немедленный выход.

    Это порог, ниже которого сделка не имеет смысла: если ожидаемое
    движение меньше, чем стоимость входа-выхода, торговать нечем.
    """
    buy = buy_quote(state, sol_in, fee_pct)
    if not buy.ok:
        return 100.0
    sell = sell_quote(buy.state_after, buy.tokens, fee_pct)
    if not sell.ok:
        return 100.0
    return (1.0 - sell.sol_out / sol_in) * 100.0


def real_sol_from_hint(sol_in_curve: float) -> float:
    """Сколько реальных SOL в кривой по полю с сокета или REST.

    `vSolInBondingCurve` и `virtual_sol_reserves` включают 30 виртуальных
    SOL. Маленькое число — уже реальная ликвидность: иначе порог
    `min_curve_liquidity_sol` (3) никогда не выполнится из `sol_in_curve=5`.
    """
    if sol_in_curve <= 0:
        return 0.0
    if sol_in_curve > INITIAL_VIRTUAL_SOL:
        return sol_in_curve - INITIAL_VIRTUAL_SOL
    return sol_in_curve


def state_from_sol_in_curve(
    sol_in_curve: float, *, complete: bool = False,
) -> CurveState | None:
    """Кривая из одного объёма SOL: виртуальный (обычно) или реальный."""
    if sol_in_curve <= 0:
        return None
    sol = sol_in_curve if sol_in_curve > INITIAL_VIRTUAL_SOL else (
        INITIAL_VIRTUAL_SOL + sol_in_curve
    )
    k = INITIAL_VIRTUAL_SOL * INITIAL_VIRTUAL_TOKENS
    state = CurveState(sol_reserves=sol, token_reserves=k / sol, complete=complete)
    return state if state.is_valid else None


def state_from_any(
    data: dict[str, Any],
    market_cap_sol: float = 0.0,
    sol_in_curve: float = 0.0,
) -> CurveState | None:
    """Состояние кривой из чего угодно: резервов, капитализации, цены, SOL."""
    state = CurveState.from_api(data)
    if state is not None:
        return state
    spot = price_from_reserves(data)
    if spot <= 0 and market_cap_sol > 0:
        spot = market_cap_sol / TOTAL_SUPPLY
    restored = CurveState.from_spot_price(spot)
    if restored is not None:
        restored.complete = bool(data.get("complete") or data.get("raydium_pool"))
        return restored
    return state_from_sol_in_curve(
        sol_in_curve,
        complete=bool(data.get("complete") or data.get("raydium_pool")),
    )


def price_from_reserves(data: dict[str, Any]) -> float:
    """Спотовая цена из ответа провайдера, с запасным вариантом по капитализации."""
    state = CurveState.from_api(data)
    if state is not None:
        return state.spot_price
    market_cap = data.get("market_cap") or data.get("usd_market_cap")
    if market_cap:
        try:
            return float(market_cap) / TOTAL_SUPPLY
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def progress_from_sol(sol_in_curve: float) -> float:
    """Заполнение кривой по одному лишь объёму SOL — как его отдаёт сокет."""
    if sol_in_curve <= 0:
        return 0.0
    real = max(0.0, sol_in_curve - INITIAL_VIRTUAL_SOL)
    return max(0.0, min(1.0, real / CURVE_COMPLETION_SOL))


def tokens_for_market_cap(market_cap_sol: float) -> float:
    """Обратная прикидка: сколько токенов на SOL при такой капитализации."""
    if market_cap_sol <= 0:
        return 0.0
    return TOTAL_SUPPLY / market_cap_sol


def sanity_check() -> dict[str, float]:
    """Числа, по которым видно, что модуль считает не ерунду.

    Полезно вызвать руками после обновления констант программы.
    """
    fresh = CurveState()
    return {
        "spot_price": fresh.spot_price,
        "impact_0.5_sol": buy_quote(fresh, 0.5).impact_pct,
        "round_trip_0.5_sol": round_trip_cost_pct(fresh, 0.5),
        "max_sol_for_3pct": max_sol_for_impact(fresh, 3.0),
        "tokens_for_1_sol": buy_quote(fresh, 1.0).tokens,
    }


assert math.isclose(CurveState().k, INITIAL_VIRTUAL_SOL * INITIAL_VIRTUAL_TOKENS)
