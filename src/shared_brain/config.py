"""Client configuration stored with restrictive permissions."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional


def default_config_path() -> Path:
    return Path(os.environ.get("AMM_CONFIG", str(Path.home() / ".amm" / "config.json"))).expanduser()


def load_config(path: Optional[str] = None) -> Dict[str, Any]:
    target = Path(path).expanduser() if path else default_config_path()
    if not target.exists():
        return {}
    return json.loads(target.read_text(encoding="utf-8"))


def save_config(config: Dict[str, Any], path: Optional[str] = None) -> Path:
    target = Path(path).expanduser() if path else default_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    # Create the temporary with restrictive permissions from the first byte;
    # chmod-after-write leaves a small window where a bearer token can inherit
    # a permissive process umask.
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(config, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(target)
    os.chmod(target, 0o600)
    return target
