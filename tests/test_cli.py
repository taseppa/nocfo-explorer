"""CLI argument handling."""

from __future__ import annotations

import pytest

from nocfo_explorer import cli


def test_missing_config_file_exits_2():
    with pytest.raises(SystemExit) as e:
        cli.main(["--config", "/nonexistent/config.toml"])
    assert e.value.code == 2


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["--version"])
    assert e.value.code == 0
    assert "0.1.0" in capsys.readouterr().out


def test_business_flag_is_applied_and_app_runs(monkeypatch):
    captured: dict = {}

    class StubApp:
        def __init__(self, cfg):
            captured["cfg"] = cfg

        def run(self):
            captured["ran"] = True

    monkeypatch.setattr(cli, "ExplorerApp", StubApp)
    cli.main(["--business", "beta"])
    assert captured["cfg"].business_slug == "beta"
    assert captured["ran"] is True


def test_no_config_anywhere_starts_unconfigured_wizard_app(monkeypatch):
    captured: dict = {}

    class StubApp:
        def __init__(self, cfg):
            captured["cfg"] = cfg

        def run(self):
            captured["ran"] = True

    monkeypatch.setattr(cli, "ExplorerApp", StubApp)
    cli.main([])  # cwd + HOME are empty tmp dirs (conftest)
    assert captured["cfg"].configured is False
    assert captured["ran"] is True
