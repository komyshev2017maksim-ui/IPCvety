"""Самопроверка при каждом запуске.

До перехода на сайт браузер открыт на пустой странице. В этот момент
проверяется, что подмены действительно применились: в странице и в воркере.
Если важная проверка не прошла, запуск отменяется и профиль на сайт не идёт.
"""

from __future__ import annotations

from . import fingerprint

PROBE = """(async () => {
  const native = (fn) => { try { return /\\[native code\\]/.test(Function.prototype.toString.call(fn)); } catch (e) { return false; } };
  const gpu = (canvas) => { try { const gl = canvas.getContext('webgl'); const ext = gl && gl.getExtension('WEBGL_debug_renderer_info');
    return ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : null; } catch (e) { return null; } };
  const workerSource = `(() => { let renderer = null;
    try { const gl = new OffscreenCanvas(4, 4).getContext('webgl'); const ext = gl && gl.getExtension('WEBGL_debug_renderer_info');
      renderer = ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : null; } catch (e) {}
    postMessage({ cores: navigator.hardwareConcurrency, memory: navigator.deviceMemory, renderer,
      timezone: Intl.DateTimeFormat().resolvedOptions().timeZone, language: navigator.language }); })()`;
  let worker = null;
  try {
    worker = await new Promise((resolve, reject) => {
      const w = new Worker(URL.createObjectURL(new Blob([workerSource])));
      w.onmessage = (event) => resolve(event.data);
      w.onerror = () => reject(new Error('worker'));
      setTimeout(() => reject(new Error('timeout')), 4000);
    });
  } catch (e) { worker = null; }
  return {
    webdriver: navigator.webdriver, cores: navigator.hardwareConcurrency, memory: navigator.deviceMemory,
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone, language: navigator.language,
    locale: Intl.DateTimeFormat().resolvedOptions().locale,
    screen: [screen.width, screen.height], outer: [outerWidth, outerHeight],
    renderer: gpu(document.createElement('canvas')),
    native: native(HTMLCanvasElement.prototype.toDataURL) && native(Function.prototype.toString),
    worker,
  };
})()"""


def evaluate(seen: dict, profile: dict, settings: dict, timezone: str | None) -> list[dict]:
    """Сравнивает увиденное страницей с тем, что должно быть. critical: при провале запуск отменяется."""
    fp = profile["fingerprint"]
    flags = fingerprint.spoof_flags()
    checks = []

    def add(name: str, ok: bool, got, want, critical: bool = True) -> None:
        checks.append({"name": name, "ok": bool(ok), "got": got, "want": want, "critical": critical})

    add("Признак автоматизации выключен", seen["webdriver"] is False, seen["webdriver"], False)
    if timezone:
        add("Часовой пояс", seen["timezone"] == timezone, seen["timezone"], timezone)
    locale = settings["locale"]
    add("Язык браузера", seen["language"] == locale, seen["language"], locale)
    add("Формат дат и чисел", seen["locale"] == locale, seen["locale"], locale)
    want_screen = [fp["screen"]["width"], fp["screen"]["height"]]
    add("Размер экрана", seen["screen"] == want_screen, seen["screen"], want_screen)
    add("Окно помещается на экран", seen["outer"][0] <= want_screen[0] and seen["outer"][1] <= want_screen[1],
        seen["outer"], want_screen, critical=False)
    if flags["hardware"]:
        add("Число ядер", seen["cores"] == fp["hardwareConcurrency"], seen["cores"], fp["hardwareConcurrency"])
        if seen.get("memory") is not None:  # на пустой странице Chrome это значение не отдаёт
            add("Память", seen["memory"] == fp["deviceMemory"], seen["memory"], fp["deviceMemory"])
    if flags["webgl"] and fp.get("webgl") and seen.get("renderer"):
        add("Видеокарта", seen["renderer"] == fp["webgl"]["renderer"], seen["renderer"], fp["webgl"]["renderer"])
    add("Подменённые функции выглядят встроенными", seen["native"], seen["native"], True, critical=False)

    worker = seen.get("worker")
    if worker is None:
        add("Воркер запустился", False, None, "ответ воркера", critical=False)
    else:
        same = {key: worker.get(key) for key in ("cores", "memory", "timezone", "language")}
        page = {key: seen.get(key) for key in same}
        add("Воркер видит то же устройство", same == page, same, page)
        if worker.get("renderer") and seen.get("renderer"):
            add("Видеокарта в воркере", worker["renderer"] == seen["renderer"], worker["renderer"], seen["renderer"])
    return checks


def summary(checks: list[dict]) -> dict:
    failed = [c for c in checks if not c["ok"]]
    return {
        "total": len(checks),
        "passed": len(checks) - len(failed),
        "failed": [{"name": c["name"], "got": c["got"], "want": c["want"], "critical": c["critical"]} for c in failed],
        "blocked": any(c["critical"] for c in failed),
    }
