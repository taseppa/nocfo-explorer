"""Standalone configuration for nocfo-explorer.

The tool is not tied to the directory it runs from.  It looks for
``config.toml`` in this order and uses the first one it finds:

1. the path passed with ``--config``
2. the ``NOCFO_EXPLORER_CONFIG`` environment variable
3. the current working directory
4. ``~/.config/nocfo-explorer/config.toml``
5. the repository root (so a checked-out ``.venv`` works from anywhere)

The file holds a single ``[nocfo]`` section (``base_url``, ``token``,
``business_slug``) — same shape as nocfo-sync's shared section, but it is its
own file so the two tools stay independent.  ``NOCFO_TOKEN`` and
``NOCFO_BUSINESS`` override the token/business for e.g. CI or testing.

The first-run wizard saves through :func:`save_config`, which defaults to
``~/.config/nocfo-explorer/config.toml``.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_BASE_URL = "https://api.nocfo.io"


class ConfigError(Exception):
    pass


@dataclass
class Config:
    base_url: str = DEFAULT_BASE_URL
    token: str = ""
    business_slug: str = ""

    path: Path | None = None  # file this config was loaded from

    @property
    def configured(self) -> bool:
        """True when there is enough to talk to NoCFO without a wizard."""
        return bool(self.token and self.business_slug)

    @property
    def default_path(self) -> Path:
        """Where the wizard writes when no config file exists yet."""
        return Path.home() / ".config" / "nocfo-explorer" / "config.toml"

    def missing(self) -> list[str]:
        """Which wizard steps are still needed, in wizard order."""
        out: list[str] = []
        if not self.token:
            out.append("token")
        if not self.business_slug:
            out.append("business")
        return out


def _search_dirs() -> list[Path]:
    """Directories searched for ``config.toml``, in order (de-duplicated)."""
    dirs = [
        Path.cwd(),
        Path.home() / ".config" / "nocfo-explorer",
        # src/nocfo_explorer/config.py -> [0] nocfo_explorer, [1] src, [2] repo
        Path(__file__).resolve().parents[2],
    ]
    seen: set[Path] = set()
    out: list[Path] = []
    for d in dirs:
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out


def find_config(explicit: str | Path | None = None) -> Path | None:
    """Return the config.toml to use, or None if none exists."""
    if explicit is not None:
        path = Path(explicit).expanduser()
        if not path.exists():
            raise ConfigError(f"config file not found: {path}")
        return path
    env = os.environ.get("NOCFO_EXPLORER_CONFIG")
    if env:
        path = Path(env).expanduser()
        if not path.exists():
            raise ConfigError(f"NOCFO_EXPLORER_CONFIG points at a missing file: {path}")
        return path
    for d in _search_dirs():
        candidate = d / "config.toml"
        if candidate.exists():
            return candidate
    return None


def load_config(explicit: str | Path | None = None) -> Config:
    """Load ``config.toml`` from the search order (see module docstring)."""
    cfg = Config()
    path = find_config(explicit)
    if path is not None:
        try:
            data = tomllib.loads(path.read_text())
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"{path}: invalid TOML: {e}") from e
        nocfo = data.get("nocfo", {})
        cfg.base_url = str(nocfo.get("base_url", cfg.base_url)).rstrip("/")
        cfg.token = str(nocfo.get("token", cfg.token))
        cfg.business_slug = str(nocfo.get("business_slug", cfg.business_slug))
        cfg.path = path

    # env overrides (handy for CI / testing)
    if os.environ.get("NOCFO_TOKEN"):
        cfg.token = os.environ["NOCFO_TOKEN"]
    if os.environ.get("NOCFO_BUSINESS"):
        cfg.business_slug = os.environ["NOCFO_BUSINESS"]
    return cfg


def _toml_str(value: str) -> str:
    """A quoted TOML basic string (escapes backslashes and quotes)."""
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def save_config(cfg: Config, path: str | Path | None = None) -> Path:
    """Persist the config as TOML.

    Writes to ``path`` if given, else the file it was loaded from, else the
    default location (``~/.config/nocfo-explorer/config.toml``).
    """
    target = Path(path).expanduser() if path else (cfg.path or cfg.default_path)
    lines = [
        "[nocfo]",
        f"base_url = {_toml_str(cfg.base_url)}",
        f"token = {_toml_str(cfg.token)}",
        f"business_slug = {_toml_str(cfg.business_slug)}",
    ]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines) + "\n")
    cfg.path = target
    return target
