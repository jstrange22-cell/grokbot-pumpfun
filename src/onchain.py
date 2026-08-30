"""Сборка и отправка транзакций pump.fun. Только то, что нужно LiveExecutor.

Инструкция — классическая `buy` / `sell` программы
`6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P`, с тем набором аккаунтов,
который программа принимает сейчас (creator_vault, volume accumulator,
fee_config). Это не снайпер и не бандл на дамп: один кошелёк, одна
покупка или продажа, потом ждать подтверждения.

Любая неопределённость — отказ, а не догадка: нет ключа, нет аккаунта
кривой, RPC молчит, подтверждения нет — сделка не считается исполненной.
Сеть ходит только через переданный httpx-клиент, чтобы тесты могли
подменить транспорт и никуда не ходили.
"""

from __future__ import annotations

import base64
import logging
import struct
from dataclasses import dataclass
from typing import Any

import httpx
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import Message
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from solders.transaction import Transaction

from .models import is_placeholder

log = logging.getLogger(__name__)

PUMP_PROGRAM = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
FEE_PROGRAM = Pubkey.from_string("pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ")
SYSTEM_PROGRAM = Pubkey.from_string("11111111111111111111111111111111")
TOKEN_PROGRAM = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
TOKEN_2022_PROGRAM = Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
ASSOCIATED_TOKEN_PROGRAM = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
WSOL_MINT = Pubkey.from_string("So11111111111111111111111111111111111111112")

BUY_DISCRIMINATOR = bytes([102, 6, 61, 18, 1, 218, 235, 234])
SELL_DISCRIMINATOR = bytes([51, 230, 133, 164, 1, 127, 131, 173])

TOKEN_DECIMALS = 6
LAMPORTS_PER_SOL = 1_000_000_000
# Допуск к max_sol_cost / min_sol_output. Влияние своей заявки уже сидит
# в plan_*; это запас на то, что кривая уедет между котировкой и слотом.
DEFAULT_SLIPPAGE = 0.02

COMPUTE_UNIT_LIMIT = 300_000
COMPUTE_UNIT_PRICE = 50_000

# Первый «официальный» tip-аккаунт Jito. Не крутим случайно: тест и
# эксплуатация должны видеть один и тот же перевод.
JITO_TIP_ACCOUNT = Pubkey.from_string("96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5")

CONFIRM_TRIES = 40
CONFIRM_WAIT_SECONDS = 0.4

# Смещение полей в аккаунтах программы. Короче — отказываемся читать.
_GLOBAL_FEE_RECIPIENT = 41
_CURVE_VIRTUAL_TOKEN = 8
_CURVE_VIRTUAL_SOL = 16
_CURVE_COMPLETE = 48
_CURVE_CREATOR = 49
_CURVE_MAYHEM = 81
_CURVE_QUOTE_MINT = 83
_GLOBAL_RESERVED_FEE = 483

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

__all__ = [
    "DEFAULT_SLIPPAGE",
    "LAMPORTS_PER_SOL",
    "PUMP_PROGRAM",
    "TOKEN_DECIMALS",
    "AccountInfo",
    "Accounts",
    "BondingCurveOnchain",
    "LiveClosed",
    "RpcError",
    "SolanaRpc",
    "b58encode",
    "budget_and_tip",
    "build_buy_instruction",
    "build_sell_instruction",
    "close_token_account",
    "create_ata_idempotent",
    "derive_accounts",
    "encode_buy",
    "encode_sell",
    "load_keypair",
    "parse_bonding_curve",
    "parse_fee_recipient",
    "pubkey_from_str",
    "sign_transaction",
    "sol_to_lamports",
    "token_program_from_mint",
    "tokens_to_raw",
]


class LiveClosed(Exception):
    """Отказ live-исполнения: неопределённость важнее сделки."""


class RpcError(LiveClosed):
    """RPC или Jito не ответили так, чтобы можно было продолжать."""


@dataclass(frozen=True)
class AccountInfo:
    pubkey: Pubkey
    owner: Pubkey
    data: bytes
    lamports: int = 0


@dataclass(frozen=True)
class BondingCurveOnchain:
    creator: Pubkey
    complete: bool
    mayhem: bool
    quote_mint: Pubkey
    virtual_token_reserves: int = 0
    virtual_sol_reserves: int = 0


@dataclass(frozen=True)
class Accounts:
    mint: Pubkey
    user: Pubkey
    token_program: Pubkey
    bonding_curve: Pubkey
    associated_bonding_curve: Pubkey
    associated_user: Pubkey
    global_account: Pubkey
    fee_recipient: Pubkey
    creator_vault: Pubkey
    event_authority: Pubkey
    global_volume_accumulator: Pubkey
    user_volume_accumulator: Pubkey
    fee_config: Pubkey


def b58encode(data: bytes) -> str:
    """Минимальный base58 без сторонней библиотеки — для бандла Jito."""
    n = int.from_bytes(data, "big")
    out = ""
    while n:
        n, rem = divmod(n, 58)
        out = _B58[rem] + out
    pad = 0
    for byte in data:
        if byte == 0:
            pad += 1
        else:
            break
    return ("1" * pad) + (out or "1")


def load_keypair(secret: str) -> Keypair:
    """Ключ только как байты: base58, JSON-массив или hex. Не seed-фраза."""
    raw = secret.strip()
    if not raw or is_placeholder(raw):
        raise LiveClosed("нет ключа кошелька")
    if " " in raw:
        # Seed-фраза Phantom/SafePal сюда не принимается: это не hot-wallet.
        raise LiveClosed("ключ кошелька похож на seed-фразу — отказ")
    if raw.startswith("["):
        try:
            return Keypair.from_json(raw)
        except Exception as exc:
            raise LiveClosed(f"JSON-ключ кошелька не читается: {exc}") from exc
    try:
        return Keypair.from_base58_string(raw)
    except Exception:
        pass
    try:
        blob = bytes.fromhex(raw)
    except ValueError:
        blob = b""
    if len(blob) == 64:
        try:
            return Keypair.from_bytes(blob)
        except Exception as exc:
            raise LiveClosed(f"hex-ключ кошелька не читается: {exc}") from exc
    if len(blob) == 32:
        try:
            return Keypair.from_seed(blob)
        except Exception as exc:
            raise LiveClosed(f"seed кошелька не читается: {exc}") from exc
    raise LiveClosed("ключ кошелька не читается (нужен base58, JSON или hex)")


def pubkey_from_str(value: str, what: str = "pubkey") -> Pubkey:
    try:
        return Pubkey.from_string(value)
    except Exception as exc:
        raise LiveClosed(f"{what} не pubkey: {value!r}") from exc


def tokens_to_raw(tokens: float) -> int:
    raw = int(tokens * 10**TOKEN_DECIMALS)
    if raw <= 0:
        raise LiveClosed("нулевое количество токенов")
    return raw


def sol_to_lamports(sol: float) -> int:
    raw = round(sol * LAMPORTS_PER_SOL)
    if raw <= 0:
        raise LiveClosed("нулевая сумма в лампортах")
    return raw


def _pda(seeds: list[bytes], program: Pubkey) -> Pubkey:
    return Pubkey.find_program_address(seeds, program)[0]


def associated_token_address(owner: Pubkey, mint: Pubkey, token_program: Pubkey) -> Pubkey:
    return _pda([bytes(owner), bytes(token_program), bytes(mint)], ASSOCIATED_TOKEN_PROGRAM)


def derive_accounts(
    mint: Pubkey,
    user: Pubkey,
    creator: Pubkey,
    fee_recipient: Pubkey,
    token_program: Pubkey,
) -> Accounts:
    bonding_curve = _pda([b"bonding-curve", bytes(mint)], PUMP_PROGRAM)
    return Accounts(
        mint=mint,
        user=user,
        token_program=token_program,
        bonding_curve=bonding_curve,
        associated_bonding_curve=associated_token_address(bonding_curve, mint, token_program),
        associated_user=associated_token_address(user, mint, token_program),
        global_account=_pda([b"global"], PUMP_PROGRAM),
        fee_recipient=fee_recipient,
        creator_vault=_pda([b"creator-vault", bytes(creator)], PUMP_PROGRAM),
        event_authority=_pda([b"__event_authority"], PUMP_PROGRAM),
        global_volume_accumulator=_pda([b"global_volume_accumulator"], PUMP_PROGRAM),
        user_volume_accumulator=_pda([b"user_volume_accumulator", bytes(user)], PUMP_PROGRAM),
        fee_config=_pda([b"fee_config", bytes(PUMP_PROGRAM)], FEE_PROGRAM),
    )


def parse_bonding_curve(data: bytes) -> BondingCurveOnchain:
    if len(data) < _CURVE_CREATOR + 32:
        raise LiveClosed("аккаунт кривой короче, чем нужно: нет creator")
    complete = bool(data[_CURVE_COMPLETE])
    creator = Pubkey.from_bytes(data[_CURVE_CREATOR:_CURVE_CREATOR + 32])
    mayhem = bool(data[_CURVE_MAYHEM]) if len(data) > _CURVE_MAYHEM else False
    if len(data) >= _CURVE_QUOTE_MINT + 32:
        quote = Pubkey.from_bytes(data[_CURVE_QUOTE_MINT:_CURVE_QUOTE_MINT + 32])
    else:
        quote = Pubkey.default()
    if quote not in (Pubkey.default(), WSOL_MINT):
        raise LiveClosed("кривая не в SOL — этот исполнитель такие не берёт")
    virtual_token = 0
    virtual_sol = 0
    if len(data) >= _CURVE_VIRTUAL_SOL + 8:
        virtual_token = struct.unpack_from("<Q", data, _CURVE_VIRTUAL_TOKEN)[0]
        virtual_sol = struct.unpack_from("<Q", data, _CURVE_VIRTUAL_SOL)[0]
    return BondingCurveOnchain(
        creator=creator, complete=complete, mayhem=mayhem, quote_mint=quote,
        virtual_token_reserves=virtual_token, virtual_sol_reserves=virtual_sol,
    )


def parse_fee_recipient(data: bytes, mayhem: bool = False) -> Pubkey:
    if len(data) < _GLOBAL_FEE_RECIPIENT + 32:
        raise LiveClosed("global-аккаунт короче, чем нужно: нет fee_recipient")
    if mayhem:
        if len(data) < _GLOBAL_RESERVED_FEE + 32:
            raise LiveClosed("mayhem-монета, а reserved fee_recipient не прочитан")
        recipient = Pubkey.from_bytes(data[_GLOBAL_RESERVED_FEE:_GLOBAL_RESERVED_FEE + 32])
    else:
        recipient = Pubkey.from_bytes(
            data[_GLOBAL_FEE_RECIPIENT:_GLOBAL_FEE_RECIPIENT + 32]
        )
    if recipient == Pubkey.default():
        raise LiveClosed("fee_recipient пустой")
    return recipient


def encode_buy(amount: int, max_sol_cost: int, track_volume: bool = False) -> bytes:
    return (
        BUY_DISCRIMINATOR
        + struct.pack("<Q", amount)
        + struct.pack("<Q", max_sol_cost)
        + bytes([1 if track_volume else 0])
    )


def encode_sell(amount: int, min_sol_output: int) -> bytes:
    return SELL_DISCRIMINATOR + struct.pack("<Q", amount) + struct.pack("<Q", min_sol_output)


def _meta(pubkey: Pubkey, writable: bool = False, signer: bool = False) -> AccountMeta:
    return AccountMeta(pubkey, is_signer=signer, is_writable=writable)


def build_buy_instruction(accounts: Accounts, amount: int, max_sol_cost: int) -> Instruction:
    keys = [
        _meta(accounts.global_account),
        _meta(accounts.fee_recipient, writable=True),
        _meta(accounts.mint),
        _meta(accounts.bonding_curve, writable=True),
        _meta(accounts.associated_bonding_curve, writable=True),
        _meta(accounts.associated_user, writable=True),
        _meta(accounts.user, writable=True, signer=True),
        _meta(SYSTEM_PROGRAM),
        _meta(accounts.token_program),
        _meta(accounts.creator_vault, writable=True),
        _meta(accounts.event_authority),
        _meta(PUMP_PROGRAM),
        _meta(accounts.global_volume_accumulator),
        _meta(accounts.user_volume_accumulator, writable=True),
        _meta(accounts.fee_config),
        _meta(FEE_PROGRAM),
    ]
    return Instruction(PUMP_PROGRAM, encode_buy(amount, max_sol_cost), keys)


def build_sell_instruction(accounts: Accounts, amount: int, min_sol_output: int) -> Instruction:
    # У sell token_program и creator_vault стоят в другом порядке, чем у buy.
    keys = [
        _meta(accounts.global_account),
        _meta(accounts.fee_recipient, writable=True),
        _meta(accounts.mint),
        _meta(accounts.bonding_curve, writable=True),
        _meta(accounts.associated_bonding_curve, writable=True),
        _meta(accounts.associated_user, writable=True),
        _meta(accounts.user, writable=True, signer=True),
        _meta(SYSTEM_PROGRAM),
        _meta(accounts.creator_vault, writable=True),
        _meta(accounts.token_program),
        _meta(accounts.event_authority),
        _meta(PUMP_PROGRAM),
        _meta(accounts.fee_config),
        _meta(FEE_PROGRAM),
    ]
    return Instruction(PUMP_PROGRAM, encode_sell(amount, min_sol_output), keys)


def create_ata_idempotent(
    payer: Pubkey, owner: Pubkey, mint: Pubkey, token_program: Pubkey,
) -> Instruction:
    dest = associated_token_address(owner, mint, token_program)
    return Instruction(
        ASSOCIATED_TOKEN_PROGRAM,
        bytes([1]),
        [
            _meta(payer, writable=True, signer=True),
            _meta(dest, writable=True),
            _meta(owner),
            _meta(mint),
            _meta(SYSTEM_PROGRAM),
            _meta(token_program),
        ],
    )


def close_token_account(
    account: Pubkey, dest: Pubkey, owner: Pubkey, token_program: Pubkey,
) -> Instruction:
    return Instruction(
        token_program,
        bytes([9]),
        [_meta(account, writable=True), _meta(dest, writable=True), _meta(owner, signer=True)],
    )


def sign_transaction(
    payer: Keypair,
    instructions: list[Instruction],
    blockhash: Hash,
) -> Transaction:
    message = Message.new_with_blockhash(instructions, payer.pubkey(), blockhash)
    tx = Transaction.new_unsigned(message)
    tx.sign([payer], blockhash)
    if not tx.signatures:
        raise LiveClosed("транзакция не подписалась")
    return tx


class SolanaRpc:
    """JSON-RPC Solana и бандл Jito через один httpx-клиент."""

    def __init__(
        self,
        url: str,
        client: httpx.AsyncClient,
        jito_url: str = "",
        confirm_tries: int = CONFIRM_TRIES,
        confirm_wait: float = CONFIRM_WAIT_SECONDS,
        sleeper: Any = None,
    ) -> None:
        self.url = url
        self.client = client
        self.jito_url = jito_url
        self.confirm_tries = confirm_tries
        self.confirm_wait = confirm_wait
        self._sleep = sleeper

    async def call(self, method: str, params: list[Any], url: str | None = None) -> Any:
        target = url or self.url
        try:
            response = await self.client.post(
                target,
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            )
        except Exception as exc:
            raise RpcError(f"RPC {method} недоступен: {exc}") from exc
        if response.status_code >= 400:
            raise RpcError(f"RPC {method} HTTP {response.status_code}")
        try:
            body = response.json()
        except Exception as exc:
            raise RpcError(f"RPC {method} не JSON: {exc}") from exc
        if not isinstance(body, dict):
            raise RpcError(f"RPC {method} странный ответ")
        if body.get("error"):
            raise RpcError(f"RPC {method}: {body['error']}")
        return body.get("result")

    async def get_account(self, pubkey: Pubkey) -> AccountInfo | None:
        result = await self.call(
            "getAccountInfo",
            [str(pubkey), {"encoding": "base64", "commitment": "confirmed"}],
        )
        if not result or not isinstance(result, dict):
            return None
        value = result.get("value")
        if not value:
            return None
        raw = value.get("data")
        if isinstance(raw, list) and raw:
            try:
                data = base64.b64decode(raw[0])
            except Exception as exc:
                raise RpcError(f"аккаунт {pubkey} не декодируется: {exc}") from exc
        elif isinstance(raw, str):
            try:
                data = base64.b64decode(raw)
            except Exception as exc:
                raise RpcError(f"аккаунт {pubkey} не декодируется: {exc}") from exc
        else:
            raise RpcError(f"аккаунт {pubkey}: нет данных")
        try:
            owner = Pubkey.from_string(value["owner"])
        except Exception as exc:
            raise RpcError(f"аккаунт {pubkey}: owner не pubkey") from exc
        return AccountInfo(
            pubkey=pubkey,
            owner=owner,
            data=data,
            lamports=int(value.get("lamports") or 0),
        )

    async def get_latest_blockhash(self) -> Hash:
        result = await self.call("getLatestBlockhash", [{"commitment": "confirmed"}])
        if not isinstance(result, dict):
            raise RpcError("getLatestBlockhash: пустой ответ")
        value = result.get("value") if "value" in result else result
        if not isinstance(value, dict) or not value.get("blockhash"):
            raise RpcError("getLatestBlockhash: нет blockhash")
        try:
            return Hash.from_string(value["blockhash"])
        except Exception as exc:
            raise RpcError("getLatestBlockhash: blockhash не читается") from exc

    async def send_transaction(self, tx: Transaction) -> str:
        payload = base64.b64encode(bytes(tx)).decode()
        result = await self.call(
            "sendTransaction",
            [payload, {
                "encoding": "base64",
                "skipPreflight": False,
                "preflightCommitment": "confirmed",
            }],
        )
        if not isinstance(result, str) or not result:
            raise RpcError("sendTransaction не вернул подпись")
        return result

    async def send_jito_bundle(self, tx: Transaction) -> str:
        if not self.jito_url:
            raise RpcError("Jito включён, но block_engine_url пустой")
        encoded = b58encode(bytes(tx))
        result = await self.call("sendBundle", [[encoded]], url=self.jito_url)
        # Бандл принят. Подпись самой сделки всё равно нужна, чтобы ждать слот.
        if result in (None, ""):
            raise RpcError("Jito sendBundle не вернул идентификатор")
        return str(tx.signatures[0])

    async def wait_confirmed(self, signature: str) -> None:
        import asyncio

        sleep = self._sleep or asyncio.sleep
        for _ in range(self.confirm_tries):
            result = await self.call(
                "getSignatureStatuses",
                [[signature], {"searchTransactionHistory": True}],
            )
            value = None
            if isinstance(result, dict):
                statuses = result.get("value")
                if isinstance(statuses, list) and statuses:
                    value = statuses[0]
            if isinstance(value, dict):
                if value.get("err"):
                    raise RpcError(f"транзакция {signature} отклонена: {value['err']}")
                status = value.get("confirmationStatus") or ""
                if status in ("confirmed", "finalized"):
                    return
                if value.get("confirmations") == 0 and value.get("slot"):
                    return
            await sleep(self.confirm_wait)
        raise RpcError(f"нет подтверждения {signature}")


def budget_and_tip(
    instructions: list[Instruction],
    payer: Pubkey,
    *,
    jito_enabled: bool,
    tip_lamports: int,
) -> tuple[list[Instruction], int]:
    """ComputeBudget в начало, чаевые Jito в конец. Возвращает чаевые в лампортах."""
    head = [
        set_compute_unit_limit(COMPUTE_UNIT_LIMIT),
        set_compute_unit_price(COMPUTE_UNIT_PRICE),
    ]
    tip = 0
    tail: list[Instruction] = []
    if jito_enabled:
        if tip_lamports <= 0:
            raise LiveClosed("Jito включён, но tip_lamports не задан")
        tail.append(transfer(TransferParams(
            from_pubkey=payer, to_pubkey=JITO_TIP_ACCOUNT, lamports=tip_lamports,
        )))
        tip = tip_lamports
    return head + instructions + tail, tip


def token_program_from_mint(info: AccountInfo) -> Pubkey:
    if info.owner not in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
        raise LiveClosed(f"mint принадлежит неизвестной программе {info.owner}")
    return info.owner

