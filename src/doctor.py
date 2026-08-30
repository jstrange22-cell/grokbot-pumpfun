"""Предполётная проверка: заработает ли бот, если его сейчас запустить.

Половина неудачных запусков — не про логику, а про окружение: ключ
просрочен, провайдер данных отвечает 403, сокет не открывается из этой
сети, каталог состояния не пишется. Всё это выясняется через час
молчаливой работы вхолостую, а могло бы за десять секунд до запуска.

Каждая проверка возвращает одно из трёх: ok — работает; warn — работать
будет, но хуже, чем могло бы; fail — не заработает. Ни одна из них не
торгует и не тратит токены модели: проверяется доступность, а не ответы.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import websockets

from .curve import sanity_check
from .kill import is_killed, kill_file_path
from .models import Config, is_placeholder, mask
from .monitor import data_socket_url
from .state import InstanceLock

log = logging.getLogger(__name__)

OK, WARN, FAIL = "ok", "warn", "fail"
MARKS = {OK: "✓", WARN: "!", FAIL: "✗"}

# Свободного места меньше — лог и состояние скоро упрутся в диск.
MIN_FREE_MB = 200


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""
    hint: str = ""

    def line(self) -> str:
        text = f"  {MARKS[self.status]} {self.name}"
        if self.detail:
            text += f": {self.detail}"
        if self.hint and self.status != OK:
            text += f"\n      → {self.hint}"
        return text


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, *checks: Check) -> None:
        self.checks.extend(checks)

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if c.status == FAIL]

    @property
    def warned(self) -> list[Check]:
        return [c for c in self.checks if c.status == WARN]

    def render(self) -> str:
        lines = [check.line() for check in self.checks]
        lines.append("")
        if self.failed:
            lines.append(f"  Не заработает: провалено проверок — {len(self.failed)}.")
        elif self.warned:
            lines.append(f"  Заработает, но есть замечания ({len(self.warned)}).")
        else:
            lines.append("  Всё на месте, можно запускать.")
        return "\n".join(lines)


# --------------------------------------------------------------------------
# Отдельные проверки
# --------------------------------------------------------------------------


def check_config(config: Config) -> list[Check]:
    errors, warnings = config.problems()
    checks = [
        Check("конфиг", FAIL if errors else OK,
              "; ".join(errors) if errors else f"режим {config.mode}",
              "исправьте перечисленное и запустите проверку снова")
    ]
    checks += [Check("настройка", WARN, warning) for warning in warnings]
    return checks


def check_paths(config: Config) -> list[Check]:
    checks: list[Check] = []
    for name, raw in (("лог", config.logging.path), ("состояние", config.ops.state_path),
                      ("книга репутации", config.ops.reputation_path)):
        path = Path(raw)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            probe = path.parent / f".doctor{os.getpid()}"
            probe.write_text("x")
            probe.unlink()
            checks.append(Check(f"{name} пишется", OK, str(path.parent)))
        except OSError as exc:
            checks.append(Check(f"{name} пишется", FAIL, str(exc),
                                f"дайте процессу права на {path.parent}"))

    lock = InstanceLock(config.ops.state_path)
    holder = lock._holder()
    if holder and lock._alive(holder) and holder != os.getpid():
        checks.append(Check("состояние свободно", FAIL, f"занято процессом {holder}",
                            "уже запущен другой бот на этом же состоянии"))
    else:
        checks.append(Check("состояние свободно", OK))

    free_mb = shutil.disk_usage(Path(config.logging.path).parent.resolve()).free / 1e6
    checks.append(Check(
        "место на диске",
        OK if free_mb >= MIN_FREE_MB else WARN,
        f"{free_mb:.0f} МБ свободно",
        "лог растёт быстро; включите ротацию или освободите место",
    ))
    return checks


async def check_grok(config: Config, client: httpx.AsyncClient | None = None) -> Check:
    """Ключ и доступность API. Модель не вызывается — токены не тратятся."""
    url = config.grok.base_url.replace("/chat/completions", "/models")
    owns = client is None
    client = client or httpx.AsyncClient(timeout=10.0)
    try:
        response = await client.get(
            url, headers={"Authorization": f"Bearer {config.grok.key}"}
        )
        if response.status_code in (401, 403):
            return Check("Grok API", FAIL, f"ключ отвергнут (HTTP {response.status_code})",
                         f"проверьте GROKBOT_GROK_API_KEY, сейчас {mask(config.grok.key)}")
        if response.status_code >= 500:
            return Check("Grok API", WARN, f"сервис отвечает {response.status_code}",
                         "похоже на сбой на стороне xAI, повторите позже")
        if response.status_code >= 400:
            return Check("Grok API", WARN, f"неожиданный ответ {response.status_code}")
        models = _model_names(response)
        missing = [m for m in (config.grok.fast_model, config.grok.checker_model)
                   if models and m not in models]
        if missing:
            return Check("Grok API", WARN, f"ключ принят, но моделей нет в списке: {missing}",
                         "сверьте grok.fast_model и grok.checker_model с доступными")
        return Check("Grok API", OK, f"ключ принят, {mask(config.grok.key)}")
    except Exception as exc:
        return Check("Grok API", FAIL, f"недоступен: {exc}",
                     "проверьте сеть и прокси")
    finally:
        if owns:
            await client.aclose()


def _model_names(response: httpx.Response) -> list[str]:
    try:
        body = response.json()
    except (json.JSONDecodeError, ValueError):
        return []
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, list):
        return []
    return [item.get("id", "") for item in data if isinstance(item, dict)]


async def check_data_api(config: Config, client: httpx.AsyncClient | None = None) -> Check:
    owns = client is None
    client = client or httpx.AsyncClient(timeout=10.0)
    try:
        response = await client.get(config.data.rest_url)
        if response.status_code >= 500:
            return Check("провайдер данных", WARN,
                         f"отвечает {response.status_code}",
                         "без него не считаются метрики: проверьте data.rest_url")
        return Check("провайдер данных", OK,
                     f"{config.data.rest_url} отвечает {response.status_code}")
    except Exception as exc:
        return Check("провайдер данных", FAIL, f"недоступен: {exc}",
                     "проверьте data.rest_url и сеть")
    finally:
        if owns:
            await client.aclose()


async def check_socket(config: Config, wait_seconds: float = 15.0) -> Check:
    """Открывается ли поток лончей и идут ли по нему события."""
    try:
        async with websockets.connect(data_socket_url(config), open_timeout=wait_seconds) as ws:
            await ws.send(json.dumps({"method": "subscribeNewToken"}))
            started = time.monotonic()
            while True:
                left = wait_seconds - (time.monotonic() - started)
                if left <= 0:
                    break
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=left)
                except TimeoutError:
                    break
                payload = json.loads(raw) if raw else {}
                if isinstance(payload, dict) and payload.get("mint"):
                    return Check("поток лончей", OK, "события идут")
            # Подключились, но тишина — это не сломанный сокет, а тихая ночь.
            return Check("поток лончей", WARN, "подключились, но событий не дождались",
                         "ночью поток бывает редким; повторите проверку днём")
    except Exception as exc:
        return Check("поток лончей", FAIL, f"не открылся: {exc}",
                     "проверьте data.ws_url и то, что сеть пропускает websocket")


async def check_rpc(config: Config, client: httpx.AsyncClient | None = None) -> Check:
    """RPC нужен только в live, но проверить его лучше заранее."""
    owns = client is None
    client = client or httpx.AsyncClient(timeout=10.0)
    try:
        response = await client.post(
            config.solana.rpc_url,
            json={"jsonrpc": "2.0", "id": 1, "method": "getHealth"},
        )
        body = response.json() if response.status_code < 400 else {}
        healthy = body.get("result") == "ok"
        status = OK if healthy else (WARN if config.is_live else OK)
        detail = "здоров" if healthy else f"ответ {response.status_code}: {body or 'пусто'}"
        return Check("Solana RPC", status, detail,
                     "в live это критично: возьмите платный RPC")
    except Exception as exc:
        return Check("Solana RPC", FAIL if config.is_live else WARN,
                     f"недоступен: {exc}",
                     "в dry-run не нужен, в live обязателен")
    finally:
        if owns:
            await client.aclose()


def check_live_readiness(config: Config) -> list[Check]:
    if not config.is_live:
        return [Check("режим", OK, "dry-run: транзакции не отправляются")]
    checks = [Check("режим", WARN, "live: транзакции будут отправлены по-настоящему")]
    if is_placeholder(config.solana.wallet_key):
        checks.append(Check(
            "кошелёк", FAIL, "mode: live без ключа кошелька",
            "задайте GROKBOT_WALLET_PRIVATE_KEY или верните mode: dry-run",
        ))
    else:
        checks.append(Check("кошелёк", OK, mask(config.solana.wallet_key)))
    return checks


def check_kill_switch() -> Check:
    path = kill_file_path()
    if is_killed():
        return Check(
            "kill-switch", WARN, f"файл {path} есть — новые покупки закрыты",
            "уберите файл, когда снова можно покупать; выходы из позиций продолжают работать",
        )
    return Check("kill-switch", OK, f"файла {path} нет")


def check_curve_constants() -> Check:
    """Числа кривой должны оставаться правдоподобными: программа обновляется."""
    numbers = sanity_check()
    cap = numbers["max_sol_for_3pct"]
    round_trip = numbers["round_trip_0.5_sol"]
    if not (0.1 < cap < 10) or not (0 < round_trip < 10):
        return Check("константы кривой", WARN,
                     f"подозрительные числа: {numbers}",
                     "сверьте параметры программы pump.fun с ончейном")
    return Check("константы кривой", OK,
                 f"вход-выход 0.5 SOL ≈ {round_trip:.2f}%, "
                 f"потолок заявки при 3% ≈ {cap:.2f} SOL")


# --------------------------------------------------------------------------
# Всё вместе
# --------------------------------------------------------------------------


async def run_checks(config: Config, skip_network: bool = False) -> Report:
    report = Report()
    report.add(*check_config(config))
    report.add(*check_paths(config))
    report.add(check_curve_constants())
    report.add(check_kill_switch())
    report.add(*check_live_readiness(config))

    if skip_network:
        report.add(Check("сеть", WARN, "проверки сети пропущены (--offline)"))
        return report

    grok, data, rpc = await asyncio.gather(
        check_grok(config), check_data_api(config), check_rpc(config)
    )
    report.add(grok, data, rpc)
    report.add(await check_socket(config))
    return report


def summary(report: Report) -> dict[str, Any]:
    return {
        "ok": len([c for c in report.checks if c.status == OK]),
        "warn": len(report.warned),
        "fail": len(report.failed),
    }
