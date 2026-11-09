// Подмена отпечатка. Выполняется в каждой странице и фрейме до скриптов сайта.
// __FINGERPRINT__ заменяется на JSON профиля (см. fingerprint.py).
(() => {
  'use strict';
  const FP = __FINGERPRINT__;

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

  const wrapMethod = (proto, name, handler) => {
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

  const wrapGetter = (proto, name, getValue) => {
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

  // --- Железо и языки ---------------------------------------------------------
  if (typeof Navigator !== 'undefined') {
    if (FP.hardwareConcurrency) wrapGetter(Navigator.prototype, 'hardwareConcurrency', () => FP.hardwareConcurrency);
    if (FP.deviceMemory) wrapGetter(Navigator.prototype, 'deviceMemory', () => FP.deviceMemory);
    if (Array.isArray(FP.languages) && FP.languages.length) {
      const languages = Object.freeze([...FP.languages]);
      wrapGetter(Navigator.prototype, 'languages', () => languages);
      wrapGetter(Navigator.prototype, 'language', () => languages[0]);
    }
  }

  // --- Экран: размер задаёт движок (эмуляция Playwright), здесь рабочая область
  if (FP.screen && typeof Screen !== 'undefined') {
    wrapGetter(Screen.prototype, 'availWidth', () => FP.screen.availWidth);
    wrapGetter(Screen.prototype, 'availHeight', () => FP.screen.availHeight);
    wrapGetter(Screen.prototype, 'availLeft', () => 0);
    wrapGetter(Screen.prototype, 'availTop', () => 0);
    wrapGetter(Screen.prototype, 'colorDepth', () => FP.screen.colorDepth);
    wrapGetter(Screen.prototype, 'pixelDepth', () => FP.screen.colorDepth);
  }

  // --- WebGL: производитель и модель видеокарты -------------------------------
  if (FP.webgl) {
    const UNMASKED_VENDOR = 0x9245;
    const UNMASKED_RENDERER = 0x9246;
    const patchGetParameter = (proto) =>
      wrapMethod(proto, 'getParameter', (target, self, args) => {
        const value = Reflect.apply(target, self, args);
        if (value === null) return value; // расширение не запрошено или контекст потерян
        if (args[0] === UNMASKED_VENDOR) return FP.webgl.vendor;
        if (args[0] === UNMASKED_RENDERER) return FP.webgl.renderer;
        return value;
      });
    if (typeof WebGLRenderingContext !== 'undefined') patchGetParameter(WebGLRenderingContext.prototype);
    if (typeof WebGL2RenderingContext !== 'undefined') patchGetParameter(WebGL2RenderingContext.prototype);
  }

  // --- Canvas: меняем младший бит у части пикселей ----------------------------
  if (FP.canvasNoise && typeof HTMLCanvasElement !== 'undefined') {
    const seed = FP.seed | 0;
    const noisePixels = (data, width) => {
      // data: Uint8ClampedArray RGBA. Трогаем примерно каждый 16-й непрозрачный пиксель.
      for (let i = 0, p = 0; i < data.length; i += 4, p++) {
        if (data[i + 3] === 0) continue;
        const h = hash(seed, p + width * 131);
        if ((h & 15) !== 0) continue;
        const channel = (h >>> 4) % 3;
        data[i + channel] ^= 1;
      }
    };

    const getImageData = CanvasRenderingContext2D.prototype.getImageData;
    const putImageData = CanvasRenderingContext2D.prototype.putImageData;
    const drawImage = CanvasRenderingContext2D.prototype.drawImage;
    const getContext = HTMLCanvasElement.prototype.getContext;
    const createElement = Document.prototype.createElement;

    // Копия холста с шумом; оригинал не трогаем, чтобы шум не накапливался.
    const noisyCopy = (canvas) => {
      const { width, height } = canvas;
      if (!width || !height) return null;
      try {
        const copy = createElement.call(document, 'canvas');
        copy.width = width;
        copy.height = height;
        const ctx = getContext.call(copy, '2d');
        drawImage.call(ctx, canvas, 0, 0);
        const image = getImageData.call(ctx, 0, 0, width, height);
        noisePixels(image.data, width);
        putImageData.call(ctx, image, 0, 0);
        return copy;
      } catch (e) {
        return null; // например, холст «загрязнён» чужой картинкой
      }
    };

    wrapMethod(HTMLCanvasElement.prototype, 'toDataURL', (target, self, args) => {
      const copy = self instanceof HTMLCanvasElement ? noisyCopy(self) : null;
      return Reflect.apply(target, copy || self, args);
    });
    wrapMethod(HTMLCanvasElement.prototype, 'toBlob', (target, self, args) => {
      const copy = self instanceof HTMLCanvasElement ? noisyCopy(self) : null;
      return Reflect.apply(target, copy || self, args);
    });
    wrapMethod(CanvasRenderingContext2D.prototype, 'getImageData', (target, self, args) => {
      const image = Reflect.apply(target, self, args);
      noisePixels(image.data, image.width);
      return image;
    });

    const patchReadPixels = (proto) =>
      wrapMethod(proto, 'readPixels', (target, self, args) => {
        const result = Reflect.apply(target, self, args);
        const pixels = args[6];
        if (pixels instanceof Uint8Array || pixels instanceof Uint8ClampedArray) {
          noisePixels(pixels, args[2] | 0);
        }
        return result;
      });
    if (typeof WebGLRenderingContext !== 'undefined') patchReadPixels(WebGLRenderingContext.prototype);
    if (typeof WebGL2RenderingContext !== 'undefined') patchReadPixels(WebGL2RenderingContext.prototype);
  }

  // --- Аудио: микроскопический сдвиг сэмплов ----------------------------------
  if (FP.audioNoise && typeof AudioBuffer !== 'undefined') {
    const seed = (FP.seed | 0) ^ 0x5bd1e995;
    const noiseSamples = (samples) => {
      for (let i = 0; i < samples.length; i += 97) {
        const h = hash(seed, i);
        samples[i] += ((h & 0xffff) / 0xffff - 0.5) * 2e-7;
      }
    };
    const touched = new WeakMap(); // AudioBuffer -> Set(номер канала)

    wrapMethod(AudioBuffer.prototype, 'getChannelData', (target, self, args) => {
      const data = Reflect.apply(target, self, args);
      let channels = touched.get(self);
      if (!channels) touched.set(self, (channels = new Set()));
      if (!channels.has(args[0])) {
        channels.add(args[0]);
        noiseSamples(data);
      }
      return data;
    });
    wrapMethod(AudioBuffer.prototype, 'copyFromChannel', (target, self, args) => {
      const result = Reflect.apply(target, self, args);
      const channels = touched.get(self);
      // Если канал уже зашумлён через getChannelData, копия уже содержит шум.
      if (args[0] instanceof Float32Array && !(channels && channels.has(args[1]))) {
        noiseSamples(args[0]);
      }
      return result;
    });
    if (typeof AnalyserNode !== 'undefined') {
      wrapMethod(AnalyserNode.prototype, 'getFloatFrequencyData', (target, self, args) => {
        const result = Reflect.apply(target, self, args);
        if (args[0] instanceof Float32Array) {
          for (let i = 0; i < args[0].length; i += 7) {
            args[0][i] += ((hash(seed, i) & 0xff) / 0xff - 0.5) * 1e-3;
          }
        }
        return result;
      });
    }
  }
})();
