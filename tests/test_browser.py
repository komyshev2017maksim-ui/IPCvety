"""Проверка в настоящем браузере: что сайт видит у профиля."""

import http.server
import json
import threading
import urllib.error
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

  const worker = await new Promise((resolve) => {
    const blob = new Blob(['postMessage({languages: navigator.languages, timezone: Intl.DateTimeFormat().resolvedOptions().timeZone, locale: Intl.DateTimeFormat().resolvedOptions().locale})']);
    const w = new Worker(URL.createObjectURL(blob));
    w.onmessage = (event) => resolve(event.data);
  });

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
    worker,
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
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<!doctype html><title>probe</title>")

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

    def on_ready(context, page):
        result.update(page.evaluate(PROBE))

    try:
        launcher.open_profile(name, url=url, no_proxy=True, headless=True, on_ready=on_ready, log=lambda *_: None)
    except Exception as exc:
        if "Executable doesn't exist" in str(exc) or "not found" in str(exc).lower():
            pytest.skip(f"Браузер не установлен: {str(exc).splitlines()[0]}")
        raise
    return result


def make_profile(name: str, city: str) -> dict:
    with store.transaction() as profiles:
        return store.new_profile(profiles, name, proxy_city=city)


def test_page_sees_profile_fingerprint(site):
    fp = make_profile("A", "Houston, TX")["fingerprint"]
    seen = probe("A", site)

    assert seen["webdriver"] is False
    assert seen["cores"] == fp["hardwareConcurrency"]
    assert seen["memory"] == fp["deviceMemory"]
    assert seen["languages"] == ["en-US", "en"] and seen["sameLanguagesObject"]
    assert seen["timezone"] == "America/Chicago"
    assert seen["locale"] == "en-US"
    assert seen["screen"] == {
        "width": fp["screen"]["width"],
        "height": fp["screen"]["height"],
        "availHeight": fp["screen"]["availHeight"],
    }
    assert seen["screenMedia"] is True  # размер экрана подменён на уровне движка
    assert seen["inner"] == fp["viewport"]
    if seen["hasWebgl"]:
        assert seen["vendor"] == fp["webgl"]["vendor"]
        assert seen["renderer"] == fp["webgl"]["renderer"]
    assert all("[native code]" in text for text in seen["looksNative"])
    assert seen["canvasRepeatable"] and seen["audioRepeatable"]

    # То, что задаётся на уровне браузера, одинаково и в воркере.
    assert seen["worker"] == {
        "languages": ["en-US", "en"],
        "timezone": "America/Chicago",
        "locale": "en-US",
    }
    assert _Site.seen_headers[0]["Accept-Language"] == "en-US,en;q=0.9"
    assert not launcher.is_running(store.get("A")["id"])


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

    call(panel, f"/api/profiles/{created['id']}/delete", {}, ok)
    assert [p["name"] for p in store.all_profiles()] == ["IG-05"]
