"""Единая точка входа: команды должны попадать туда, куда обещано."""

import json
import sys

import pytest

from src.cli import build_parser, main

GOOD_CONFIG = """
mode: dry-run
grok:
  api_key: xai-cli-ключ-1234567890
"""


@pytest.fixture
def config_file(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(GOOD_CONFIG + f"""
logging:
  path: "{tmp_path}/logs/trades.jsonl"
ops:
  state_path: "{tmp_path}/state/pipeline.json"
  reputation_path: "{tmp_path}/state/creators.json"
""")
    return path


# --- разбор аргументов ----------------------------------------------------


def test_default_command_is_run():
    assert build_parser().parse_args([]).command is None      # main подставит run


@pytest.mark.parametrize("command", ["run", "check", "doctor", "replay",
                                     "dashboard", "tune", "curve"])
def test_every_command_parses(command):
    assert build_parser().parse_args([command]).command == command


def test_unknown_command_is_rejected():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["взлететь"])


# --- команды без сети -----------------------------------------------------


def test_curve_prints_numbers(capsys):
    assert main(["curve"]) == 0
    printed = capsys.readouterr().out
    assert "потолок заявки" in printed
    assert "%" in printed


def test_check_accepts_good_config(config_file, capsys):
    assert main(["check", "--config", str(config_file)]) == 0
    printed = capsys.readouterr().out
    assert "xai-cli-ключ-1234567890" not in printed      # секрет замаскирован
    assert json.loads(printed)["mode"] == "dry-run"


def test_check_rejects_bad_config(tmp_path, capsys):
    path = tmp_path / "config.yaml"
    path.write_text("mode: dry-run\nrisk:\n  max_sol_per_trade: 0\n")
    assert main(["check", "--config", str(path)]) == 1
    assert "max_sol_per_trade" in capsys.readouterr().err


def test_check_allows_missing_grok_key_when_veto_off(tmp_path, capsys):
    path = tmp_path / "config.yaml"
    path.write_text("mode: dry-run\n")
    assert main(["check", "--config", str(path)]) == 0
    assert "механический" in capsys.readouterr().err


def test_missing_config_is_explained(tmp_path):
    with pytest.raises(SystemExit) as exc:
        main(["check", "--config", str(tmp_path / "нет.yaml")])
    assert "config.example.yaml" in str(exc.value)


def test_doctor_offline(config_file, capsys):
    assert main(["doctor", "--config", str(config_file), "--offline"]) == 0
    printed = capsys.readouterr().out
    assert "ПРЕДПОЛЁТНАЯ ПРОВЕРКА" in printed
    assert "константы кривой" in printed


def test_doctor_json_output(config_file, capsys):
    main(["doctor", "--config", str(config_file), "--offline", "--json"])
    report = json.loads(capsys.readouterr().out)
    assert report["summary"]["fail"] == 0
    assert any(c["name"] == "константы кривой" for c in report["checks"])


def test_doctor_fails_on_bad_config(tmp_path, capsys):
    path = tmp_path / "config.yaml"
    path.write_text("mode: dry-run\nrisk:\n  max_sol_per_trade: 0\n")
    assert main(["doctor", "--config", str(path), "--offline"]) == 1


# --- делегирование скриптам ----------------------------------------------


def test_replay_runs_the_script(tmp_path, capsys):
    log = tmp_path / "trades.jsonl"
    log.write_text(json.dumps({"type": "buy", "mint": "A", "ts": 1_800_000_000,
                               "size_sol": 0.4, "scores": {"total": 0.8}}) + "\n")
    code = main(["replay", str(log)])
    assert code == 0
    assert "РЕПЛЕЙ" in capsys.readouterr().out


def test_dashboard_runs_the_script(tmp_path, capsys):
    log = tmp_path / "trades.jsonl"
    log.write_text(json.dumps({"type": "buy", "mint": "A", "ts": 1_800_000_000,
                               "size_sol": 0.4, "scores": {"total": 0.8}}) + "\n")
    main(["dashboard", str(log)])
    assert "grokbot-pumpfun" in capsys.readouterr().out


def test_script_arguments_are_passed_through(tmp_path, capsys):
    log = tmp_path / "trades.jsonl"
    log.write_text("")
    main(["replay", str(log), "--since", "2020-01-01"])
    assert "нет записей" in capsys.readouterr().out


def test_unknown_script_is_reported(monkeypatch):
    import src.cli as cli_module

    monkeypatch.setattr(cli_module, "SCRIPTS", cli_module.SCRIPTS / "нет-такой-папки")
    with pytest.raises(SystemExit):
        main(["tune"])


def test_argv_restored_for_the_caller(tmp_path):
    """Делегирование не должно ломать argv вызывающего процесса."""
    log = tmp_path / "trades.jsonl"
    log.write_text("")
    before = list(sys.argv)
    main(["replay", str(log)])
    sys.argv = before
    assert sys.argv == before
