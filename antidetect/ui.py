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

from . import config, fingerprint, launcher, store
from .proxy import ProxyError

INDEX_HTML = Path(__file__).with_name("static") / "index.html"
MAX_BODY = 5 * 1024 * 1024


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _log_file(profile_id: str) -> Path:
    return config.browser_data_dir(profile_id) / "antidetect-last.log"


def _run_cli(*args: str, **popen_kwargs):
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
    }


def _update(profile_id: str, body: dict) -> None:
    with store.transaction() as profiles:
        profile = store.find(profiles, profile_id)
        if profile is None:
            raise ApiError("Профиль не найден", 404)
        fields = {k: body.get(k) for k in ("brand", "site", "instagram", "proxy_city", "timezone", "notes")}
        if "proxy" in body:
            store.set_proxy(profile, body["proxy"] or "")
        store.apply_fields(profile, fields)
        if profile.get("proxy"):
            if body.get("proxy_user") is not None:
                profile["proxy"]["username"] = body["proxy_user"]
            # Пустое поле пароля в форме значит «не менять».
            if body.get("proxy_pass"):
                profile["proxy"]["password"] = body["proxy_pass"]


def _open(profile_id: str, body: dict) -> None:
    profile = store.get(profile_id)
    if launcher.is_running(profile["id"]):
        raise ApiError("Профиль уже открыт")
    extra = []
    if body.get("no_proxy"):
        extra.append("--no-proxy")
    if body.get("force"):
        extra.append("--force")
    log = open(_log_file(profile["id"]), "w", encoding="utf-8")
    command, kwargs = _run_cli("open", profile["id"], *extra, stdout=log, stderr=subprocess.STDOUT)
    subprocess.Popen(command, **kwargs)
    log.close()


def _check(profile_id: str) -> dict:
    profile = store.get(profile_id)
    command, kwargs = _run_cli("check", profile["id"], capture_output=True, timeout=180)
    result = subprocess.run(command, **kwargs)
    output = (result.stdout + result.stderr).decode("utf-8", errors="replace").strip()
    return {"ok": result.returncode == 0, "output": output}


def _regen(profile_id: str) -> None:
    with store.transaction() as profiles:
        profile = store.find(profiles, profile_id)
        if profile is None:
            raise ApiError("Профиль не найден", 404)
        if launcher.is_running(profile["id"]):
            raise ApiError("Сначала закройте браузер профиля")
        profile["fingerprint"] = fingerprint.generate()


def _delete(profile_id: str) -> None:
    profile = store.get(profile_id)
    if launcher.is_running(profile["id"]):
        raise ApiError("Сначала закройте браузер профиля")
    with store.transaction() as profiles:
        profiles[:] = [p for p in profiles if p["id"] != profile["id"]]
    shutil.rmtree(config.browser_data_dir(profile["id"]), ignore_errors=True)


def handle_post(path: str, body) -> dict:
    parts = [part for part in path.split("/") if part]  # api, profiles, <id>, <action>
    if parts == ["api", "import"]:
        if not isinstance(body, list):
            raise ApiError("Ожидается массив профилей из instagram-profiles.json")
        return store.import_records(body)
    if parts == ["api", "profiles"]:
        with store.transaction() as profiles:
            profile = store.new_profile(profiles, str(body.get("name") or ""))
        try:
            _update(profile["id"], body)
        except Exception:
            # Например, прокси с опечаткой: не оставляем полупустой профиль.
            _delete(profile["id"])
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
        elif action == "delete":
            _delete(profile_id)
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
