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
    temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(target)
    os.chmod(target, 0o600)
    return target

