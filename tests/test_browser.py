"""Проверка в настоящем браузере: что сайт видит у профиля."""

import http.server
import json
import threading
import urllib.error
import urllib.parse
import urllib.request

import pytest

from antidetect import launcher, store, ui

PROBE = """async () => {
  const canvas = document.createElement('canvas');
  canvas.width = 220; canvas.height = 60;
  const ctx = canvas.getContext('2d');
  ctx.fillStyle = '#f60'; ctx.fillRect(10, 10, 120, 30);
  ctx.fillStyle = '#069'; ctx.font = '16px Arial'; ctx.fillText('Fingerprint <canvas> 1.0', 4, 22);
  const first = canvas.toDataURL();
  const second = canvas.toDataURL();
  const pixels = Array.from(ctx.getImageData(0, 0, 220, 60).data);

  const gl = document.createElement('canvas').getContext('webgl');
  let vendor = null, renderer = null;
  if (gl) {
    const ext = gl.getExtension('WEBGL_debug_renderer_info');
    vendor = gl.getParameter(ext.UNMASKED_VENDOR_WEBGL);
    renderer = gl.getParameter(ext.UNMASKED_RENDERER_WEBGL);
  }

  const offline = new OfflineAudioContext(1, 4410, 44100);
  const osc = offline.createOscillator();
  osc.type = 'triangle'; osc.frequency.value = 1000;
  osc.connect(offline.destination); osc.start(0);
  const buffer = await offline.startRendering();
  const samples = buffer.getChannelData(0);
  let audio = 0;
  for (let i = 0; i < samples.length; i++) audio += Math.abs(samples[i]);
  const audioAgain = Array.from(buffer.getChannelData(0)).reduce((a, v) => a + Math.abs(v), 0);

  const workerSource = `(async () => {
    const gl = new OffscreenCanvas(8, 8).getContext('webgl');
    const ext = gl && gl.getExtension('WEBGL_debug_renderer_info');
    const c = new OffscreenCanvas(60, 20); const x = c.getContext('2d');
    x.fillStyle = '#f60'; x.fillRect(0, 0, 60, 20);
    const px = Array.from(x.getImageData(0, 0, 60, 20).data);
    postMessage({
      languages: navigator.languages, cores: navigator.hardwareConcurrency, memory: navigator.deviceMemory,
      timezone: Intl.DateTimeFormat().resolvedOptions().timeZone, locale: Intl.DateTimeFormat().resolvedOptions().locale,
      renderer: ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : null,
      canvasNoised: px.some((v, i) => i % 4 < 3 && v !== [255, 102, 0][i % 4]),
      native: Function.prototype.toString.call(WebGLRenderingContext.prototype.getParameter),
    });
  })()`;
  const runWorker = (url) => new Promise((resolve) => { const w = new Worker(url); w.onmessage = (event) => resolve(event.data); });
  const worker = await runWorker(URL.createObjectURL(new Blob([workerSource])));
  const urlWorker = await runWorker('/worker.js?' + encodeURIComponent(workerSource));
  const gpuAdapter = navigator.gpu ? await navigator.gpu.requestAdapter() : null;

  const native = (fn) => Function.prototype.toString.call(fn);
  return {
    webdriver: navigator.webdriver,
    cores: navigator.hardwareConcurrency,
    memory: navigator.deviceMemory,
    languages: navigator.languages,
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
    locale: Intl.DateTimeFormat().resolvedOptions().locale,
    screen: { width: screen.width, height: screen.height, availHeight: screen.availHeight },
    screenMedia: matchMedia(`(device-width: ${screen.width}px) and (device-height: ${screen.height}px)`).matches,
    inner: { width: innerWidth, height: innerHeight },
    vendor, renderer, hasWebgl: Boolean(gl),
    canvas: first, canvasRepeatable: first === second, pixels,
    audio, audioRepeatable: audio === audioAgain,
    worker, urlWorker, gpuAdapter, outer: { width: outerWidth, height: outerHeight },
    looksNative: [
      native(HTMLCanvasElement.prototype.toDataURL),
      native(Object.getOwnPropertyDescriptor(Navigator.prototype, 'hardwareConcurrency').get),
      native(Function.prototype.toString),
      native(WebGLRenderingContext.prototype.getParameter),
    ],
    sameLanguagesObject: navigator.languages === navigator.languages,
  };
}"""


class _Site(http.server.BaseHTTPRequestHandler):
    seen_headers = []

    def do_GET(self):
        type(self).seen_headers.append(dict(self.headers))
        is_worker = self.path.startswith("/worker.js?")
        body = urllib.parse.unquote(self.path.split("?", 1)[1]).encode() if is_worker else b"<!doctype html><title>probe</title>"
        self.send_response(200)
        self.send_header("Content-Type", "text/javascript" if is_worker else "text/html")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def site():
    _Site.seen_headers = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Site)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/"
    server.shutdown()


def probe(name: str, url: str) -> dict:
    result = {}

    async def on_ready(tab):
        result.update(await tab.evaluate(f"({PROBE})()"))

    try:
        launcher.open_profile(name, url=url, no_proxy=True, headless=True, on_ready=on_ready, log=lambda *_: None)
    except launcher.LaunchError as exc:
        if "Не нашёл Google Chrome" in str(exc):
            pytest.skip("Браузер не установлен")
        raise
    return result


def make_profile(name: str, city: str) -> dict:
    with store.transaction() as profiles:
        return store.new_profile(profiles, name, proxy_city=city)


def test_page_sees_profile_fingerprint(site):
    fp = make_profile("A", "Houston, TX")["fingerprint"]
    seen = probe("A", site)

    assert seen["webdriver"] is False, "браузер не должен выглядеть управляемым программой"
    assert seen["cores"] == fp["hardwareConcurrency"]
    assert seen["memory"] == fp["deviceMemory"]
    # Сколько языков показывать, решает сам Chrome (новые версии сокращают список до одного).
    assert seen["languages"][0] == "en-US" and all(code.startswith("en") for code in seen["languages"])
    assert seen["sameLanguagesObject"]
    assert seen["timezone"] == "America/Chicago"
    assert seen["locale"] == "en-US"
    assert seen["screen"] == {
        "width": fp["screen"]["width"],
        "height": fp["screen"]["height"],
        "availHeight": fp["screen"]["availHeight"],
    }
    assert seen["screenMedia"] is True  # размер экрана подменён на уровне движка
    assert seen["inner"]["width"] <= seen["outer"]["width"] <= fp["screen"]["width"]
    if seen["hasWebgl"]:
        assert seen["vendor"] == fp["webgl"]["vendor"]
        assert seen["renderer"] == fp["webgl"]["renderer"]
    assert seen["gpuAdapter"] is None, "WebGPU не должен выдавать настоящую видеокарту"
    assert all("[native code]" in text for text in seen["looksNative"])
    assert seen["canvasRepeatable"] and seen["audioRepeatable"]
    assert _Site.seen_headers[0]["Accept-Language"].startswith("en-US")
    assert "ru" not in _Site.seen_headers[0]["Accept-Language"]
    assert not launcher.is_running(store.get("A")["id"])
    report = store.get("A")["preflight"]
    assert report["passed"] == report["total"] >= 8 and not report["blocked"], report


def test_launch_stops_before_any_site_when_spoofing_did_not_apply(site, monkeypatch):
    make_profile("A", "Houston, TX")
    # Имитируем сбой: проверка сообщает, что пояс не тот.
    monkeypatch.setattr(launcher.preflight, "evaluate",
                        lambda seen, profile, settings, tz: [{"name": "Часовой пояс", "ok": False, "got": seen["timezone"], "want": "Mars/Olympus", "critical": True}])
    with pytest.raises(launcher.LaunchError, match="Самопроверка не пройдена: Часовой пояс"):
        launcher.open_profile("A", url=site, no_proxy=True, headless=True, log=lambda *_: None)
    assert _Site.seen_headers == [], "до сайта дело не дошло"
    assert store.get("A")["preflight"]["blocked"] is True


def test_workers_see_the_same_device_as_the_page(site):
    fp = make_profile("A", "Houston, TX")["fingerprint"]
    seen = probe("A", site)
    for kind in ("worker", "urlWorker"):  # воркер из blob и воркер из файла на сайте
        worker = seen[kind]
        assert worker["languages"] == seen["languages"], kind
        assert worker["timezone"] == "America/Chicago" and worker["locale"] == "en-US", kind
        assert worker["cores"] == fp["hardwareConcurrency"], kind
        assert worker["memory"] == fp["deviceMemory"], kind
        assert worker["renderer"] in (fp["webgl"]["renderer"], None), kind
        assert worker["canvasNoised"] is True, kind
        assert "[native code]" in worker["native"], kind


def test_profiles_differ_and_each_is_stable_between_launches(site):
    make_profile("A", "Miami, FL")
    make_profile("B", "Miami, FL")
    a1, b, a2 = probe("A", site), probe("B", site), probe("A", site)

    assert a1["canvas"] == a2["canvas"], "отпечаток профиля должен сохраняться между запусками"
    assert a1["audio"] == a2["audio"]
    assert a1["canvas"] != b["canvas"], "у разных профилей разный canvas"
    assert a1["audio"] != b["audio"]

    # Шум незаметен глазу: меняется только младший бит у малой части пикселей.
    diffs = [abs(x - y) for x, y in zip(a1["pixels"], b["pixels"])]
    assert max(diffs) == 1
    assert 0 < sum(diffs) < len(diffs) * 0.1


def test_refuses_to_open_without_proxy_or_timezone(site):
    make_profile("A", "Miami, FL")
    with pytest.raises(launcher.LaunchError, match="не задан прокси"):
        launcher.open_profile("A", url=site, headless=True, log=lambda *_: None)

    make_profile("B", "")
    with pytest.raises(launcher.LaunchError, match="часовой пояс"):
        launcher.open_profile("B", url=site, no_proxy=True, headless=True, log=lambda *_: None)


def test_cookies_survive_export_and_import(site):
    make_profile("A", "Miami, FL")
    make_profile("B", "Miami, FL")
    cookie = {"name": "sessionid", "value": "abc123", "domain": ".example.com", "path": "/",
              "secure": True, "httpOnly": True, "expires": 1999999999}
    assert launcher.import_cookies("A", [cookie]) == 1
    exported = launcher.export_cookies("A")
    assert [(c["name"], c["value"], c["domain"]) for c in exported] == [("sessionid", "abc123", ".example.com")]
    assert launcher.export_cookies("B") == [], "cookies одного профиля не видны другому"


# --- панель ---------------------------------------------------------------------


@pytest.fixture
def panel():
    server = ui.make_server(0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def call(base, path, body=None, headers=None):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(base + path, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def test_panel_api(panel):
    ok = {"X-Antidetect": "1", "Content-Type": "application/json"}

    status, _ = call(panel, "/api/profiles", {"name": "X"})
    assert status == 403, "без заголовка панели запись запрещена"
    status, _ = call(panel, "/api/state", headers={"Host": "evil.example"})
    assert status == 403, "чужой Host отклоняется"

    status, created = call(panel, "/api/profiles", {"name": "IG-01", "brand": "Brand", "proxy": "1.2.3.4:8000",
                                                    "proxy_user": "u", "proxy_pass": "secret", "proxy_city": "Miami, FL"}, ok)
    assert status == 200
    status, state = call(panel, "/api/state")
    profile = state["profiles"][0]
    assert profile["proxy"]["has_password"] and "secret" not in json.dumps(state)
    assert profile["effective_timezone"] == "America/New_York" and profile["running"] is False

    # Пустой пароль в форме не стирает сохранённый.
    call(panel, f"/api/profiles/{created['id']}", {"proxy": "1.2.3.4:8000", "proxy_pass": "", "notes": "n"}, ok)
    assert store.get("IG-01")["proxy"]["password"] == "secret"
    assert store.get("IG-01")["notes"] == "n"

    status, error = call(panel, "/api/profiles", {"name": "IG-02", "proxy": "not a proxy"}, ok)
    assert status == 400 and len(store.all_profiles()) == 1, "профиль с ошибкой в прокси не создаётся"

    status, result = call(panel, "/api/import", [{"n": 5, "brand": "B5", "profile_name": "IG-05"}], ok)
    assert result["created"] == ["IG-05"]

    fp_before = store.get("IG-01")["fingerprint"]
    call(panel, f"/api/profiles/{created['id']}/regen", {}, ok)
    assert store.get("IG-01")["fingerprint"] != fp_before

    # Карточка: статус, метки, шаги, переименование.
    call(panel, f"/api/profiles/{created['id']}", {"name": "IG-01a", "status": "warm", "tags": ["vip"],
                                                   "steps": {"num": True}, "email": "a@example.com"}, ok)
    profile = store.get("IG-01a")
    assert (profile["status"], profile["tags"], profile["steps"]) == ("warm", ["vip"], {"num": True})
    _, events = call(panel, f"/api/profiles/{created['id']}/events", {}, ok)
    assert any("Прогрев" in event["text"] for event in events["events"])

    # Копия: новый отпечаток, прокси и почта не копируются.
    _, copy = call(panel, f"/api/profiles/{created['id']}/clone", {}, ok)
    clone = store.get(copy["id"])
    assert clone["name"] == "IG-01a копия" and clone["proxy"] is None and clone["email"] == ""
    assert store.get("IG-01a")["id"] == created["id"], "название одного профиля не должно совпадать с id другого"
    assert clone["fingerprint"] != profile["fingerprint"] and clone["tags"] == ["vip"]

    # Прокси списком: раздаются профилям без прокси, по одному.
    status, result = call(panel, "/api/proxies", {"text": "5.5.5.5:1:u:p\nsocks5://u:p@6.6.6.6:2\n7.7.7.7:3"}, ok)
    assert result["assigned"] == ["IG-05", "IG-01a копия"] and result["left_proxies"] == 1
    assert store.get("IG-05")["proxy"]["password"] == "p" and store.get(copy["id"])["proxy"]["scheme"] == "socks5"
    status, error = call(panel, "/api/proxies", {"text": "мусор"}, ok)
    assert status == 400 and "Строка 1" in error["error"]

    # Массовые действия и корзина.
    ids = [p["id"] for p in store.all_profiles()]
    _, result = call(panel, "/api/bulk", {"ids": ids, "action": "group", "value": "Лодки"}, ok)
    assert result["done"] == 3 and all(p["group"] == "Лодки" for p in store.all_profiles())
    call(panel, f"/api/profiles/{created['id']}/trash", {}, ok)
    assert store.get("IG-01a")["deleted_at"] and len(store.all_profiles()) == 3
    assert "прокси 1.2.3.4:8000" not in json.dumps(call(panel, "/api/state")[1]["duplicates"], ensure_ascii=False)
    call(panel, f"/api/profiles/{created['id']}/restore", {}, ok)
    assert store.get("IG-01a")["deleted_at"] is None
    call(panel, f"/api/profiles/{created['id']}/purge", {}, ok)
    assert "IG-01a" not in [p["name"] for p in store.all_profiles()]

    # Карточки 33 брендов создаются один раз.
    _, seeded = call(panel, "/api/seed", {}, ok)
    assert len(seeded["created"]) == 32 and seeded["updated"] == ["IG-05"]
    _, again = call(panel, "/api/seed", {}, ok)
    assert again["created"] == [] and again["updated"] == []
    assert store.get("IG-33")["site"] == "rvcamperremoval.com" and store.get("IG-12")["group"] == "Расчистка участков"
