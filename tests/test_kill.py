"""Kill-switch: файл на диске закрывает новые покупки, выходы оставляет."""

from src.kill import DEFAULT_KILL_FILE, ENV_KILL_FILE, is_killed, kill_file_path
from src.models import Config


def test_default_kill_path_is_cwd_kill():
    assert kill_file_path(env={}) == __import__("pathlib").Path(DEFAULT_KILL_FILE)
    assert ENV_KILL_FILE == "GROKBOT_KILL_FILE"


def test_env_overrides_kill_path(tmp_path):
    target = tmp_path / "STOP"
    assert kill_file_path(env={ENV_KILL_FILE: str(target)}) == target
    assert not is_killed(env={ENV_KILL_FILE: str(target)})
    target.write_text("stop")
    assert is_killed(env={ENV_KILL_FILE: str(target)})


def test_empty_env_falls_back_to_default():
    assert kill_file_path(env={ENV_KILL_FILE: ""}) == __import__("pathlib").Path(DEFAULT_KILL_FILE)


def test_default_config_is_still_dry_run():
    assert Config().mode == "dry-run"
    assert not Config().is_live


def test_atlas_config_is_dry_run_with_caps():
    cfg = Config.load("config.atlas.yaml", env={})
    assert cfg.mode == "dry-run"
    assert not cfg.is_live
    assert cfg.risk.max_open_positions == 1
    assert cfg.risk.max_sol_per_trade == 0.05
    assert cfg.risk.daily_loss_limit_sol == 0.1
    assert cfg.risk.max_total_exposure_sol == 0.05
    assert cfg.risk.max_trades_per_day == 10
    assert cfg.ops.health_port == 8080
    assert cfg.ops.health_host == "127.0.0.1"
    assert cfg.filter.require_metadata is True
    errors, warnings = cfg.problems()
    assert not any("grok.api_key" in e for e in errors)
    assert any("grok.api_key" in w for w in warnings)
    assert cfg.ops.grok_entry_veto is False
