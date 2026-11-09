"""Локальная панель управления: http://127.0.0.1:8765

Сервер слушает только этот компьютер. Браузеры профилей запускаются
отдельными процессами (``python -m antidetect open``), поэтому панель можно
закрыть и открыть снова, не трогая открытые профили.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import brands, config, cookiefile, fingerprint, launcher, store
from .proxy import ProxyError, parse_proxy

INDEX_HTML = Path(__file__).with_name("static") / "index.html"
MAX_BODY = 20 * 1024 * 1024

# Страницы, на которых удобно посмотреть на профиль глазами сайта.
TEST_PAGES = [
    ("Pixelscan: сходится ли отпечаток с IP", "https://pixelscan.net/"),
    ("CreepJS: ищет подменённые функции", "https://abrahamjuliot.github.io/creepjs/"),
    ("BrowserLeaks: значения по каждому API", "https://browserleaks.com/"),
    ("BrowserScan: общий отчёт", "https://www.browserscan.net/"),
    ("IPhey: быстрый вердикт", "https://iphey.com/"),
    ("Whoer: IP, DNS, пояс и язык", "https://whoer.net/"),
]


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _log_file(profile_id: str) -> Path:
    return config.browser_data_dir(profile_id) / "antidetect-last.log"


def _cli(*args: str, **popen_kwargs):
    kwargs = {"cwd": str(config.REPO_DIR), **popen_kwargs}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return [sys.executable, "-m", "antidetect", *args], kwargs


def _last_log(profile_id: str) -> str:
    try:
        lines = _log_file(profile_id).read_text(encoding="utf-8", errors="replace").strip().splitlines()
    except FileNotFoundError:
        return ""
    return "\n".join(lines[-4:])


def state() -> dict:
    profiles = store.all_profiles()
    views = []
    for profile in profiles:
        view = store.public_view(profile)
        view["running"] = launcher.is_running(profile["id"])
        view["effective_timezone"] = launcher.resolve_timezone(profile)
        view["log"] = _last_log(profile["id"])
        views.append(view)
    return {
        "profiles": views,
        "duplicates": store.duplicates(profiles),
        "home": str(config.home()),
        "expected_country": config.settings().get("expected_country"),
        "groups": brands.GROUPS,
        "statuses": brands.STATUSES,
        "steps": brands.STEPS,
        "test_pages": TEST_PAGES,
    }


def _require(profiles: list[dict], profile_id: str) -> dict:
    profile = store.find(profiles, profile_id)
    if profile is None:
        raise ApiError("Профиль не найден", 404)
    return profile


def _closed(profile: dict) -> None:
    if launcher.is_running(profile["id"]):
        raise ApiError(f"Сначала закройте браузер профиля {profile['name']}")


def _update(profile_id: str, body: dict) -> None:
    with store.transaction() as profiles:
        profile = _require(profiles, profile_id)
        if body.get("name") and body["name"].strip() != profile["name"]:
            store.rename(profiles, profile, body["name"])
        if "proxy" in body:
            store.set_proxy(profile, body["proxy"] or "")
        store.apply_fields(profile, {k: body.get(k) for k in store.EDITABLE})
        if profile.get("proxy"):
            if body.get("proxy_user") is not None:
                profile["proxy"]["username"] = body["proxy_user"]
            # Пустое поле пароля в форме значит «не менять».
            if body.get("proxy_pass"):
                profile["proxy"]["password"] = body["proxy_pass"]
        if "status" in body:
            store.log_event(profile["id"], f"Статус: {dict(brands.STATUSES).get(body['status'], body['status'])}")


def _open(profile_id: str, body: dict) -> None:
    profile = store.get(profile_id)
    if launcher.is_running(profile["id"]):
        raise ApiError("Профиль уже открыт")
    extra = []
    url = str(body.get("url") or "")
    if url:
        if not url.startswith(("https://", "http://")):
            raise ApiError("Адрес страницы должен начинаться с https://")
        extra += ["--url", url]
    if body.get("no_proxy"):
        extra.append("--no-proxy")
    if body.get("force"):
        extra.append("--force")
    with open(_log_file(profile["id"]), "w", encoding="utf-8") as log:
        command, kwargs = _cli("open", profile["id"], *extra, stdout=log, stderr=subprocess.STDOUT)
        subprocess.Popen(command, **kwargs)


def _check(profile_id: str) -> dict:
    profile = store.get(profile_id)
    command, kwargs = _cli("check", profile["id"], capture_output=True, timeout=180)
    result = subprocess.run(command, **kwargs)
    output = (result.stdout + result.stderr).decode("utf-8", errors="replace").strip()
    return {"ok": result.returncode == 0, "output": output}


def _trash(profile_id: str) -> None:
    with store.transaction() as profiles:
        profile = _require(profiles, profile_id)
        _closed(profile)
        profile["deleted_at"] = store.now_iso()


def _restore(profile_id: str) -> None:
    with store.transaction() as profiles:
        _require(profiles, profile_id)["deleted_at"] = None


def _purge(profile_id: str) -> None:
    profile = store.get(profile_id)
    _closed(profile)
    with store.transaction() as profiles:
        profiles[:] = [p for p in profiles if p["id"] != profile["id"]]
    shutil.rmtree(config.browser_data_dir(profile["id"]), ignore_errors=True)


def _regen(profile_id: str) -> None:
    with store.transaction() as profiles:
        profile = _require(profiles, profile_id)
        _closed(profile)
        profile["fingerprint"] = fingerprint.generate()
    store.log_event(profile["id"], "Отпечаток сменён")


def _bulk(body: dict) -> dict:
    ids, action, value = body.get("ids") or [], body.get("action"), body.get("value")
    done, errors = 0, []
    for profile_id in ids:
        try:
            if action == "open":
                _open(profile_id, {})
            elif action == "stop":
                launcher.request_stop(store.get(profile_id)["id"])
            elif action == "trash":
                _trash(profile_id)
            elif action == "restore":
                _restore(profile_id)
            elif action in ("status", "group"):
                _update(profile_id, {action: value or ""})
            else:
                raise ApiError("Неизвестное действие")
            done += 1
        except (ApiError, store.StoreError, launcher.LaunchError) as exc:
            errors.append(str(exc))
    return {"done": done, "errors": errors}


def _assign_proxies(body: dict) -> dict:
    """Раздаёт вставленный список прокси: по одному на профиль, по порядку."""
    lines = [line.strip() for line in str(body.get("text") or "").splitlines() if line.strip()]
    parsed = []
    for number, line in enumerate(lines, start=1):
        try:
            parsed.append((line, parse_proxy(line)))
        except ProxyError as exc:
            raise ApiError(f"Строка {number}: {exc}") from exc
    ids = body.get("ids") or []
    assigned = []
    with store.transaction() as profiles:
        if ids:
            targets = [_require(profiles, profile_id) for profile_id in ids]
        else:
            targets = [p for p in profiles if not p.get("proxy") and not p.get("deleted_at")]
        for profile, (line, _) in zip(targets, parsed):
            store.set_proxy(profile, line)
            assigned.append(profile["name"])
    return {"assigned": assigned, "left_proxies": max(0, len(parsed) - len(assigned)),
            "left_profiles": max(0, len(targets) - len(assigned))}


def handle_post(path: str, body) -> dict:
    parts = [part for part in path.split("/") if part]  # api, profiles, <id>, <action>
    if parts == ["api", "import"]:
        if not isinstance(body, list):
            raise ApiError("Ожидается массив профилей из instagram-profiles.json")
        return store.import_records(body)
    if parts == ["api", "seed"]:
        return store.import_records(brands.records())
    if parts == ["api", "bulk"]:
        return _bulk(body)
    if parts == ["api", "proxies"]:
        return _assign_proxies(body)
    if parts == ["api", "profiles"]:
        with store.transaction() as profiles:
            profile = store.new_profile(profiles, str(body.get("name") or ""))
        try:
            _update(profile["id"], body)
        except Exception:
            _purge(profile["id"])  # например, прокси с опечаткой: не оставляем полупустой профиль
            raise
        return {"id": profile["id"]}
    if len(parts) >= 3 and parts[:2] == ["api", "profiles"]:
        profile_id, action = parts[2], (parts[3] if len(parts) > 3 else "update")
        if action == "update":
            _update(profile_id, body)
        elif action == "open":
            _open(profile_id, body)
        elif action == "stop":
            launcher.request_stop(store.get(profile_id)["id"])
        elif action == "check":
            return _check(profile_id)
        elif action == "regen":
            _regen(profile_id)
        elif action == "trash":
            _trash(profile_id)
        elif action == "restore":
            _restore(profile_id)
        elif action == "purge":
            _purge(profile_id)
        elif action == "clone":
            with store.transaction() as profiles:
                return {"id": store.clone(profiles, _require(profiles, profile_id))["id"]}
        elif action == "events":
            return {"events": store.events(store.get(profile_id)["id"])}
        elif action == "cookies-export":
            return {"cookies": launcher.export_cookies(profile_id)}
        elif action == "cookies-import":
            return {"count": launcher.import_cookies(profile_id, cookiefile.parse(str(body.get("text") or "")))}
        else:
            raise ApiError("Неизвестное действие", 404)
        return {"ok": True}
    raise ApiError("Не найдено", 404)


class Handler(BaseHTTPRequestHandler):
    server_version = "antidetect"

    def log_message(self, *args) -> None:  # тихий сервер
        pass

    def _send(self, status: int, payload, content_type: str = "application/json") -> None:
        body = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _host_ok(self) -> bool:
        # Защита от обращений с чужих сайтов через подмену DNS.
        port = self.server.server_address[1]
        return self.headers.get("Host") in (f"127.0.0.1:{port}", f"localhost:{port}")

    def do_GET(self) -> None:
        if not self._host_ok():
            return self._send(403, {"error": "Доступ только с этого компьютера"})
        if self.path in ("/", "/index.html"):
            return self._send(200, INDEX_HTML.read_bytes(), "text/html")
        if self.path == "/api/state":
            return self._send(200, state())
        self._send(404, {"error": "Не найдено"})

    def do_POST(self) -> None:
        # Заголовок, который чужой сайт не может отправить без разрешения CORS.
        if not self._host_ok() or self.headers.get("X-Antidetect") != "1":
            return self._send(403, {"error": "Доступ только из панели"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise ApiError("Слишком большой запрос", 413)
            body = json.loads(self.rfile.read(length) or b"{}")
            self._send(200, handle_post(self.path, body))
        except ApiError as exc:
            self._send(exc.status, {"error": str(exc)})
        except (store.StoreError, launcher.LaunchError, ProxyError, ValueError) as exc:
            self._send(400, {"error": str(exc)})
        except subprocess.TimeoutExpired:
            self._send(504, {"error": "Проверка прокси не уложилась в отведённое время"})


def make_server(port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def serve(port: int | None = None, open_browser: bool = True) -> None:
    port = port or config.settings()["ui_port"]
    server = make_server(port)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"Панель: {url}\nДанные: {config.home()}\nОстановить: Ctrl+C")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
