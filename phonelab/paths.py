"""Portable, non-secret local path configuration for PhoneLab."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
from typing import Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOCAL_CONFIG = ".phonelab-local.json"
_CONFIG_KEYS = {"data_root", "pwsh", "python_source"}


class PathConfigurationError(ValueError):
    """Local path configuration is malformed; do not silently pick another location."""


def load_local_config(project_root: Path | None = None) -> dict[str, str]:
    root = Path(project_root or PROJECT_ROOT)
    config_path = root / LOCAL_CONFIG
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PathConfigurationError("local path configuration is unreadable") from exc
    if not isinstance(raw, dict) or set(raw) - _CONFIG_KEYS:
        raise PathConfigurationError("local path configuration has an unsupported shape")
    values: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(value, str) or not value.strip():
            raise PathConfigurationError("local path configuration contains an invalid path")
        values[key] = value.strip()
    return values


def _path(value: str, project_root: Path) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (project_root / path).resolve()


def resolve_data_root(explicit: Path | str | None = None, *,
                      project_root: Path | None = None,
                      environ: Mapping[str, str] | None = None) -> Path:
    root = Path(project_root or PROJECT_ROOT).resolve()
    env = os.environ if environ is None else environ
    if explicit is not None:
        return Path(explicit).expanduser().resolve()
    configured = env.get("PHONELAB_DATA_ROOT")
    if configured and configured.strip():
        return Path(configured.strip()).expanduser().resolve()
    local = load_local_config(root)
    if local.get("data_root"):
        return _path(local["data_root"], root)
    appdata = env.get("APPDATA")
    if appdata and appdata.strip():
        return Path(appdata.strip()).expanduser().resolve() / "PhoneLab" / "data"
    if os.name == "nt":
        return Path.home() / "AppData" / "Roaming" / "PhoneLab" / "data"
    xdg = env.get("XDG_DATA_HOME")
    base = Path(xdg).expanduser() if xdg and xdg.strip() else Path.home() / ".local" / "share"
    return base.resolve() / "PhoneLab" / "data"


def resolve_pwsh(*, project_root: Path | None = None,
                 environ: Mapping[str, str] | None = None,
                 path_lookup=shutil.which) -> Path:
    root = Path(project_root or PROJECT_ROOT).resolve()
    env = os.environ if environ is None else environ
    explicit = env.get("PHONELAB_PWSH")
    if explicit and explicit.strip():
        return Path(explicit.strip()).expanduser()
    local = load_local_config(root)
    configured = local.get("pwsh")
    if configured:
        return _path(configured, root)
    found = path_lookup("pwsh") or path_lookup("pwsh.exe")
    return Path(found) if found else Path("pwsh")


def resolve_python_source(*, project_root: Path | None = None,
                          environ: Mapping[str, str] | None = None) -> Path | None:
    root = Path(project_root or PROJECT_ROOT).resolve()
    env = os.environ if environ is None else environ
    explicit = env.get("PHONELAB_PYTHON_SOURCE")
    if explicit and explicit.strip():
        return Path(explicit.strip()).expanduser()
    configured = load_local_config(root).get("python_source")
    return _path(configured, root) if configured else None
