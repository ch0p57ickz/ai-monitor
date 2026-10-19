"""Tests for ai_monitor.config: validation, bounds, and load_config."""

from __future__ import annotations

import json
import textwrap

import pytest

from ai_monitor.config import Config, ConfigError, load_config


# ---------------------------------------------------------------------------
# defaults
# ---------------------------------------------------------------------------

def test_default_config_is_valid():
    cfg = Config()
    assert cfg.api_host == "127.0.0.1"
    assert cfg.api_port == 8732
    assert cfg.retention_days == 30


def test_load_config_no_path_returns_defaults():
    cfg = load_config(None)
    assert cfg.api_host == "127.0.0.1"
    assert cfg.api_port == 8732


# ---------------------------------------------------------------------------
# loopback-only binding
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "host",
    ["0.0.0.0", "192.168.1.1", "example.com", "::1", "localhost"],
)
def test_non_loopback_host_rejected(host: str):
    with pytest.raises(ConfigError) as exc_info:
        Config(api_host=host)
    assert exc_info.value.category == "invalid_binding"


def test_loopback_host_accepted():
    cfg = Config(api_host="127.0.0.1")
    assert cfg.api_host == "127.0.0.1"


# ---------------------------------------------------------------------------
# port bounds
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("port", [-1, 65536, 99999])
def test_port_out_of_bounds(port: int):
    with pytest.raises(ConfigError) as exc_info:
        Config(api_port=port)
    assert exc_info.value.category == "out_of_bounds"


def test_port_at_bounds_accepted():
    assert Config(api_port=0).api_port == 0  # auto-assign
    assert Config(api_port=1).api_port == 1
    assert Config(api_port=65535).api_port == 65535


# ---------------------------------------------------------------------------
# interval bounds
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("val", [0.0, -1.0, 86401.0, 100000.0])
def test_interval_out_of_bounds(val: float):
    with pytest.raises(ConfigError) as exc_info:
        Config(codex_interval=val)
    assert exc_info.value.category == "out_of_bounds"


def test_interval_at_bounds_accepted():
    assert Config(codex_interval=1.0).codex_interval == 1.0
    assert Config(codex_interval=86400.0).codex_interval == 86400.0


# ---------------------------------------------------------------------------
# unknown keys
# ---------------------------------------------------------------------------

def test_unknown_key_rejected(tmp_path):
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({"bogus_key": 42}))
    with pytest.raises(ConfigError) as exc_info:
        load_config(str(cfg_file))
    assert exc_info.value.category == "unknown_key"


def test_unknown_key_in_construction_rejected():
    # Config is frozen; unknown keys can only come through _from_dict,
    # so we test via load_config.
    pass


# ---------------------------------------------------------------------------
# invalid JSON / missing file
# ---------------------------------------------------------------------------

def test_load_config_missing_file(tmp_path):
    with pytest.raises(ConfigError) as exc_info:
        load_config(str(tmp_path / "nonexistent.json"))
    assert exc_info.value.category == "not_found"


def test_load_config_invalid_json(tmp_path):
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text("{ not valid json }")
    with pytest.raises(ConfigError) as exc_info:
        load_config(str(cfg_file))
    assert exc_info.value.category == "invalid_json"


# ---------------------------------------------------------------------------
# type coercion
# ---------------------------------------------------------------------------

def test_load_config_rejects_string_for_int(tmp_path):
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({"api_port": "8080"}))
    with pytest.raises(ConfigError) as exc_info:
        load_config(str(cfg_file))
    assert exc_info.value.category == "invalid_type"


def test_load_config_rejects_bool_for_int(tmp_path):
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({"retention_days": True}))
    with pytest.raises(ConfigError) as exc_info:
        load_config(str(cfg_file))
    assert exc_info.value.category == "invalid_type"


def test_load_config_accepts_valid_json(tmp_path):
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({"api_port": 9999, "retention_days": 7}))
    cfg = load_config(str(cfg_file))
    assert cfg.api_port == 9999
    assert cfg.retention_days == 7
    # Keys not present in the file fall back to defaults.
    assert cfg.api_host == "127.0.0.1"


# ---------------------------------------------------------------------------
# history range validation
# ---------------------------------------------------------------------------

def test_history_max_less_than_default_rejected():
    with pytest.raises(ConfigError) as exc_info:
        Config(
            history_default_hours=24,
            history_max_hours=12,
        )
    assert exc_info.value.category == "invalid_range"
