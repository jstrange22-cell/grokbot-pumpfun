"""Предполётная проверка. Ни одна проверка не должна сама ходить в сеть
в тестах — весь транспорт подменён."""

import asyncio
import json

import httpx
import pytest

from src.doctor import (
    FAIL,
    OK,
    WARN,
    Check,
    Report,
    check_config,
    check_curve_constants,
    check_data_api,
    check_grok,
    check_kill_switch,
    check_live_readiness,
    check_paths,
    check_rpc,
    check_socket,
    run_checks,
    summary,
)
from src.models import Config


@pytest.fixture
def config(tmp_path) -> Config:
    cfg = Config()
    cfg.grok.api_key = "xai-doctor-key-1234567890"
    cfg.logging.path = str(tmp_path / "logs" / "trades.jsonl")
    cfg.ops.state_path = str(tmp_path / "state" / "pipeline.json")
    cfg.ops.reputation_path = str(tmp_path / "state" / "creators.json")
    return cfg


def client(status: int, payload: dict | list | None = None) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload if payload is not None else {})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def failing_client(exc: Exception) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# --- конфиг и пути --------------------------------------------------------


def test_good_config_passes(config):
    statuses = [c.status for c in check_config(config)]
    assert statuses[0] == OK


def test_placeholder_key_fails():
    checks = check_config(Config())
    assert checks[0].status == FAIL
    assert "api_key" in checks[0].detail


def test_warnings_do_not_fail(config):
    config.filter.min_total_score = 0.1
    checks = check_config(config)
    assert checks[0].status == OK
    assert any(c.status == WARN for c in checks[1:])


def test_paths_are_probed(config):
    checks = check_paths(config)
    assert all(c.status == OK for c in checks if "пишется" in c.name)


def test_unwritable_path_fails(config):
    config.ops.state_path = "/несуществующий-корень/state.json"
    checks = check_paths(config)
    assert any(c.status == FAIL and "состояние" in c.name for c in checks)


# --- Grok -----------------------------------------------------------------


async def test_grok_ok(config):
    models = {"data": [{"id": "grok-4-fast"}, {"id": "grok-4"}]}
    check = await check_grok(config, client(200, models))
    assert check.status == OK
    assert "xai-doct" not in check.detail          # ключ замаскирован


async def test_grok_rejected_key_fails(config):
    check = await check_grok(config, client(401))
    assert check.status == FAIL
    assert "отвергнут" in check.detail


async def test_grok_missing_model_warns(config):
    check = await check_grok(config, client(200, {"data": [{"id": "grok-2"}]}))
    assert check.status == WARN
    assert "grok-4" in check.detail


async def test_grok_server_error_warns(config):
    assert (await check_grok(config, client(503))).status == WARN


async def test_grok_unreachable_fails(config):
    check = await check_grok(config, failing_client(httpx.ConnectError("сети нет")))
    assert check.status == FAIL


async def test_grok_check_does_not_call_the_model(config):
    """Проверка не должна тратить токены: только GET списка моделей."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": []})

    await check_grok(config, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert seen[0].method == "GET"
    assert "chat/completions" not in str(seen[0].url)


# --- провайдер данных и RPC ----------------------------------------------


async def test_data_api_ok(config):
    assert (await check_data_api(config, client(200))).status == OK


async def test_data_api_unreachable_fails(config):
    check = await check_data_api(config, failing_client(httpx.ConnectError("нет")))
    assert check.status == FAIL


async def test_rpc_healthy(config):
    assert (await check_rpc(config, client(200, {"result": "ok"}))).status == OK


async def test_rpc_missing_is_only_warning_in_dry_run(config):
    check = await check_rpc(config, failing_client(httpx.ConnectError("нет")))
    assert check.status == WARN


async def test_rpc_missing_fails_in_live(config):
    config.mode = "live"
    check = await check_rpc(config, failing_client(httpx.ConnectError("нет")))
    assert check.status == FAIL


# --- сокет ----------------------------------------------------------------


class FakeSocket:
    def __init__(self, messages: list[dict]) -> None:
        self.messages = [json.dumps(m) for m in messages]
        self.sent: list[str] = []

    async def send(self, raw: str) -> None:
        self.sent.append(raw)

    async def recv(self) -> str:
        if self.messages:
            return self.messages.pop(0)
        await asyncio.sleep(10)
        raise AssertionError("недостижимо")

    async def __aenter__(self) -> "FakeSocket":
        return self

    async def __aexit__(self, *exc) -> bool:
        return False


async def test_socket_ok(config, monkeypatch):
    import src.doctor as doctor_module

    socket = FakeSocket([{"txType": "create", "mint": "Abc"}])
    monkeypatch.setattr(doctor_module.websockets, "connect", lambda *a, **k: socket)
    check = await check_socket(config, wait_seconds=1.0)
    assert check.status == OK
    assert "subscribeNewToken" in socket.sent[0]


async def test_socket_silence_warns(config, monkeypatch):
    import src.doctor as doctor_module

    monkeypatch.setattr(doctor_module.websockets, "connect", lambda *a, **k: FakeSocket([]))
    check = await check_socket(config, wait_seconds=0.05)
    assert check.status == WARN


async def test_socket_failure_fails(config, monkeypatch):
    import src.doctor as doctor_module

    def boom(*args, **kwargs):
        raise OSError("сеть режет websocket")

    monkeypatch.setattr(doctor_module.websockets, "connect", boom)
    assert (await check_socket(config, wait_seconds=0.1)).status == FAIL


# --- режим и константы ----------------------------------------------------


def test_dry_run_mode_is_ok(config):
    assert check_live_readiness(config)[0].status == OK


def test_live_mode_warns_and_requires_a_wallet(config):
    """Live больше не заглушка, но без ключа кошелька doctor всё равно откажет."""
    config.mode = "live"
    checks = check_live_readiness(config)
    assert checks[0].status == WARN
    assert any(c.status == FAIL and "кошелька" in c.detail for c in checks)
    config.solana.wallet_private_key = "5xРеальныйКлючДляДоктора"
    ready = check_live_readiness(config)
    assert ready[0].status == WARN
    assert all(c.status != FAIL for c in ready)


def test_curve_constants_look_sane():
    assert check_curve_constants().status == OK


def test_kill_switch_check_warns_when_file_exists(tmp_path, monkeypatch):
    from src.kill import ENV_KILL_FILE

    path = tmp_path / "KILL"
    monkeypatch.setenv(ENV_KILL_FILE, str(path))
    assert check_kill_switch().status == OK
    path.write_text("stop")
    check = check_kill_switch()
    assert check.status == WARN
    assert "KILL" in check.detail


# --- отчёт ----------------------------------------------------------------


def test_report_renders_and_counts():
    report = Report()
    report.add(Check("раз", OK), Check("два", WARN, "мелочь"),
               Check("три", FAIL, "беда", "почините"))
    text = report.render()
    assert "✓ раз" in text and "! два" in text and "✗ три" in text
    assert "почините" in text
    assert "Не заработает" in text
    assert summary(report) == {"ok": 1, "warn": 1, "fail": 1}


def test_clean_report_says_so():
    report = Report()
    report.add(Check("раз", OK))
    assert "можно запускать" in report.render()


async def test_offline_run_skips_network(config):
    report = await run_checks(config, skip_network=True)
    names = [c.name for c in report.checks]
    assert "Grok API" not in names
    assert any("сеть" in name for name in names)
