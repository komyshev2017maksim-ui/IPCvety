"""Хранилище профилей: один JSON-файл в папке данных.

Запись идёт под файловой блокировкой и через временный файл, потому что
панель и открытые браузеры работают с файлом одновременно.
"""

from __future__ import annotations

import json
import os
import re
import time
from contextlib import contextmanager
from datetime import datetime, timezone

from . import config, fingerprint
from .proxy import Proxy, parse_proxy

VERSION = 1


class StoreError(Exception):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "profile"


def _read() -> dict:
    path = config.profiles_file()
    if not path.exists():
        return {"version": VERSION, "profiles": []}
    return json.loads(path.read_text(encoding="utf-8"))


def _write(data: dict) -> None:
    path = config.profiles_file()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


@contextmanager
def _lock(timeout: float = 10.0):
    lock_path = config.profiles_file().with_suffix(".lock")
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                # Блокировка от упавшего процесса: запись занимает миллисекунды.
                if time.time() - lock_path.stat().st_mtime > 30:
                    lock_path.unlink(missing_ok=True)
                    continue
            except FileNotFoundError:
                continue
            if time.monotonic() > deadline:
                raise StoreError(f"Файл профилей занят другим процессом: {lock_path}")
            time.sleep(0.05)
    try:
        yield
    finally:
        os.close(fd)
        lock_path.unlink(missing_ok=True)


@contextmanager
def transaction():
    """Прочитать, изменить и записать профили атомарно."""
    with _lock():
        data = _read()
        yield data["profiles"]
        _write(data)


def all_profiles() -> list[dict]:
    return _read()["profiles"]


def find(profiles: list[dict], key: str) -> dict | None:
    """Ищет по id, названию профиля, номеру бренда или названию бренда."""
    key_norm = str(key).strip().lower()
    for field in ("id", "name", "brand"):
        for profile in profiles:
            if str(profile.get(field, "")).lower() == key_norm:
                return profile
    if key_norm.isdigit():
        for profile in profiles:
            if profile.get("n") == int(key_norm):
                return profile
    return None


def get(key: str) -> dict:
    profile = find(all_profiles(), key)
    if profile is None:
        raise StoreError(f"Профиль не найден: {key}")
    return profile


def new_profile(profiles: list[dict], name: str, **fields) -> dict:
    name = name.strip()
    if not name:
        raise StoreError("У профиля должно быть название")
    if any(p["name"].lower() == name.lower() for p in profiles):
        raise StoreError(f"Профиль с названием {name} уже есть")
    base = slugify(name)
    taken = {p["id"] for p in profiles}
    profile_id, counter = base, 2
    while profile_id in taken:
        profile_id, counter = f"{base}-{counter}", counter + 1
    profile = {
        "id": profile_id,
        "name": name,
        "n": None,
        "brand": "",
        "site": "",
        "instagram": "",
        "proxy": None,
        "proxy_city": "",
        "timezone": "",
        "geo": None,
        "fingerprint": fingerprint.generate(),
        "notes": "",
        "created_at": now_iso(),
        "last_opened": None,
    }
    apply_fields(profile, fields)
    profiles.append(profile)
    return profile


EDITABLE = {"brand", "site", "instagram", "proxy_city", "timezone", "notes", "n"}


def apply_fields(profile: dict, fields: dict) -> None:
    """Обновляет поля профиля. Прокси принимает строку в любом из форматов."""
    for key, value in fields.items():
        if value is None:
            continue
        if key == "proxy":
            set_proxy(profile, value)
        elif key in EDITABLE:
            profile[key] = value.strip() if isinstance(value, str) else value


def set_proxy(profile: dict, text: str) -> None:
    """Меняет прокси. Если в строке нет логина и пароля, а адрес прежний,
    сохранённые логин и пароль остаются: панель учёта экспортирует прокси без пароля."""
    new = parse_proxy(text)
    old = Proxy.from_dict(profile.get("proxy"))
    if new is None:
        profile["proxy"] = None
        profile["geo"] = None
        return
    if old and not new.username and old.endpoint == new.endpoint:
        new = Proxy(new.scheme, new.host, new.port, old.username, old.password)
    if old is None or old.endpoint != new.endpoint:
        profile["geo"] = None  # другой прокси: прошлую проверку IP забываем
    profile["proxy"] = new.to_dict()


def import_records(records: list[dict]) -> dict:
    """Импорт ``instagram-profiles.json`` из панели учёта.

    Повторный запуск безопасен: профиль ищется по ``profile_name``, затем по
    номеру бренда; существующие профили обновляются, отпечаток не меняется.
    """
    created, updated, unchanged = [], [], []
    with transaction() as profiles:
        for record in records:
            name = str(record.get("profile_name") or "").strip()
            number = record.get("n")
            if not name:
                if number is None:
                    raise StoreError(f"В записи нет ни profile_name, ни n: {record}")
                name = f"IG-{int(number):02d}"
            fields = {
                "n": number,
                "brand": record.get("brand"),
                "site": record.get("site"),
                "instagram": record.get("instagram"),
                "proxy_city": record.get("proxy_city"),
                "timezone": record.get("timezone"),
            }
            proxy_text = str(record.get("proxy") or "").strip()
            existing = find(profiles, name)
            if existing is None and number is not None:
                existing = next((p for p in profiles if p.get("n") == number), None)
            if existing is None:
                profile = new_profile(profiles, name, **fields)
                if proxy_text:
                    set_proxy(profile, proxy_text)
                created.append(profile["name"])
                continue
            before = json.dumps(existing, sort_keys=True)
            # Пустые значения из панели не затирают то, что уже заполнено здесь.
            apply_fields(existing, {k: v for k, v in fields.items() if v not in ("", None)})
            if proxy_text:
                set_proxy(existing, proxy_text)
            if json.dumps(existing, sort_keys=True) == before:
                unchanged.append(existing["name"])
            else:
                updated.append(existing["name"])
    return {"created": created, "updated": updated, "unchanged": unchanged}


def duplicates(profiles: list[dict]) -> dict[str, list[str]]:
    """Что повторяется между профилями: адрес прокси, IP выхода, логин Instagram."""
    seen: dict[tuple[str, str], list[str]] = {}
    for profile in profiles:
        proxy = Proxy.from_dict(profile.get("proxy"))
        values = {
            "прокси": proxy.endpoint if proxy else "",
            "IP выхода": (profile.get("geo") or {}).get("ip", ""),
            "логин Instagram": (profile.get("instagram") or "").lower(),
        }
        for kind, value in values.items():
            if value:
                seen.setdefault((kind, value), []).append(profile["name"])
    return {f"{kind} {value}": names for (kind, value), names in seen.items() if len(names) > 1}


def public_view(profile: dict) -> dict:
    """Профиль без пароля прокси: для вывода на экран и в панель."""
    view = json.loads(json.dumps(profile))
    if view.get("proxy"):
        view["proxy"]["has_password"] = bool(view["proxy"].pop("password", ""))
    return view


# --- Журнал профиля: что и когда с ним происходило --------------------------------


def _events_file(profile_id: str):
    return config.browser_data_dir(profile_id) / "antidetect-events.log"


def log_event(profile_id: str, text: str) -> None:
    try:
        with open(_events_file(profile_id), "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"t": now_iso(), "text": text}, ensure_ascii=False) + "\n")
    except OSError:
        pass


def events(profile_id: str, limit: int = 30) -> list[dict]:
    try:
        lines = _events_file(profile_id).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    result = []
    for line in lines[-limit:]:
        try:
            result.append(json.loads(line))
        except ValueError:
            pass
    return result[::-1]
