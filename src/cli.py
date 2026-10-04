"""Единая точка входа: `grokbot <команда>`.

До этого модуля запуск, проверка, реплей, дашборд и подбор весов жили в
разных местах и вызывались по-разному. Одна команда с подкомандами — это
не украшение: в runbook и в unit-файле должно стоять что-то одно, что
человек вспомнит через месяц.

    grokbot run                # торговый цикл (dry-run по умолчанию)
    grokbot check              # проверить конфиг и выйти
    grokbot doctor             # предполётная проверка окружения
    grokbot replay [лог]       # сводка по логу
    grokbot dashboard [лог]    # живое состояние
    grokbot tune [лог]         # подбор весов и порога
    grokbot curve              # числа кривой: комиссия, влияние, потолок

`python -m src.pipeline` продолжает работать: старые unit-файлы и cron не
должны ломаться из-за переезда команды.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import runpy
import sys
from pathlib import Path

from .curve import sanity_check
from .doctor import run_checks, summary
from .models import Config, ConfigError
from .pipeline import amain as run_pipeline

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="grokbot",
        description="Пайплайн pump.fun: механический вход, опциональное вето Grok",
    )
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="торговый цикл")
    run.add_argument("--config", default="config.yaml")
    run.add_argument("--i-understand-the-risk", action="store_true",
                     help="обязателен для запуска в режиме live")

    check = sub.add_parser("check", help="проверить конфиг и выйти")
    check.add_argument("--config", default="config.yaml")

    doctor = sub.add_parser("doctor", help="предполётная проверка окружения")
    doctor.add_argument("--config", default="config.yaml")
    doctor.add_argument("--offline", action="store_true", help="без сетевых проверок")
    doctor.add_argument("--json", action="store_true", help="машиночитаемый вывод")

    for name, help_text in (("replay", "сводка по логу"),
                            ("dashboard", "живое состояние"),
                            ("tune", "подбор весов и порога")):
        script = sub.add_parser(name, help=help_text)
        script.add_argument("args", nargs=argparse.REMAINDER)

    sub.add_parser("curve", help="числа кривой: комиссия, влияние, потолок заявки")
    return parser


def load(config_path: str) -> Config:
    path = Path(config_path)
    if not path.exists():
        raise SystemExit(f"Конфига {path} нет. Скопируйте config.example.yaml в config.yaml.")
    try:
        return Config.load(path)
    except Exception as exc:
        raise SystemExit(f"Конфиг {path} не читается: {exc}") from exc


def cmd_doctor(args: argparse.Namespace) -> int:
    config = load(args.config)
    report = asyncio.run(run_checks(config, skip_network=args.offline))

    if args.json:
        print(json.dumps({
            "summary": summary(report),
            "checks": [{"name": c.name, "status": c.status, "detail": c.detail}
                       for c in report.checks],
        }, ensure_ascii=False, indent=2))
    else:
        print()
        print("  ПРЕДПОЛЁТНАЯ ПРОВЕРКА")
        print("  " + "─" * 58)
        print(report.render())
        print()
    return 1 if report.failed else 0


def cmd_check(args: argparse.Namespace) -> int:
    config = load(args.config)
    try:
        warnings = config.check_ready()
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    for warning in warnings:
        print(f"ВНИМАНИЕ: {warning}", file=sys.stderr)
    print(json.dumps(config.redacted(), ensure_ascii=False, indent=2))
    print("\nКонфиг пригоден для запуска.", file=sys.stderr)
    return 0


def cmd_curve() -> int:
    numbers = sanity_check()
    print("\n  Кривая pump.fun: во что обходится сделка на свежем токене\n")
    print(f"    цена в начале кривой         {numbers['spot_price']:.12f} SOL")
    print(f"    заявка 0.5 SOL двигает цену  {numbers['impact_0.5_sol']:.2f} %")
    print(f"    вход и выход 0.5 SOL стоят   {numbers['round_trip_0.5_sol']:.2f} %")
    print(f"    потолок заявки при 3%        {numbers['max_sol_for_3pct']:.3f} SOL")
    print(f"    за 1 SOL дают токенов        {numbers['tokens_for_1_sol']:,.0f}")
    print("\n  Константы взяты из программы pump.fun и могут устареть:")
    print("  перед включением live сверьте их с ончейном.\n")
    return 0


def run_script(name: str, args: list[str]) -> int:
    """Запустить скрипт из scripts/ так, будто его вызвали напрямую."""
    script = SCRIPTS / f"{name}.py"
    if not script.exists():
        raise SystemExit(f"Скрипт {script} не найден")
    sys.argv = [str(script), *args]
    try:
        runpy.run_path(str(script), run_name="__main__")
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    command = args.command or "run"

    if command == "run":
        run_args = ["--config", getattr(args, "config", "config.yaml")]
        if getattr(args, "i_understand_the_risk", False):
            run_args.append("--i-understand-the-risk")
        return asyncio.run(run_pipeline(run_args))
    if command == "check":
        return cmd_check(args)
    if command == "doctor":
        return cmd_doctor(args)
    if command == "curve":
        return cmd_curve()
    if command in ("replay", "dashboard", "tune"):
        return run_script(command, [a for a in args.args if a != "--"])

    build_parser().print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
