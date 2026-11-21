import json
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"

DEFAULT_CONFIG = {
    "client_id": "",
    "access_token": "",
    "refresh_token": "",
    "expires_at": 0.0,
    "pin_mode": "auto",          # "auto" | "compact" | "expanded"
    "track_peek": True,          # Auto-peek compact pill for 3.5s on song change
    "hide_in_fullscreen": True,  # Disable top-edge trigger in fullscreen apps
    "hover_dwell_ms": 150,       # Prevents accidental triggers when clicking browser tabs
}


def load_config() -> dict:
    data = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            with CONFIG_PATH.open("r", encoding="utf-8") as f:
                saved = json.load(f)
            if isinstance(saved, dict):
                data.update(saved)
        except Exception:
            pass
    return data


def save_config(cfg: dict) -> None:
    try:
        with CONFIG_PATH.open("w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass