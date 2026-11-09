// Подмена отпечатка. Выполняется до скриптов сайта в каждой странице, фрейме
// и воркере. __FINGERPRINT__ заменяется на JSON профиля (см. fingerprint.py).
// Пояс, язык, размер экрана и число ядер подменяет сам движок (launcher.py),
// здесь только то, для чего у движка нет настройки.
(() => {
  'use strict';
  const FP = __FINGERPRINT__;
  const scope = typeof globalThis !== 'undefined' ? globalThis : self;
  const inPage = typeof document !== 'undefined';

  // --- Подменённые функции должны выглядеть как встроенные -------------------
  const nativeToString = Function.prototype.toString;
  const masked = new WeakMap();
  const toStringProxy = new Proxy(nativeToString, {
    apply(target, self, args) {
      if (masked.has(self)) return masked.get(self);
      return Reflect.apply(target, self, args);
    },
  });
  masked.set(toStringProxy, nativeToString.call(nativeToString));
  Object.defineProperty(Function.prototype, 'toString', {
    ...Object.getOwnPropertyDescriptor(Function.prototype, 'toString'),
    value: toStringProxy,
  });

  const wrapMethod = (owner, name, handler) => {
    const proto = owner && owner.prototype;
    const desc = proto && Object.getOwnPropertyDescriptor(proto, name);
    if (!desc || typeof desc.value !== 'function') return;
    const original = desc.value;
    const proxy = new Proxy(original, {
      apply(target, self, args) {
        return handler(target, self, args);
      },
    });
    masked.set(proxy, nativeToString.call(original));
    Object.defineProperty(proto, name, { ...desc, value: proxy });
  };

  const wrapGetter = (owner, name, getValue) => {
    const proto = owner && owner.prototype;
    const desc = proto && Object.getOwnPropertyDescriptor(proto, name);
    if (!desc || typeof desc.get !== 'function') return;
    const original = desc.get;
    const proxy = new Proxy(original, {
      apply(target, self, args) {
        // Вызов оригинала сохраняет «Illegal invocation» на чужом объекте.
        const real = Reflect.apply(target, self, args);
        return getValue(real);
      },
    });
    masked.set(proxy, nativeToString.call(original));
    Object.defineProperty(proto, name, { ...desc, get: proxy });
  };

  // --- Детерминированный шум: один и тот же для профиля при каждом запуске ---
  const hash = (a, b) => {
    let h = (Math.imul(a ^ 0x9e3779b9, 0x85ebca6b) ^ Math.imul(b + 0x7f4a7c15, 0xc2b2ae35)) >>> 0;
    h ^= h >>> 15;
    h = Math.imul(h, 0x2c1b3c6d) >>> 0;
    h ^= h >>> 12;
    h = Math.imul(h, 0x297a2d39) >>> 0;
    return (h ^ (h >>> 15)) >>> 0;
  };

  // --- Память устройства (в странице и в воркере одинаково) ------------------
  if (FP.deviceMemory) {
    wrapGetter(scope.Navigator, 'deviceMemory', () => FP.deviceMemory);
    wrapGetter(scope.WorkerNavigator, 'deviceMemory', () => FP.deviceMemory);
  }

  // --- Рабочая область экрана: экран без панели задач ------------------------
  if (FP.screen && scope.Screen) {
    wrapGetter(scope.Screen, 'availWidth', () => FP.screen.availWidth);
    wrapGetter(scope.Screen, 'availHeight', () => FP.screen.availHeight);
    wrapGetter(scope.Screen, 'availLeft', () => 0);
    wrapGetter(scope.Screen, 'availTop', () => 0);
  }

  // --- WebGL: производитель и модель видеокарты -------------------------------
  if (FP.webgl) {
    const UNMASKED_VENDOR = 0x9245;
    const UNMASKED_RENDERER = 0x9246;
    for (const owner of [scope.WebGLRenderingContext, scope.WebGL2RenderingContext]) {
      wrapMethod(owner, 'getParameter', (target, self, args) => {
        const value = Reflect.apply(target, self, args);
        if (value === null) return value; // расширение не запрошено или контекст потерян
        if (args[0] === UNMASKED_VENDOR) return FP.webgl.vendor;
        if (args[0] === UNMASKED_RENDERER) return FP.webgl.renderer;
        return value;
      });
    }
    // WebGPU назвал бы настоящую видеокарту. Отвечаем как на железе без поддержки.
    wrapMethod(scope.GPU, 'requestAdapter', () => Promise.resolve(null));
  }

  // --- Canvas: меняем младший бит у части пикселей ----------------------------
  if (FP.canvasNoise) {
    const seed = FP.seed | 0;
    const noisePixels = (data, width) => {
      // data: RGBA. Трогаем примерно каждый 16-й непрозрачный пиксель.
      for (let i = 0, p = 0; i < data.length; i += 4, p++) {
        if (data[i + 3] === 0) continue;
        const h = hash(seed, p + width * 131);
        if ((h & 15) !== 0) continue;
        data[i + ((h >>> 4) % 3)] ^= 1;
      }
    };
    const Offscreen = scope.OffscreenCanvas;
    const contexts = [scope.CanvasRenderingContext2D, scope.OffscreenCanvasRenderingContext2D].filter(Boolean);
    const original2d = contexts.map((owner) => ({
      owner,
      getImageData: owner.prototype.getImageData,
      putImageData: owner.prototype.putImageData,
      drawImage: owner.prototype.drawImage,
    }));
    const createElement = inPage ? Document.prototype.createElement : null;

    // Копия холста с шумом; оригинал не трогаем, чтобы шум не накапливался.
    const noisyCopy = (canvas) => {
      const { width, height } = canvas;
      if (!width || !height) return null;
      try {
        const isOffscreen = Offscreen && canvas instanceof Offscreen;
        const copy = isOffscreen ? new Offscreen(width, height) : createElement.call(document, 'canvas');
        if (!isOffscreen) {
          copy.width = width;
          copy.height = height;
        }
        const ctx = copy.getContext('2d');
        const api = original2d.find((entry) => ctx instanceof entry.owner);
        api.drawImage.call(ctx, canvas, 0, 0);
        const image = api.getImageData.call(ctx, 0, 0, width, height);
        noisePixels(image.data, width);
        api.putImageData.call(ctx, image, 0, 0);
        return copy;
      } catch (e) {
        return null; // например, холст «загрязнён» чужой картинкой
      }
    };
    const viaCopy = (target, self, args) => Reflect.apply(target, noisyCopy(self) || self, args);

    if (scope.HTMLCanvasElement) {
      wrapMethod(scope.HTMLCanvasElement, 'toDataURL', (t, self, a) => (self instanceof scope.HTMLCanvasElement ? viaCopy(t, self, a) : Reflect.apply(t, self, a)));
      wrapMethod(scope.HTMLCanvasElement, 'toBlob', (t, self, a) => (self instanceof scope.HTMLCanvasElement ? viaCopy(t, self, a) : Reflect.apply(t, self, a)));
    }
    if (Offscreen) {
      wrapMethod(Offscreen, 'convertToBlob', (t, self, a) => (self instanceof Offscreen ? viaCopy(t, self, a) : Reflect.apply(t, self, a)));
    }
    for (const owner of contexts) {
      wrapMethod(owner, 'getImageData', (target, self, args) => {
        const image = Reflect.apply(target, self, args);
        noisePixels(image.data, image.width);
        return image;
      });
    }
    for (const owner of [scope.WebGLRenderingContext, scope.WebGL2RenderingContext]) {
      wrapMethod(owner, 'readPixels', (target, self, args) => {
        const result = Reflect.apply(target, self, args);
        const pixels = args[6];
        if (pixels instanceof Uint8Array || pixels instanceof Uint8ClampedArray) noisePixels(pixels, args[2] | 0);
        return result;
      });
    }
  }

  // --- Аудио: микроскопический сдвиг сэмплов ----------------------------------
  if (FP.audioNoise && scope.AudioBuffer) {
    const seed = (FP.seed | 0) ^ 0x5bd1e995;
    const noiseSamples = (samples) => {
      for (let i = 0; i < samples.length; i += 97) {
        samples[i] += ((hash(seed, i) & 0xffff) / 0xffff - 0.5) * 2e-7;
      }
    };
    const touched = new WeakMap(); // AudioBuffer -> Set(номер канала)

    wrapMethod(scope.AudioBuffer, 'getChannelData', (target, self, args) => {
      const data = Reflect.apply(target, self, args);
      let channels = touched.get(self);
      if (!channels) touched.set(self, (channels = new Set()));
      if (!channels.has(args[0])) {
        channels.add(args[0]);
        noiseSamples(data);
      }
      return data;
    });
    wrapMethod(scope.AudioBuffer, 'copyFromChannel', (target, self, args) => {
      const result = Reflect.apply(target, self, args);
      const channels = touched.get(self);
      // Если канал уже зашумлён через getChannelData, копия уже содержит шум.
      if (args[0] instanceof Float32Array && !(channels && channels.has(args[1]))) noiseSamples(args[0]);
      return result;
    });
    wrapMethod(scope.AnalyserNode, 'getFloatFrequencyData', (target, self, args) => {
      const result = Reflect.apply(target, self, args);
      if (args[0] instanceof Float32Array) {
        for (let i = 0; i < args[0].length; i += 7) args[0][i] += ((hash(seed, i) & 0xff) / 0xff - 0.5) * 1e-3;
      }
      return result;
    });
  }
})();
