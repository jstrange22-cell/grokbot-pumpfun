"""Исполнение сделок на Solana.

`DryRunExecutor` — путь по умолчанию: считает ту же кривую, что и live,
но транзакцию не отправляет. `LiveExecutor` собирает buy/sell pump.fun и
шлёт её в RPC или Jito, но включается только при `mode: live` плюс флаг
`--i-understand-the-risk` и настоящий ключ кошелька.

Всё остальное настоящее и, что важнее, честное: dry-run исполняется по
математике кривой из `curve.py` — с комиссией, с проскальзыванием и с
влиянием собственной заявки на цену. Раньше он покупал по цене котировки,
и dry-run показывал прибыль, которой в live не бывает.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx
from pydantic import BaseModel, Field
from solders.instruction import Instruction
from solders.keypair import Keypair
from solders.pubkey import Pubkey

from .curve import (
    TOTAL_SUPPLY,
    CurveState,
    buy_quote,
    price_from_reserves,
    sell_quote,
    state_from_any,
)
from .models import Config, Position, Token
from .onchain import (
    DEFAULT_SLIPPAGE,
    LAMPORTS_PER_SOL,
    Accounts,
    BondingCurveOnchain,
    LiveClosed,
    SolanaRpc,
    budget_and_tip,
    build_buy_instruction,
    build_sell_instruction,
    close_token_account,
    create_ata_idempotent,
    derive_accounts,
    load_keypair,
    parse_bonding_curve,
    parse_fee_recipient,
    pubkey_from_str,
    sign_transaction,
    sol_to_lamports,
    token_program_from_mint,
    tokens_to_raw,
)

log = logging.getLogger(__name__)

DRY_RUN_TX = "dry_run"

__all__ = [
    "TOTAL_SUPPLY",
    "BaseExecutor",
    "DryRunExecutor",
    "ExecutionResult",
    "LiveExecutor",
    "build_executor",
    "new_position",
    "price_from_reserves",
]


class ExecutionResult(BaseModel):
    """Итог попытки исполнения."""

    ok: bool
    tx_hash: str = ""
    price: float = 0.0           # средняя цена исполнения, а не котировка
    token_amount: float = 0.0
    sol_amount: float = 0.0
    fee_sol: float = 0.0
    impact_pct: float = 0.0
    error: str = ""
    state_after: CurveState | None = Field(default=None)


class BaseExecutor:
    """Общая часть: котировки, состояние кривой, расчёт исполнения."""

    def __init__(self, config: Config, client: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self.market = config.market
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> BaseExecutor:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.config.data.rest_url,
                timeout=self.config.data.request_timeout,
            )
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _coin(self, mint: str) -> dict[str, Any]:
        if self._client is None:
            return {}
        try:
            resp = await self._client.get(f"/coins/{mint}")
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            log.warning("данные по %s недоступны: %s", mint, exc)
            return {}
        return data if isinstance(data, dict) else {}

    async def curve(self, mint: str, market_cap_sol: float = 0.0) -> CurveState | None:
        """Состояние кривой сейчас. None, если восстановить не из чего."""
        return state_from_any(await self._coin(mint), market_cap_sol)

    async def price(self, mint: str) -> float:
        """Спотовая цена. Ею меряются правила выхода — они про движение рынка,
        а не про исполнение конкретной заявки."""
        state = await self.curve(mint)
        return state.spot_price if state else 0.0

    async def buy(self, token: Token, size_sol: float) -> ExecutionResult:
        raise NotImplementedError

    async def sell(self, position: Position, fraction: float = 1.0) -> ExecutionResult:
        raise NotImplementedError

    # -- расчёт, общий для обоих режимов ----------------------------------

    def plan_buy(self, state: CurveState, size_sol: float) -> ExecutionResult:
        quote = buy_quote(state, size_sol, self.market.trade_fee_pct)
        if not quote.ok:
            return ExecutionResult(ok=False, error=quote.reason)
        if quote.impact_pct > self.market.max_price_impact_pct:
            return ExecutionResult(
                ok=False,
                error=(f"влияние на цену {quote.impact_pct:.2f}% выше "
                       f"потолка {self.market.max_price_impact_pct:.2f}%"),
                impact_pct=quote.impact_pct,
            )
        return ExecutionResult(
            ok=True,
            price=quote.avg_price,
            token_amount=quote.tokens,
            sol_amount=size_sol,
            fee_sol=quote.fee_sol,
            impact_pct=quote.impact_pct,
            state_after=quote.state_after,
        )

    def plan_sell(self, state: CurveState, tokens: float) -> ExecutionResult:
        quote = sell_quote(state, tokens, self.market.trade_fee_pct)
        if not quote.ok:
            return ExecutionResult(ok=False, error=quote.reason)
        return ExecutionResult(
            ok=True,
            price=quote.avg_price,
            token_amount=tokens,
            sol_amount=quote.sol_out,
            fee_sol=quote.fee_sol,
            impact_pct=quote.impact_pct,
            state_after=quote.state_after,
        )

    @staticmethod
    def _portion(position: Position, fraction: float) -> float:
        """Сколько токенов продаём. Хвост меньше процента добираем целиком:
        оставлять пыль в позиции незачем, она только мешает учёту."""
        fraction = max(0.0, min(1.0, fraction))
        tokens = position.token_amount * fraction
        if position.token_amount - tokens < position.token_amount * 0.01:
            tokens = position.token_amount
        return tokens


class DryRunExecutor(BaseExecutor):
    """Проходит весь путь, кроме отправки транзакции."""

    async def buy(self, token: Token, size_sol: float) -> ExecutionResult:
        state = await self.curve(token.mint, token.market_cap_sol)
        if state is None:
            # Позиция с неизвестной ценой входа неуправляема: ни одно
            # правило выхода на ней не срабатывает.
            log.warning("покупка %s отменена: состояние кривой неизвестно", token.mint[:8])
            return ExecutionResult(ok=False, error="состояние кривой неизвестно")

        result = self.plan_buy(state, size_sol)
        if not result.ok:
            log.warning("покупка %s отменена: %s", token.mint[:8], result.error)
            return result

        result.tx_hash = DRY_RUN_TX
        log.info("[dry-run] куплено %s: %.4f SOL -> %.0f токенов по %.12f "
                 "(комиссия %.4f SOL, влияние %.2f%%)",
                 token.mint[:8], size_sol, result.token_amount, result.price,
                 result.fee_sol, result.impact_pct)
        return result

    async def sell(self, position: Position, fraction: float = 1.0) -> ExecutionResult:
        state = await self.curve(position.mint)
        if state is None:
            return ExecutionResult(ok=False, error="состояние кривой неизвестно")

        tokens = self._portion(position, fraction)
        if state.complete:
            # Кривой больше нет: токен торгуется на Raydium со своей
            # ликвидностью, и постоянное произведение к нему неприменимо.
            # Считаем по споту без влияния и честно помечаем прикидкой.
            log.warning("%s уже на Raydium: выручка посчитана по споту, "
                        "без проскальзывания — это прикидка, а не котировка",
                        position.mint[:8])
            gross = tokens * state.spot_price
            fee = gross * self.market.trade_fee_pct / 100.0
            return ExecutionResult(
                ok=True, tx_hash=DRY_RUN_TX, price=state.spot_price,
                token_amount=tokens, sol_amount=gross - fee, fee_sol=fee,
            )

        result = self.plan_sell(state, tokens)
        if not result.ok:
            return result

        result.tx_hash = DRY_RUN_TX
        log.info("[dry-run] продано %s: %.0f токенов -> %.4f SOL по %.12f "
                 "(комиссия %.4f SOL, влияние %.2f%%)",
                 position.mint[:8], tokens, result.sol_amount, result.price,
                 result.fee_sol, result.impact_pct)
        return result


class LiveExecutor(BaseExecutor):
    """Реальная отправка транзакций на бондинговую кривую pump.fun.

    Считает заявку теми же `plan_buy` / `plan_sell`, что и dry-run: из них
    берутся `max_sol_cost` и `min_sol_output` с допуском 1–2%. Дальше —
    ключ, аккаунты кривой, ATA, ComputeBudget, опционально чаевые Jito,
    подтверждение. Нет ключа, нет RPC, нет слота — отказ, не «ну почти».
    """

    def __init__(
        self,
        config: Config,
        client: httpx.AsyncClient | None = None,
        rpc_client: httpx.AsyncClient | None = None,
        rpc: SolanaRpc | None = None,
        slippage: float = DEFAULT_SLIPPAGE,
    ) -> None:
        super().__init__(config, client)
        self.rpc_url = config.solana.rpc_url
        self.jito = config.solana.jito
        self._rpc_http = rpc_client
        self._owns_rpc = rpc_client is None and rpc is None
        self._rpc = rpc
        self.slippage = slippage

    async def __aenter__(self) -> LiveExecutor:
        await super().__aenter__()
        if self._rpc is None and self._rpc_http is None:
            self._rpc_http = httpx.AsyncClient(timeout=20.0)
            self._owns_rpc = True
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._owns_rpc and self._rpc_http is not None:
            await self._rpc_http.aclose()
            self._rpc_http = None
        await super().__aexit__(*exc)

    def _rpc_or_fail(self) -> SolanaRpc:
        if self._rpc is not None:
            return self._rpc
        if self._rpc_http is None:
            raise LiveClosed(
                "RPC-клиент не создан — LiveExecutor нужно открывать через async with"
            )
        self._rpc = SolanaRpc(
            self.rpc_url,
            self._rpc_http,
            jito_url=self.jito.block_engine_url if self.jito.enabled else "",
        )
        return self._rpc

    async def buy(self, token: Token, size_sol: float) -> ExecutionResult:
        try:
            return await self._buy(token, size_sol)
        except LiveClosed as exc:
            log.error("live buy отказан: %s", exc)
            return ExecutionResult(ok=False, error=str(exc))
        except Exception as exc:
            log.exception("live buy упал: %s", exc)
            return ExecutionResult(ok=False, error=f"неожиданная ошибка: {exc}")

    async def sell(self, position: Position, fraction: float = 1.0) -> ExecutionResult:
        try:
            return await self._sell(position, fraction)
        except LiveClosed as exc:
            log.error("live sell отказан: %s", exc)
            return ExecutionResult(ok=False, error=str(exc))
        except Exception as exc:
            log.exception("live sell упал: %s", exc)
            return ExecutionResult(ok=False, error=f"неожиданная ошибка: {exc}")

    async def _buy(self, token: Token, size_sol: float) -> ExecutionResult:
        keypair = load_keypair(self.config.solana.wallet_key)
        state = await self.curve(token.mint, token.market_cap_sol)
        if state is None:
            raise LiveClosed("состояние кривой неизвестно")
        if state.complete:
            raise LiveClosed("кривая уже закрыта, на Raydium этот исполнитель не ходит")

        planned = self.plan_buy(state, size_sol)
        if not planned.ok:
            return planned

        max_sol_cost = sol_to_lamports(size_sol * (1.0 + self.slippage))
        amount = tokens_to_raw(planned.token_amount)
        accounts, _curve = await self._resolve_accounts(token.mint, keypair.pubkey())
        if _curve.complete:
            raise LiveClosed("ончейн: кривая уже закрыта")

        instructions = []
        ata = await self._rpc_or_fail().get_account(accounts.associated_user)
        if ata is None:
            instructions.append(create_ata_idempotent(
                keypair.pubkey(), keypair.pubkey(), accounts.mint, accounts.token_program,
            ))
        instructions.append(build_buy_instruction(accounts, amount, max_sol_cost))
        return await self._send(
            keypair, instructions, planned, spent_sol=size_sol, is_buy=True,
        )

    async def _sell(self, position: Position, fraction: float) -> ExecutionResult:
        keypair = load_keypair(self.config.solana.wallet_key)
        state = await self.curve(position.mint)
        if state is None:
            raise LiveClosed("состояние кривой неизвестно")
        if state.complete:
            # В dry-run здесь считают по споту. В live это уже не кривая:
            # отправить sell в программу — бессмысленно, прикидкой торговать нельзя.
            raise LiveClosed("кривая уже закрыта: live не продаёт по спотовой прикидке")

        tokens = self._portion(position, fraction)
        planned = self.plan_sell(state, tokens)
        if not planned.ok:
            return planned

        min_sol = planned.sol_amount * (1.0 - self.slippage)
        if min_sol <= 0:
            raise LiveClosed("min_sol_output после допуска неположительный")
        amount = tokens_to_raw(tokens)
        accounts, _curve = await self._resolve_accounts(position.mint, keypair.pubkey())
        if _curve.complete:
            raise LiveClosed("ончейн: кривая уже закрыта")

        instructions = [build_sell_instruction(
            accounts, amount, sol_to_lamports(min_sol),
        )]
        full_exit = tokens >= position.token_amount * 0.999
        if full_exit:
            instructions.append(close_token_account(
                accounts.associated_user, keypair.pubkey(),
                keypair.pubkey(), accounts.token_program,
            ))
        return await self._send(
            keypair, instructions, planned, spent_sol=planned.sol_amount, is_buy=False,
        )

    async def _resolve_accounts(
        self, mint_str: str, user: Pubkey
    ) -> tuple[Accounts, BondingCurveOnchain]:
        rpc = self._rpc_or_fail()
        mint = pubkey_from_str(mint_str, "mint")
        mint_info = await rpc.get_account(mint)
        if mint_info is None:
            raise LiveClosed("mint на RPC не найден")
        token_program = token_program_from_mint(mint_info)

        bonding_curve = derive_accounts(
            mint, user, user, user, token_program,
        ).bonding_curve
        curve_info = await rpc.get_account(bonding_curve)
        if curve_info is None:
            raise LiveClosed("аккаунт bonding_curve не найден")
        curve = parse_bonding_curve(curve_info.data)

        global_pda = derive_accounts(mint, user, curve.creator, user, token_program).global_account
        global_info = await rpc.get_account(global_pda)
        if global_info is None:
            raise LiveClosed("global-аккаунт не найден")
        fee_recipient = parse_fee_recipient(global_info.data, mayhem=curve.mayhem)
        return derive_accounts(mint, user, curve.creator, fee_recipient, token_program), curve

    async def _send(
        self,
        keypair: Keypair,
        instructions: list[Instruction],
        planned: ExecutionResult,
        *,
        spent_sol: float,
        is_buy: bool,
    ) -> ExecutionResult:
        rpc = self._rpc_or_fail()
        prepared, tip_lamports = budget_and_tip(
            instructions,
            keypair.pubkey(),
            jito_enabled=self.jito.enabled,
            tip_lamports=self.jito.tip_lamports,
        )
        blockhash = await rpc.get_latest_blockhash()
        tx = sign_transaction(keypair, prepared, blockhash)
        if self.jito.enabled:
            signature = await rpc.send_jito_bundle(tx)
        else:
            signature = await rpc.send_transaction(tx)
        await rpc.wait_confirmed(signature)

        tip_sol = tip_lamports / LAMPORTS_PER_SOL
        sol_amount = spent_sol + tip_sol if is_buy else max(0.0, planned.sol_amount - tip_sol)
        tokens = planned.token_amount
        price = (sol_amount / tokens) if tokens else planned.price
        return ExecutionResult(
            ok=True,
            tx_hash=signature,
            price=price,
            token_amount=tokens,
            sol_amount=sol_amount,
            fee_sol=planned.fee_sol,
            impact_pct=planned.impact_pct,
            state_after=planned.state_after,
        )


def build_executor(config: Config, client: httpx.AsyncClient | None = None) -> BaseExecutor:
    """Исполнитель по режиму из конфига."""
    if config.is_live:
        log.warning("режим live: используется LiveExecutor")
        return LiveExecutor(config, client)
    return DryRunExecutor(config, client)


def new_position(token: Token, result: ExecutionResult, score: float) -> Position:
    return Position(
        mint=token.mint,
        symbol=token.symbol,
        creator=token.creator,
        entry_price=result.price,
        peak_price=result.price,
        sol_spent=result.sol_amount,
        token_amount=result.token_amount,
        opened_at=time.time(),
        tx_hash=result.tx_hash,
        score=score,
    )
