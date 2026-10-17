"""Config discovery, overrides and persistence."""

from __future__ import annotations

from pathlib import Path

import pytest

from nocfo_explorer.config import (
    Config,
    ConfigError,
    find_config,
    load_config,
    save_config,
)

GOOD = """
[nocfo]
base_url = "https://api-tst.nocfo.io"
token = "tok-from-file"
business_slug = "acme"
"""


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """No ambient config/env leaks into a test (cwd + HOME point at tmp)."""
    for var in ("NOCFO_EXPLORER_CONFIG", "NOCFO_TOKEN", "NOCFO_BUSINESS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


def test_load_without_any_config_is_unconfigured_defaults():
    cfg = load_config()
    assert cfg.configured is False
    assert cfg.missing() == ["token", "business"]
    assert cfg.base_url == "https://api.nocfo.io"
    assert cfg.path is None


def test_load_from_cwd(tmp_path: Path):
    (tmp_path / "config.toml").write_text(GOOD)
    cfg = load_config()
    assert cfg.token == "tok-from-file"
    assert cfg.business_slug == "acme"
    assert cfg.base_url == "https://api-tst.nocfo.io"
    assert cfg.path == tmp_path / "config.toml"


def test_cwd_config_wins_over_home_config(tmp_path: Path):
    home = tmp_path / "home"
    (home / ".config" / "nocfo-explorer").mkdir(parents=True)
    (home / ".config" / "nocfo-explorer" / "config.toml").write_text(
        '[nocfo]\ntoken = "home-tok"\nbusiness_slug = "home"\n'
    )
    (tmp_path / "config.toml").write_text('[nocfo]\ntoken = "cwd-tok"\n')
    assert load_config().token == "cwd-tok"


def test_home_config_is_found_without_cwd_config(tmp_path: Path):
    home = tmp_path / "home"
    (home / ".config" / "nocfo-explorer").mkdir(parents=True)
    (home / ".config" / "nocfo-explorer" / "config.toml").write_text(GOOD)
    assert load_config().token == "tok-from-file"


def test_explicit_path(tmp_path: Path):
    cfg_file = tmp_path / "custom.toml"
    cfg_file.write_text(GOOD)
    assert load_config(cfg_file).token == "tok-from-file"
    assert load_config(str(cfg_file)).business_slug == "acme"


def test_explicit_missing_path_raises(tmp_path: Path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.toml")


def test_env_config_path_missing_file_raises(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOCFO_EXPLORER_CONFIG", str(tmp_path / "gone.toml"))
    with pytest.raises(ConfigError, match="missing file"):
        find_config()


def test_env_config_path_is_used(tmp_path: Path, monkeypatch):
    cfg_file = tmp_path / "elsewhere.toml"
    cfg_file.write_text(GOOD)
    (tmp_path / "config.toml").write_text('[nocfo]\ntoken = "cwd"\n')
    monkeypatch.setenv("NOCFO_EXPLORER_CONFIG", str(cfg_file))
    assert load_config().token == "tok-from-file"


def test_env_token_and_business_override_file(tmp_path: Path, monkeypatch):
    (tmp_path / "config.toml").write_text(GOOD)
    monkeypatch.setenv("NOCFO_TOKEN", "env-tok")
    monkeypatch.setenv("NOCFO_BUSINESS", "env-biz")
    cfg = load_config()
    assert cfg.token == "env-tok"
    assert cfg.business_slug == "env-biz"


def test_invalid_toml_raises_config_error(tmp_path: Path):
    (tmp_path / "config.toml").write_text("not [ valid toml")
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_config()


def test_base_url_trailing_slash_is_stripped(tmp_path: Path):
    (tmp_path / "config.toml").write_text('[nocfo]\nbase_url = "https://x.test/"\n')
    assert load_config().base_url == "https://x.test"


def test_save_defaults_to_home_config(tmp_path: Path):
    cfg = Config(token="t", business_slug="b")
    path = save_config(cfg)
    assert path == tmp_path / "home" / ".config" / "nocfo-explorer" / "config.toml"
    assert path.exists()
    reloaded = load_config(path)
    assert (reloaded.token, reloaded.business_slug) == ("t", "b")
    assert reloaded.path == path


def test_save_roundtrips_all_fields(tmp_path: Path):
    cfg = Config(
        base_url="https://api-tst.nocfo.io",
        token='we"ird\\tok',
        business_slug="acme",
    )
    out = save_config(cfg, tmp_path / "cfg.toml")
    reloaded = load_config(out)
    assert reloaded.base_url == "https://api-tst.nocfo.io"
    assert reloaded.token == 'we"ird\\tok'
    assert reloaded.business_slug == "acme"


def test_save_to_loaded_file_when_one_exists(tmp_path: Path):
    (tmp_path / "config.toml").write_text(GOOD)
    cfg = load_config()
    cfg.token = "updated"
    out = save_config(cfg)
    assert out == tmp_path / "config.toml"
    assert load_config().token == "updated"
