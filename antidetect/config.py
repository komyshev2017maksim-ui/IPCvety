"""Пути и общие настройки.

Все данные (профили, кэш браузеров, настройки) лежат в одной папке.
По умолчанию это ``data`` рядом с кодом; переопределяется переменной
окружения ``ANTIDETECT_HOME`` (например, ``D:\\Маркетинг\\Claude\\antidetect-data``).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
REPO_DIR = PACKAGE_DIR.parent

DEFAULT_SETTINGS = {
    # "auto": установленный Google Chrome, иначе Microsoft Edge, иначе Chromium.
    # Можно указать "chrome", "msedge", "chromium" или полный путь к exe.
    "browser": "auto",
    # Страна, в которой должен выходить прокси. Профиль не откроется,
    # если проверка IP покажет другую страну.
    "expected_country": "US",
    "locale": "en-US",
    "languages": ["en-US", "en"],
    # Сервисы определения IP, по порядку. Через них идёт только IP прокси.
    "ip_services": ["https://ipinfo.io/json", "https://ipapi.co/json/"],
    "ip_check_timeout": 20,
    # Раз в сколько минут сверять IP выхода, пока профиль открыт. 0 выключает.
    "ip_watch_minutes": 5,
    "ui_port": 8765,
    "start_url": "https://www.instagram.com/",
}


def home() -> Path:
    raw = os.environ.get("ANTIDETECT_HOME")
    path = Path(raw).expanduser() if raw else REPO_DIR / "data"
    path.mkdir(parents=True, exist_ok=True)
    return path


def profiles_file() -> Path:
    return home() / "profiles.json"


def browser_data_dir(profile_id: str) -> Path:
    path = home() / "browser-data" / profile_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def settings() -> dict:
    merged = dict(DEFAULT_SETTINGS)
    path = home() / "settings.json"
    if path.exists():
        merged.update(json.loads(path.read_text(encoding="utf-8")))
    env_browser = os.environ.get("ANTIDETECT_BROWSER")
    if env_browser:
        merged["browser"] = env_browser
    return merged
