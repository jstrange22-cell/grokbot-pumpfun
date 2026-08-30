"""Сборка инструкций и разбор аккаунтов — без сети."""

import struct

import pytest
from solders.hash import Hash
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.signature import Signature

from src.onchain import (
    BUY_DISCRIMINATOR,
    DEFAULT_SLIPPAGE,
    FEE_PROGRAM,
    LAMPORTS_PER_SOL,
    PUMP_PROGRAM,
    SELL_DISCRIMINATOR,
    TOKEN_PROGRAM,
    WSOL_MINT,
    Accounts,
    LiveClosed,
    b58encode,
    budget_and_tip,
    build_buy_instruction,
    build_sell_instruction,
    derive_accounts,
    encode_buy,
    encode_sell,
    load_keypair,
    parse_bonding_curve,
    parse_fee_recipient,
    pubkey_from_str,
    sign_transaction,
    sol_to_lamports,
    tokens_to_raw,
)


def test_default_slippage_is_two_percent():
    assert pytest.approx(0.02) == DEFAULT_SLIPPAGE


def test_load_keypair_rejects_placeholder_and_mnemonic():
    with pytest.raises(LiveClosed, match="нет ключа"):
        load_keypair("")
    with pytest.raises(LiveClosed, match="нет ключа"):
        load_keypair("YOUR-BASE58-PRIVATE-KEY")
    with pytest.raises(LiveClosed, match="seed-фраз"):
        load_keypair(
            "abandon abandon abandon abandon abandon abandon "
            "abandon abandon abandon abandon abandon about"
        )


def test_load_keypair_accepts_json_and_base58():
    kp = Keypair()
    assert load_keypair(kp.to_json()).pubkey() == kp.pubkey()
    b58 = str(Signature.from_bytes(bytes(kp)))
    assert load_keypair(b58).pubkey() == kp.pubkey()


def test_tokens_and_sol_refuse_zero():
    with pytest.raises(LiveClosed):
        tokens_to_raw(0)
    with pytest.raises(LiveClosed):
        sol_to_lamports(0)
    assert tokens_to_raw(1.5) == 1_500_000
    assert sol_to_lamports(0.05) == 50_000_000


def test_encode_buy_and_sell_layout():
    buy = encode_buy(100, 2_000_000_000)
    assert buy[:8] == BUY_DISCRIMINATOR
    assert struct.unpack_from("<Q", buy, 8)[0] == 100
    assert struct.unpack_from("<Q", buy, 16)[0] == 2_000_000_000
    assert buy[24] == 0
    sell = encode_sell(50, 1_000)
    assert sell[:8] == SELL_DISCRIMINATOR
    assert len(sell) == 24


def _curve_bytes(creator: Pubkey, *, complete: bool = False, mayhem: bool = False,
                 quote: Pubkey | None = None) -> bytes:
    data = bytearray(120)
    data[48] = 1 if complete else 0
    data[49:81] = bytes(creator)
    data[81] = 1 if mayhem else 0
    data[83:115] = bytes(quote or Pubkey.default())
    return bytes(data)


def test_parse_bonding_curve_reads_creator_and_refuses_non_sol():
    creator = Keypair().pubkey()
    parsed = parse_bonding_curve(_curve_bytes(creator))
    assert parsed.creator == creator
    assert not parsed.complete
    assert parsed.virtual_sol_reserves == 0
    with pytest.raises(LiveClosed, match="не в SOL"):
        parse_bonding_curve(_curve_bytes(creator, quote=Keypair().pubkey()))
    wsol = parse_bonding_curve(_curve_bytes(creator, quote=WSOL_MINT))
    assert wsol.quote_mint == WSOL_MINT


def test_parse_bonding_curve_reads_virtual_reserves():
    creator = Keypair().pubkey()
    data = bytearray(_curve_bytes(creator))
    struct.pack_into("<Q", data, 8, 715_333_460_666_667)
    struct.pack_into("<Q", data, 16, 45_000_000_000)
    parsed = parse_bonding_curve(bytes(data))
    assert parsed.virtual_sol_reserves == 45_000_000_000
    assert parsed.virtual_token_reserves == 715_333_460_666_667


def test_parse_fee_recipient_fails_closed():
    with pytest.raises(LiveClosed, match="короче"):
        parse_fee_recipient(b"short")
    fee = Keypair().pubkey()
    reserved = Keypair().pubkey()
    blob = bytearray(520)
    blob[41:73] = bytes(fee)
    blob[483:515] = bytes(reserved)
    assert parse_fee_recipient(bytes(blob)) == fee
    assert parse_fee_recipient(bytes(blob), mayhem=True) == reserved
    with pytest.raises(LiveClosed, match="пустой"):
        parse_fee_recipient(bytes(520))


def test_derive_accounts_are_deterministic():
    mint = Keypair().pubkey()
    user = Keypair().pubkey()
    creator = Keypair().pubkey()
    fee = Keypair().pubkey()
    a = derive_accounts(mint, user, creator, fee, TOKEN_PROGRAM)
    b = derive_accounts(mint, user, creator, fee, TOKEN_PROGRAM)
    assert a == b
    assert a.global_account == Pubkey.from_string("4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf")
    assert a.event_authority == Pubkey.from_string("Ce6TQqeHC9p8KetsN6JsjHK7UTZk7nasjjnr7XxXp9F1")


def test_buy_and_sell_account_order_differs():
    acc = derive_accounts(
        Keypair().pubkey(), Keypair().pubkey(), Keypair().pubkey(),
        Keypair().pubkey(), TOKEN_PROGRAM,
    )
    buy = build_buy_instruction(acc, 1, 2)
    sell = build_sell_instruction(acc, 1, 2)
    assert buy.program_id == PUMP_PROGRAM
    assert sell.program_id == PUMP_PROGRAM
    assert len(buy.accounts) == 16
    assert len(sell.accounts) == 14
    # buy: token_program затем creator_vault; sell — наоборот
    assert buy.accounts[8].pubkey == TOKEN_PROGRAM
    assert buy.accounts[9].pubkey == acc.creator_vault
    assert sell.accounts[8].pubkey == acc.creator_vault
    assert sell.accounts[9].pubkey == TOKEN_PROGRAM
    assert buy.accounts[15].pubkey == FEE_PROGRAM


def test_jito_tip_requires_positive_lamports():
    acc = derive_accounts(
        Keypair().pubkey(), Keypair().pubkey(), Keypair().pubkey(),
        Keypair().pubkey(), TOKEN_PROGRAM,
    )
    with pytest.raises(LiveClosed, match="tip_lamports"):
        budget_and_tip([build_buy_instruction(acc, 1, 2)], acc.user,
                       jito_enabled=True, tip_lamports=0)


def test_sign_transaction_and_b58():
    kp = Keypair()
    acc = derive_accounts(
        Keypair().pubkey(), kp.pubkey(), Keypair().pubkey(),
        Keypair().pubkey(), TOKEN_PROGRAM,
    )
    ixs, tip = budget_and_tip(
        [build_buy_instruction(acc, 10, 1_000_000)],
        kp.pubkey(), jito_enabled=True, tip_lamports=1_000,
    )
    assert tip == 1_000
    tx = sign_transaction(kp, ixs, Hash.default())
    assert tx.signatures
    encoded = b58encode(bytes(tx))
    assert encoded
    assert encoded[0] in "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def test_pubkey_from_str_fails_closed():
    with pytest.raises(LiveClosed, match="не pubkey"):
        pubkey_from_str("Mint1", "mint")


def test_accounts_type_is_exported():
    assert Accounts.__name__ == "Accounts"
    assert LAMPORTS_PER_SOL == 1_000_000_000
