"""Запуск через прокси с логином и паролем: проверка IP, пояс по IP, отказ при чужой стране.

Вместо настоящего прокси поднимается маленький локальный, он же отвечает
за «сервис определения IP» и за «сайт».
"""

import base64
import http.server
import json
import threading

import pytest

from antidetect import config, launcher, store

USER, PASSWORD = "proxyuser", "proxypass"


class _Proxy(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    geo = {}
    requests = []
    tunneled = False

    def _authorized(self) -> bool:
        expected = "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
        return self.headers.get("Proxy-Authorization") == expected

    def _deny(self):
        self.send_response(407)
        self.send_header("Proxy-Authenticate", 'Basic realm="proxy"')
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_CONNECT(self):
        # Туннель: следующие запросы в этом соединении считаем прошедшими через прокси.
        if not self._authorized():
            return self._deny()
        self.send_response(200, "Connection established")
        self.end_headers()
        self.tunneled = True

    def do_GET(self):
        if self.tunneled:
            url = f"http://{self.headers.get('Host')}{self.path}"
        elif self._authorized():
            url = self.path
        else:
            return self._deny()
        type(self).requests.append(url)
        if url.startswith("http://ipcheck.test/"):
            body, content_type = json.dumps(type(self).geo).encode(), "application/json"
        else:
            body, content_type = b"<!doctype html><title>site</title>", "text/html"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def proxy_port():
    _Proxy.requests = []
    _Proxy.geo = {"ip": "198.51.100.23", "city": "Chicago", "region": "Illinois", "country": "US",
                  "loc": "41.85,-87.65", "org": "AS64500 Example ISP", "timezone": "America/Chicago"}
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Proxy)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    (config.home() / "settings.json").write_text(
        json.dumps({"ip_services": ["http://ipcheck.test/json"], "ip_check_timeout": 10}), encoding="utf-8"
    )
    yield server.server_port
    server.shutdown()


def open_and_read(name: str, **kwargs) -> dict:
    seen = {}

    async def on_ready(tab):
        seen["timezone"] = await tab.evaluate("Intl.DateTimeFormat().resolvedOptions().timeZone")
        seen["title"] = await tab.evaluate("document.title")

    try:
        launcher.open_profile(name, url="http://site.test/", headless=True, on_ready=on_ready,
                              log=lambda *_: None, **kwargs)
    except launcher.LaunchError as exc:
        if "Не нашёл Google Chrome" in str(exc):
            pytest.skip("Браузер не установлен")
        raise
    return seen


def make(name: str, port: int, password: str = PASSWORD) -> None:
    with store.transaction() as profiles:
        # В панели учёта стоит Майами, но IP прокси на самом деле в Чикаго.
        store.new_profile(profiles, name, proxy=f"127.0.0.1:{port}:{USER}:{password}", proxy_city="Miami, FL")


def test_open_through_authenticated_proxy_uses_timezone_of_exit_ip(proxy_port):
    make("A", proxy_port)
    seen = open_and_read("A")

    assert seen["title"] == "site", "страница загружена через прокси с логином и паролем"
    assert seen["timezone"] == "America/Chicago", "пояс берётся по IP, а не из панели"
    assert "http://site.test/" in _Proxy.requests

    profile = store.get("A")
    assert profile["geo"]["ip"] == "198.51.100.23" and profile["last_opened"]
    assert (profile["geo"]["lat"], profile["geo"]["lon"]) == (41.85, -87.65)
    assert profile["geo"]["ping_ms"] >= 0


def test_wrong_proxy_password_stops_launch(proxy_port):
    make("A", proxy_port, password="wrong")
    with pytest.raises(launcher.LaunchError, match="Прокси не отвечает"):
        open_and_read("A")
    assert _Proxy.requests == []


def test_exit_ip_in_wrong_country_stops_launch_unless_forced(proxy_port):
    _Proxy.geo = {"ip": "203.0.113.5", "city": "Almaty", "region": "Almaty", "country": "KZ",
                  "loc": "43.25,76.95", "timezone": "Asia/Almaty"}
    make("A", proxy_port)
    with pytest.raises(launcher.LaunchError, match="нужна страна US"):
        open_and_read("A")
    assert "http://site.test/" not in _Proxy.requests, "браузер не открывался"

    assert open_and_read("A", force=True)["timezone"] == "Asia/Almaty"


def test_check_reports_shared_exit_ip(proxy_port):
    make("A", proxy_port)
    make("B", proxy_port)
    assert launcher.check_profile("A")["warnings"] == []
    result = launcher.check_profile("B")
    assert result["errors"] == []
    assert any("A" in warning for warning in result["warnings"])
    assert "IP выхода 198.51.100.23" in store.duplicates(store.all_profiles())


def test_ip_watch_closes_profile_when_exit_ip_leaves_the_country(monkeypatch):
    import asyncio

    with store.transaction() as profiles:
        profile = store.new_profile(profiles, "W", proxy="127.0.0.1:1", proxy_city="Miami, FL")
        profile["geo"] = {"ip": "198.51.100.23", "country": "US", "timezone": "America/New_York"}
    answers = iter([
        {"ip": "198.51.100.23", "country": "US", "city": "Miami", "timezone": "America/New_York"},
        {"ip": "198.51.100.99", "country": "US", "city": "Tampa", "timezone": "America/New_York"},
        {"ip": "203.0.113.5", "country": "KZ", "city": "Almaty", "timezone": "Asia/Almaty"},
    ])
    monkeypatch.setattr(launcher.netcheck, "check_ip", lambda url, settings: next(answers))

    class FakeRelay:
        url = "http://127.0.0.1:1"

    class FakeSession:
        pages = []

    settings = {"ip_watch_minutes": 0.0003, "expected_country": "US"}
    asyncio.run(asyncio.wait_for(
        launcher._watch_ip(store.get("W"), settings, FakeRelay(), FakeSession(), False, lambda *_: None), 5))

    texts = [event["text"] for event in store.events(store.get("W")["id"])]
    assert any("198.51.100.23 -> 198.51.100.99" in text for text in texts), "смена IP внутри страны только записывается"
    assert any("Закрыт автоматически: IP оказался в KZ" in text for text in texts)
    assert store.get("W")["geo"]["ip"] == "203.0.113.5"
