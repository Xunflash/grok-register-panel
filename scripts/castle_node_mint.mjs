#!/usr/bin/env node
// Download the Castle chunk and mint in Node. Prints length or the error, never the token.
import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import util from "node:util";
import { fileURLToPath } from "node:url";

Error.prepareStackTrace = (error) => `${error && error.name}: ${error && error.message}`;
const nativeFunctionText = new WeakMap();
{
  // A hooked toString fails the native-name check and stores an empty name.
  const originalToString = Function.prototype.toString;
  const patchedToString = function () {
    if (new.target) throw new TypeError("Function.prototype.toString is not a constructor");
    if (nativeFunctionText.has(this)) return nativeFunctionText.get(this);
    return originalToString.call(this);
  };
  Object.defineProperty(patchedToString, "name", { value: "" });
  Object.defineProperty(Function.prototype, "toString", {
    value: patchedToString,
    writable: true,
    configurable: true,
  });
}
for (const name of ["prompt", "close"]) {
  const fn = function () { return undefined; };
  nativeFunctionText.set(fn, `function ${name}() { [native code] }`);
  globalThis[name] = fn;
}
for (const method of ["toLocaleString", "toLocaleDateString", "toLocaleTimeString"]) {
  const original = Date.prototype[method];
  Date.prototype[method] = function localeFixed(locales, options) {
    return original.call(this, locales === undefined ? "en-US" : locales, options);
  };
}
{
  const OriginalDateTimeFormat = Intl.DateTimeFormat;
  Intl.DateTimeFormat = function DateTimeFormat(locales, options) {
    return new OriginalDateTimeFormat(locales === undefined ? "en-US" : locales, options);
  };
  Intl.DateTimeFormat.prototype = OriginalDateTimeFormat.prototype;
  Intl.DateTimeFormat.supportedLocalesOf = OriginalDateTimeFormat.supportedLocalesOf.bind(OriginalDateTimeFormat);
}
for (const name of ["log", "info", "warn", "error", "debug"]) {
  const original = console[name].bind(console);
  console[name] = (...args) => {
    const printable = [];
    for (const arg of args) {
      if (arg instanceof Error) {
        try {
          for (const key of Object.getOwnPropertyNames(arg)) void arg[key];
        } catch {
          /* probe getter */
        }
        continue;
      }
      printable.push(typeof arg === "string" ? arg.slice(0, 180) : arg);
    }
    if (printable.length) original(...printable);
  };
}
process.on("uncaughtException", (err) => {
  console.log("uncaught", String((err && err.message) || err).slice(0, 160));
});
process.on("unhandledRejection", (err) => {
  console.log("unhandled", String((err && err.message) || err).slice(0, 160));
});
function loadCastleFp() {
  const file = process.env.CASTLE_FP_FILE || "";
  if (!file) return null;
  try {
    const parsed = JSON.parse(fs.readFileSync(file, "utf8"));
    return parsed && typeof parsed === "object" ? parsed : null;
  } catch (err) {
    console.log("fp_error", String((err && err.message) || err).slice(0, 120));
    return null;
  }
}
const castleFp = loadCastleFp() || {};
if (castleFp.timezone_id) process.env.TZ = String(castleFp.timezone_id);
const castleHints = castleFp.ua_metadata || {};
const castleFull = String(castleHints.fullVersion || "145.0.7632.159");
const castleMajor = castleFull.split(".")[0] || "145";
const castleUa =
  String(castleFp.user_agent || "") ||
  `Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/${castleFull} Safari/537.36`;
const castleScreen =
  Array.isArray(castleFp.screen) && castleFp.screen.length === 2
    ? castleFp.screen.map((part) => Number(part) || 0)
    : [1920, 1080];
// Layout box of documentElement and body. The captured token recorded 1536x864
// while screen stayed 1920x1080.
const castleViewport =
  Array.isArray(castleFp.viewport) && castleFp.viewport.length === 2
    ? castleFp.viewport.map((part) => Number(part) || 0)
    : castleScreen;
const webglVendor = String(castleFp.webgl_vendor || "Google Inc. (Intel)");
const webglRenderer = String(
  castleFp.webgl_renderer || "ANGLE (Intel, Intel(R) HD Graphics Direct3D11 vs_5_0 ps_5_0)",
);
console.log(
  "fp",
  castleFp.user_agent ? "profile" : "default",
  `screen=${castleScreen[0]}x${castleScreen[1]}`,
  `tz=${castleFp.timezone_id || "server"}`,
  `hw=${castleFp.hardware_concurrency || 8}/${castleFp.device_memory || 8}`,
  `arch=${castleHints.architecture || "x86"}`,
  `full=${castleFull}`,
);
if (process.env.CASTLE_FP_ONLY === "1") process.exit(0);
const SDK_URL = "https://accounts.x.ai/_next/static/chunks/02bh2mv7efkj1.js";
const dest = process.argv[2] || "/tmp/castle-sdk.js";

const response = await fetch(SDK_URL, {
  headers: {
    "user-agent": castleUa,
  },
});
if (!response.ok) {
  console.log("download", response.status);
  process.exit(1);
}
const sdk = await response.text();
fs.writeFileSync(dest, sdk);
console.log("bytes", sdk.length);

const blobSources = new Map();
let blobSeq = 0;

const mintDir = path.dirname(fileURLToPath(import.meta.url));
const chromePython = process.env.CASTLE_PYTHON
  || path.join(mintDir, "..", ".venv", "bin", "python");
const chromeHelper = path.join(mintDir, "chrome_deflate_raw.py");
let chromeProc = null;
let chromeChain = Promise.resolve();
let chromeBuffer = Buffer.alloc(0);
const chromeReaders = [];

function pumpChromeReads() {
  while (chromeReaders.length) {
    const reader = chromeReaders[0];
    if (chromeBuffer.length < reader.size) return;
    const chunk = Buffer.from(chromeBuffer.subarray(0, reader.size));
    chromeBuffer = chromeBuffer.subarray(reader.size);
    chromeReaders.shift();
    reader.resolve(chunk);
  }
}

function readChrome(size) {
  return new Promise((resolve, reject) => {
    chromeReaders.push({ size, resolve, reject });
    pumpChromeReads();
  });
}

function ensureChromeDeflate() {
  if (chromeProc && !chromeProc.killed) return;
  chromeProc = spawn(chromePython, ["-u", chromeHelper], {
    stdio: ["pipe", "pipe", "pipe"],
    env: { ...process.env, PYTHONWARNINGS: "ignore" },
  });
  chromeProc.stdout.on("data", (chunk) => {
    chromeBuffer = Buffer.concat([chromeBuffer, chunk]);
    pumpChromeReads();
  });
  chromeProc.stderr.on("data", (chunk) => {
    const text = String(chunk || "").trim();
    if (text) console.log("deflate_err", text.slice(0, 160));
  });
  chromeProc.on("exit", (code) => {
    const error = new Error(`chrome deflate exited ${code}`);
    while (chromeReaders.length) chromeReaders.shift().reject(error);
    chromeProc = null;
  });
}

function chromeDeflateRaw(bytes) {
  ensureChromeDeflate();
  const run = chromeChain.then(async () => {
    const header = Buffer.alloc(4);
    header.writeUInt32BE(bytes.length);
    chromeProc.stdin.write(header);
    chromeProc.stdin.write(bytes);
    const lenBuf = await readChrome(4);
    const size = lenBuf.readUInt32BE(0);
    const output = await readChrome(size);
    console.log("deflate", "chrome", bytes.length, output.length);
    return output;
  });
  chromeChain = run.then(() => {}, () => {});
  return run;
}

function stopChromeDeflate() {
  const proc = chromeProc;
  chromeProc = null;
  if (!proc) return;
  try { proc.stdin.end(); } catch { /* already closed */ }
  try { proc.kill(); } catch { /* already gone */ }
}

process.on("exit", () => stopChromeDeflate());

const NativeCompressionStream = globalThis.CompressionStream;
globalThis.CompressionStream = class ChromeCompressionStream {
  constructor(format) {
    if (format !== "deflate-raw") {
      const inner = new NativeCompressionStream(format);
      this.readable = inner.readable;
      this.writable = inner.writable;
      return;
    }
    const chunks = [];
    let controller;
    this.readable = new ReadableStream({
      start(control) { controller = control; },
    });
    this.writable = new WritableStream({
      write(chunk) {
        chunks.push(Buffer.from(chunk.buffer, chunk.byteOffset, chunk.byteLength));
      },
      async close() {
        try {
          const output = await chromeDeflateRaw(Buffer.concat(chunks));
          controller.enqueue(new Uint8Array(output));
          controller.close();
        } catch (error) {
          controller.error(error);
        }
      },
    });
  }
};

if (process.env.CASTLE_DEFLATE_FILE) {
  const sample = fs.readFileSync(process.env.CASTLE_DEFLATE_FILE);
  const compressed = await chromeDeflateRaw(sample);
  console.log("deflate_check", sample.length, compressed.length);
  try { chromeProc?.kill(); } catch { /* already gone */ }
  process.exit(0);
}

const NativeBlob = globalThis.Blob;
class CastleBlob {
  constructor(parts) {
    const chunks = [];
    for (const part of parts || []) {
      if (typeof part === "string") chunks.push(Buffer.from(part));
      else if (part == null) chunks.push(Buffer.alloc(0));
      else chunks.push(Buffer.from(part));
    }
    this._bytes = Buffer.concat(chunks);
    this._text = this._bytes.toString("utf8");
  }
  get size() {
    return this._bytes.length;
  }
  stream() {
    return new NativeBlob([this._bytes]).stream();
  }
}

globalThis.Blob = CastleBlob;
if (typeof globalThis.URL !== "function") {
  globalThis.URL = class URL {};
}
globalThis.URL.createObjectURL = (blob) => {
  const id = `blob:castle/${++blobSeq}`;
  const text = blob && blob._text ? blob._text : "";
  blobSources.set(id, text);
  if (text.length > 1500) console.log("plain_blob", text.length, text.charCodeAt(0), text.charCodeAt(1));
  return id;
};
globalThis.URL.revokeObjectURL = (id) => {
  blobSources.delete(id);
};

function summarizeMessage(data) {
  if (Array.isArray(data)) return `arr ${typeof data[0] === "number" ? data[0] : "x"} ${data.length}`;
  if (typeof data === "string") return `str ${data.length}`;
  return typeof data;
}

function callHandler(fn, event) {
  try {
    const result = fn(event);
    if (result && typeof result.then === "function") {
      result.catch((err) => console.log("worker_async_error", String((err && err.message) || err).slice(0, 160)));
    }
  } catch (err) {
    console.log("worker_sync_error", String((err && err.message) || err).slice(0, 160));
  }
}

function deliver(target, data) {
  if (Array.isArray(data)) console.log("worker_reply", summarizeMessage(data));
  const event = { data, type: "message" };
  queueMicrotask(() => {
    if (typeof target.onmessage === "function") callHandler(target.onmessage, event);
    for (const fn of target.listeners || []) callHandler(fn, event);
  });
}

class MessagePort {
  constructor() {
    this.onmessage = null;
    this.listeners = [];
    this.peer = null;
  }
  start() {}
  close() {}
  addEventListener(type, fn) {
    if (type === "message") this.listeners.push(fn);
  }
  postMessage(data) {
    if (this.peer) deliver(this.peer, data);
  }
}

function linkPorts() {
  const outside = new MessagePort();
  const inside = new MessagePort();
  outside.peer = inside;
  inside.peer = outside;
  return { outside, inside };
}

function makeUserAgentData(platform, platformVersion, options = {}) {
  const brands = options.brands || [
    { brand: "Chromium", version: castleMajor },
    { brand: "Not:A-Brand", version: "99" },
  ];
  const fullVersionList = options.fullVersionList || [
    { brand: "Chromium", version: castleFull },
    { brand: "Not:A-Brand", version: "99.0.0.0" },
  ];
  return {
    brands,
    mobile: false,
    platform,
    getHighEntropyValues(hints) {
      const full = {
        architecture: "x86",
        bitness: "64",
        model: "",
        platform,
        platformVersion,
        wow64: false,
        uaFullVersion: castleFull,
        fullVersionList,
      };
      // Page collector calls getHighEntropyValues([]) and reads the full object.
      if (!options.honorHints) return Promise.resolve(full);
      const wanted = new Set(Array.isArray(hints) ? hints.map(String) : []);
      const result = { brands, mobile: false, platform };
      for (const hint of wanted) {
        if (Object.prototype.hasOwnProperty.call(full, hint)) result[hint] = full[hint];
      }
      return Promise.resolve(result);
    },
  };
}
const workerNavigator = {
  userAgent:
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
  appVersion: "5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
  platform: "Linux x86_64",
  vendor: "Google Inc.",
  language: "en-US",
  languages: ["en-US"],
  productSub: "20030107",
  vendorSub: "",
  appName: "Netscape",
  appCodeName: "Mozilla",
  product: "Gecko",
  deviceMemory: Number(castleFp.device_memory) || 8,
  hardwareConcurrency: Number(castleFp.hardware_concurrency) || 8,
  webdriver: false,
  // SharedWorker is the real Linux Chrome. Empty platformVersion is stored as NA.
  userAgentData: makeUserAgentData("Linux", "", { honorHints: true }),
};
const dedicatedNavigator = {
  ...workerNavigator,
  // Dedicated worker keeps the page metadata. u4() reduces brands to Google Chrome.
  userAgentData: makeUserAgentData("Windows", "15.0.0", {
    honorHints: true,
    brands: [{ brand: "Google Chrome", version: castleMajor }],
  }),
};
const pageNavigator = {
  ...workerNavigator,
  userAgent: castleUa,
  appVersion: castleUa.replace(/^Mozilla\//, ""),
  platform: String(castleFp.platform || "Win32"),
  userAgentData: makeUserAgentData("Windows", "15.0.0"),
};

function webglParameter(name) {
  // The SDK passes "37446" as a string for slot 60. The successful browser
  // returns the vendor for that call and the renderer only for numeric 37446.
  if (typeof name === "string" && (name === "37445" || name === "37446")) return webglVendor;
  const code = Number(name);
  if (code === 37445) return webglVendor;
  if (code === 37446) return webglRenderer;
  return 0;
}
function rememberShaderSource(shader, source) {
  if (shader && typeof shader === "object") shader.source = String(source ?? "");
}
const ANGLE_SHADER_TRANSLATION =
  "Inputs: p\nOutputs: gl_Position\n\nPaste the following SPIR-V binary in https://www.khronos.org/spir/visualizer/ or pass to";
function translatedShader(shader) {
  const source = shader && shader.source ? String(shader.source) : "";
  if (source.includes("gl_Position") && source.length < 240) return ANGLE_SHADER_TRANSLATION;
  return source;
}
function webglExtension(name) {
  const label = String(name || "");
  if (label.includes("renderer")) {
    return { UNMASKED_VENDOR_WEBGL: 37445, UNMASKED_RENDERER_WEBGL: 37446 };
  }
  if (label.includes("shader") || label.includes("debug")) {
    return { getTranslatedShaderSource: translatedShader };
  }
  return null;
}
function makeWebGLContext() {
  const base = {
    VERTEX_SHADER: 35633,
    FRAGMENT_SHADER: 35632,
    createShader() {
      return { source: "" };
    },
    shaderSource: rememberShaderSource,
    compileShader() {},
    getExtension: webglExtension,
    getParameter: webglParameter,
    getTranslatedShaderSource: translatedShader,
  };
  return new Proxy(base, {
    get(target, prop, receiver) {
      if (Reflect.has(target, prop)) return Reflect.get(target, prop, receiver);
      if (typeof prop === "symbol") return undefined;
      return () => 0;
    },
  });
}
function make2DContext() {
  const base = {
    fillRect() {},
    strokeRect() {},
    fillText() {},
    beginPath() {},
    closePath() {},
    fill() {},
    stroke() {},
    arc() {},
    moveTo() {},
    bezierCurveTo() {},
    getImageData() {
      return { data: new Uint8ClampedArray(4) };
    },
    measureText() {
      return { width: 0 };
    },
  };
  return new Proxy(base, {
    get(target, prop, receiver) {
      if (Reflect.has(target, prop)) return Reflect.get(target, prop, receiver);
      if (typeof prop === "symbol") return undefined;
      return () => 0;
    },
  });
}
function canvasContext(kind) {
  return String(kind || "").includes("webgl") ? makeWebGLContext() : make2DContext();
}
class OffscreenCanvas {
  constructor(width, height) {
    this.width = width;
    this.height = height;
  }
  getContext(kind) {
    return canvasContext(kind);
  }
}
globalThis.OffscreenCanvas = OffscreenCanvas;
globalThis.Notification = function Notification() {};
globalThis.CanvasRenderingContext2D = function CanvasRenderingContext2D() {};
globalThis.CanvasRenderingContext2D.prototype.getImageData = function getImageData() {
  throw new TypeError("Illegal invocation");
};
globalThis.HTMLCanvasElement = function HTMLCanvasElement() {};
globalThis.HTMLCanvasElement.prototype.toDataURL = function toDataURL() {
  return "data:,";
};

function makeWorkerGlobal(reply, navigator = workerNavigator) {
  const listeners = { message: [], connect: [], error: [] };
  const selfObj = {
    navigator,
    OffscreenCanvas,
    location: globalThis.location,
    performance: globalThis.performance,
    Date,
    Math,
    JSON,
    Promise,
    console: { log() {}, warn() {}, error() {}, info() {}, debug() {} },
    onmessage: null,
    onconnect: null,
    onerror: null,
    close() {},
    postMessage(data) {
      reply(data);
    },
    addEventListener(type, fn) {
      (listeners[type] || (listeners[type] = [])).push(fn);
    },
    removeEventListener(type, fn) {
      const list = listeners[type] || [];
      const index = list.indexOf(fn);
      if (index >= 0) list.splice(index, 1);
    },
    dispatchEvent(event) {
      const type = (event && event.type) || "message";
      const named = selfObj["on" + type];
      if (typeof named === "function") callHandler(named, event);
      for (const fn of listeners[type] || []) callHandler(fn, event);
      return true;
    },
    importScripts() {},
  };
  Object.assign(selfObj, {
    Array,
    Object,
    String,
    Number,
    Boolean,
    Error,
    TypeError,
    Uint8Array,
    ArrayBuffer,
    setTimeout,
    clearTimeout,
    setInterval,
    clearInterval,
    queueMicrotask,
    atob: globalThis.atob,
    btoa: globalThis.btoa,
    TextEncoder,
    TextDecoder,
    crypto: globalThis.crypto,
    parseInt,
    parseFloat,
    isNaN,
    isFinite,
  });
  selfObj.self = selfObj;
  selfObj.globalThis = selfObj;
  return vm.createContext(selfObj);
}

function runWorkerSource(code, selfObj) {
  vm.runInContext(String(code || "").replace(/\bdebugger\b;?/g, ""), selfObj, { timeout: 1000 });
}

class WorkerShim {
  constructor(url) {
    this.onmessage = null;
    this.listeners = [];
    const code = blobSources.get(String(url)) || "";
    console.log("worker", code.length);
    this._self = makeWorkerGlobal((data) => deliver(this, data), dedicatedNavigator);
    try {
      runWorkerSource(code, this._self);
    } catch (err) {
      console.log("worker_load_error", String((err && err.message) || err).slice(0, 160));
    }
  }
  postMessage(data) {
    this._self.dispatchEvent({ type: "message", data });
  }
  terminate() {}
  addEventListener(type, fn) {
    if (type === "message") this.listeners.push(fn);
  }
}

class SharedWorkerShim {
  constructor(url) {
    const code = blobSources.get(String(url)) || "";
    console.log("shared_worker", code.length);
    const { outside, inside } = linkPorts();
    this.port = outside;
    const selfObj = makeWorkerGlobal((data) => deliver(outside, data));
    try {
      runWorkerSource(code, selfObj);
    } catch (err) {
      console.log("shared_worker_load_error", String((err && err.message) || err).slice(0, 160));
    }
    selfObj.dispatchEvent({ type: "connect", ports: [inside] });
  }
}

if (typeof globalThis.Event !== "function") {
  globalThis.Event = class Event {
    constructor(type) {
      this.type = type;
    }
  };
}
if (typeof globalThis.ErrorEvent !== "function") {
  globalThis.ErrorEvent = class ErrorEvent extends Event {
    constructor(type, init = {}) {
      super(type || "error");
      this.message = init.message || "";
      this.filename = init.filename || "";
      this.lineno = init.lineno || 0;
      this.colno = init.colno || 0;
      this.error = init.error || null;
    }
  };
}
globalThis.Worker = WorkerShim;
globalThis.SharedWorker = SharedWorkerShim;

globalThis.window = globalThis;
globalThis.self = globalThis;
globalThis.innerWidth = castleViewport[0];
globalThis.innerHeight = castleViewport[1];
globalThis.outerWidth = castleScreen[0];
globalThis.outerHeight = castleScreen[1];
globalThis.devicePixelRatio = 1;
if (typeof globalThis.requestAnimationFrame !== "function") {
  globalThis.requestAnimationFrame = (fn) => setTimeout(() => fn(performance.now()), 16);
  globalThis.cancelAnimationFrame = (id) => clearTimeout(id);
}
if (!globalThis.screen) {
  globalThis.screen = {
    width: castleScreen[0],
    height: castleScreen[1],
    availWidth: castleScreen[0],
    availHeight: Math.max(castleScreen[1] - 40, 600),
    colorDepth: 24,
    pixelDepth: 24,
    orientation: {
      type: "landscape-primary",
      angle: 0,
      addEventListener() {},
      removeEventListener() {},
    },
  };
}
function defineChrome() {
  const chrome = {};
  const define = (key, value) => Object.defineProperty(chrome, key, { value, configurable: true });
  define("app", {
    isInstalled: false,
    getDetails() { return null; },
    getIsInstalled() { return false; },
    installState() { return "disabled"; },
  });
  define("runtime", { connect() {}, sendMessage() {} });
  define("csi", () => ({}));
  define("loadTimes", () => ({}));
  globalThis.chrome = chrome;
}
defineChrome();
if (typeof globalThis.SerialPort !== "function") globalThis.SerialPort = function SerialPort() {};
if (typeof globalThis.GPUCanvasContext !== "function") globalThis.GPUCanvasContext = function GPUCanvasContext() {};
if (typeof globalThis.AudioContext !== "function") globalThis.AudioContext = function AudioContext() {};
if (typeof globalThis.webkitTemporaryStorage === "undefined") globalThis.webkitTemporaryStorage = {};
if (typeof performance.getEntriesByType === "function") {
  const originalEntries = performance.getEntriesByType.bind(performance);
  performance.getEntriesByType = (type) => {
    if (String(type) === "navigation") {
      return [{ type: "navigate", name: "https://accounts.x.ai/sign-up?redirect=grok-com", entryType: "navigation" }];
    }
    return originalEntries(type);
  };
}
globalThis.styleMedia = { type: "screen" };
globalThis.matchMedia = (query) => ({
  matches: true,
  media: String(query || "screen"),
  addEventListener() {},
  removeEventListener() {},
});
globalThis.getComputedStyle = () => computedStyle();
function makeEventTarget() {
  const listeners = new Map();
  return {
    counts: listeners,
    addEventListener(type, fn) {
      if (typeof fn !== "function") return;
      const key = String(type);
      const list = listeners.get(key) || [];
      list.push(fn);
      listeners.set(key, list);
    },
    removeEventListener(type, fn) {
      const key = String(type);
      const list = listeners.get(key) || [];
      listeners.set(
        key,
        list.filter((item) => item !== fn),
      );
    },
    dispatchEvent(event) {
      const list = (listeners.get(String(event && event.type)) || []).slice();
      for (const fn of list) {
        try {
          fn(event);
        } catch (err) {
          console.log("event_error", String((err && err.message) || err).slice(0, 120));
        }
      }
      return true;
    },
  };
}
const windowEvents = makeEventTarget();
const documentEvents = makeEventTarget();
globalThis.addEventListener = windowEvents.addEventListener;
globalThis.removeEventListener = windowEvents.removeEventListener;
globalThis.dispatchEvent = windowEvents.dispatchEvent;
if (!globalThis.location) {
  globalThis.location = {
    href: "https://accounts.x.ai/sign-up?redirect=grok-com",
    hostname: "accounts.x.ai",
    origin: "https://accounts.x.ai",
    protocol: "https:",
    host: "accounts.x.ai",
    pathname: "/sign-up",
    search: "?redirect=grok-com",
    port: "",
  };
}
function loadStyleProfile() {
  const candidates = [];
  if (process.env.CASTLE_STYLE_FILE) candidates.push(process.env.CASTLE_STYLE_FILE);
  try {
    candidates.push(new URL("./castle-style-props.json", import.meta.url));
  } catch {
    /* path comes from the fallback */
  }
  candidates.push("/tmp/css-props.json");
  for (const path of candidates) {
    try {
      const parsed = JSON.parse(fs.readFileSync(path, "utf8"));
      if (Array.isArray(parsed) && parsed.length > 100) return { names: parsed, sheets: [], scripts: [], elements: [], marks: [] };
      if (parsed && Array.isArray(parsed.names) && parsed.names.length > 100) {
        return {
          names: parsed.names,
          sheets: Array.isArray(parsed.sheets) ? parsed.sheets : [],
          scripts: Array.isArray(parsed.scripts) ? parsed.scripts : [],
          elements: Array.isArray(parsed.elements) ? parsed.elements : [],
          marks: Array.isArray(parsed.marks) ? parsed.marks : [],
        };
      }
    } catch {
      /* try the next file */
    }
  }
  return { names: [], sheets: [], scripts: [], elements: [], marks: [] };
}
const styleProfile = loadStyleProfile();
const styleNames = styleProfile.names;
const pageElements = [];
const pageScripts = [];
const styleSheets = styleProfile.sheets.map((row) => {
  const selector = String(row.selector || "");
  const cssTextLength = Number(row.css_text_len) || 0;
  const ruleCount = Number(row.rule_count) || 0;
  return {
    cssRules: {
      length: ruleCount,
      0: {
        selectorText: selector,
        cssText: { length: cssTextLength },
      },
    },
  };
});
function computedStyle() {
  const style = {
    length: styleNames.length,
    getPropertyValue() {
      return "";
    },
    item(index) {
      return styleNames[index] || "";
    },
  };
  for (let index = 0; index < styleNames.length; index += 1) style[index] = styleNames[index];
  return style;
}
function styleObject() {
  const values = { display: "" };
  return new Proxy(values, {
    get(target, prop) {
      if (prop === "getPropertyValue") return (name) => (name in target ? String(target[name]) : "");
      if (prop === "setProperty") {
        return (name, value) => {
          target[name] = String(value);
        };
      }
      if (prop === "removeProperty") {
        return (name) => {
          const previous = target[name];
          delete target[name];
          return previous == null ? "" : String(previous);
        };
      }
      if (typeof prop === "symbol") return undefined;
      if (prop in target) return target[prop];
      return "";
    },
    set(target, prop, value) {
      target[prop] = value;
      return true;
    },
  });
}

function frameWindow() {
  const win = {
    Date,
    Math,
    JSON,
    Object,
    Array,
    String,
    Number,
    Boolean,
    Promise,
    navigator: workerNavigator,
    location: globalThis.location,
    performance: globalThis.performance,
    setTimeout,
    clearTimeout,
    setInterval,
    clearInterval,
    getComputedStyle: (...args) => globalThis.getComputedStyle(...args),
    innerWidth: castleScreen[0],
    innerHeight: castleScreen[1],
    outerWidth: castleScreen[0],
    outerHeight: castleScreen[1],
    devicePixelRatio: 1,
  };
  win.window = win;
  win.self = win;
  win.top = globalThis;
  win.parent = globalThis;
  win.document = {
    referrer: "https://grok.com/",
    readyState: "complete",
    visibilityState: "visible",
    hidden: false,
    characterSet: "UTF-8",
    compatMode: "CSS1Compat",
    location: globalThis.location,
    defaultView: win,
    body: null,
    documentElement: null,
    createElement: (tag) => {
      const node = domNode(tag || "div");
      const kind = String(tag || "").toLowerCase();
      if (kind === "audio" || kind === "video") {
        node.canPlayType = (type) => {
          const value = String(type || "").trim().toLowerCase();
          const mime = value.split(";")[0].trim();
          if (!mime || mime.includes(",")) return "";
          if (mime === "video/ogg" || value.includes("theora")) return "";
          if (mime === "audio/x-m4a") return "maybe";
          if (
            mime === "audio/mpeg" ||
            mime === "audio/mp4" ||
            mime === "audio/aac" ||
            mime === "audio/webm" ||
            mime === "audio/ogg" ||
            mime === "audio/wav" ||
            mime === "video/mp4" ||
            mime === "video/webm"
          ) {
            return "probably";
          }
          return "";
        };
      }
      if (kind !== "canvas") return node;
      const gl = makeWebGLContext();
      node.toDataURL = () => "data:,";
      node.VERTEX_SHADER = gl.VERTEX_SHADER;
      node.FRAGMENT_SHADER = gl.FRAGMENT_SHADER;
      node.createShader = gl.createShader;
      node.shaderSource = gl.shaderSource;
      node.compileShader = gl.compileShader;
      node.getContext = (kind) => (String(kind || "").includes("webgl") ? gl : make2DContext());
      return node;
    },
    addEventListener() {},
    removeEventListener() {},
    querySelector() {
      return null;
    },
    querySelectorAll() {
      return [];
    },
    hasFocus() {
      return true;
    },
  };
  return win;
}

const hiddenElementKeys = new Set([
  "nodeName",
  "tagName",
  "style",
  "childNodes",
  "children",
  "parentNode",
  "contentWindow",
  "contentDocument",
  "childElementCount",
  "clientWidth",
  "clientHeight",
  "classList",
  "className",
  "id",
  "attributes",
  "_attrs",
  "addEventListener",
  "removeEventListener",
  "dispatchEvent",
  "setAttribute",
  "getAttribute",
  "hasAttribute",
  "appendChild",
  "removeChild",
  "insertBefore",
  "querySelector",
  "querySelectorAll",
  "getBoundingClientRect",
  "getClientRects",
  "getContext",
]);
function domNode(tag) {
  const frame = frameWindow();
  const tagName = String(tag || "div").toUpperCase();
  const attrs = Object.create(null);
  const raw = {
    nodeName: tagName,
    tagName,
    style: styleObject(),
    childNodes: [],
    children: [],
    parentNode: null,
    contentWindow: frame,
    contentDocument: frame.document,
    _attrs: attrs,
    get childElementCount() {
      return raw.children.length;
    },
    get clientWidth() {
      return castleScreen[0];
    },
    get clientHeight() {
      return castleScreen[1];
    },
    get id() {
      return attrs.id || "";
    },
    set id(value) {
      attrs.id = String(value);
    },
    get className() {
      return attrs.class || "";
    },
    set className(value) {
      attrs.class = String(value);
    },
    classList: {
      add() {},
      remove() {},
      contains() {
        return false;
      },
      toggle() {
        return false;
      },
    },
    addEventListener() {},
    removeEventListener() {},
    dispatchEvent() {},
    setAttribute(name, value) {
      attrs[name] = String(value);
    },
    getAttribute(name) {
      return Object.prototype.hasOwnProperty.call(attrs, name) ? attrs[name] : null;
    },
    hasAttribute(name) {
      return Object.prototype.hasOwnProperty.call(attrs, name);
    },
    appendChild(child) {
      raw.childNodes.push(child);
      raw.children.push(child);
      if (child && typeof child === "object") child.parentNode = raw;
      return child;
    },
    removeChild(child) {
      return child;
    },
    insertBefore(child) {
      return child;
    },
    querySelector() {
      return null;
    },
    querySelectorAll() {
      return [];
    },
    getBoundingClientRect() {
      return {
        x: 0,
        y: 0,
        width: castleViewport[0],
        height: castleViewport[1],
        top: 0,
        left: 0,
        right: castleViewport[0],
        bottom: castleViewport[1],
      };
    },
    getClientRects() {
      return [raw.getBoundingClientRect()];
    },
    getContext() {
      return null;
    },
  };
  frame.document.body = raw;
  // Chrome elements have no own DOM fields. getOwnPropertyNames must see only expandos.
  return new Proxy(raw, {
    get(target, prop, receiver) {
      if (Reflect.has(target, prop)) return Reflect.get(target, prop, receiver);
      if (typeof prop === "symbol" || prop === "then") return undefined;
      return () => receiver;
    },
    ownKeys(target) {
      return Reflect.ownKeys(target).filter((key) => !hiddenElementKeys.has(key));
    },
    getOwnPropertyDescriptor(target, prop) {
      if (hiddenElementKeys.has(prop)) return undefined;
      const descriptor = Reflect.getOwnPropertyDescriptor(target, prop);
      if (descriptor && !descriptor.configurable) descriptor.configurable = true;
      return descriptor;
    },
  });
}
class RTCPeerConnection {
  constructor() {
    this.iceGatheringState = "new";
    this.iceConnectionState = "new";
    this.signalingState = "stable";
    this.connectionState = "new";
    this.localDescription = null;
    this.remoteDescription = null;
    this.onicecandidate = null;
    this.onicegatheringstatechange = null;
    this._listeners = { icecandidate: [] };
    console.log("webrtc_ctor");
  }
  addEventListener(type, fn) {
    (this._listeners[type] || (this._listeners[type] = [])).push(fn);
  }
  removeEventListener() {}
  createDataChannel() {
    return { close() {}, send() {} };
  }
  createOffer() {
    return Promise.resolve({
      type: "offer",
      sdp: [
        "v=0",
        "o=- 1 2 IN IP4 0.0.0.0",
        "s=-",
        "t=0 0",
        "a=group:BUNDLE 0",
        "m=application 9 UDP/DTLS/SCTP webrtc-datachannel",
        "c=IN IP4 0.0.0.0",
        "a=ice-ufrag:castle",
        "a=ice-pwd:castlecastlecastlecastle",
        "a=fingerprint:sha-256 00:11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF",
        "a=setup:actpass",
        "a=mid:0",
        "a=sctp-port:5000",
        "",
      ].join("\r\n"),
    });
  }
  setLocalDescription(description) {
    this.localDescription = description || this.localDescription;
    this.signalingState = "have-local-offer";
    // Leave gathering incomplete. The SDK's 300ms timer then records "-".
    this.iceGatheringState = "gathering";
    return Promise.resolve();
  }
  close() {
    this.signalingState = "closed";
  }
}
globalThis.RTCPeerConnection = RTCPeerConnection;
if (!globalThis.document) {
  const documentRaw = {
    currentScript: null,
    location: globalThis.location,
    referrer: "https://grok.com/",
    readyState: "complete",
    visibilityState: "visible",
    hidden: false,
    characterSet: "UTF-8",
    compatMode: "CSS1Compat",
    title: "Create Your Grok Account | Grok",
    cookie: "",
    styleSheets,
    scripts: pageScripts,
    createElement: (tag) => {
      const node = domNode(tag || "div");
      const kind = String(tag || "").toLowerCase();
      if (kind === "audio" || kind === "video") {
        node.canPlayType = (type) => {
          const value = String(type || "").trim().toLowerCase();
          const mime = value.split(";")[0].trim();
          if (!mime || mime.includes(",")) return "";
          if (mime === "video/ogg" || value.includes("theora")) return "";
          if (mime === "audio/x-m4a") return "maybe";
          if (
            mime === "audio/mpeg" ||
            mime === "audio/mp4" ||
            mime === "audio/aac" ||
            mime === "audio/webm" ||
            mime === "audio/ogg" ||
            mime === "audio/wav" ||
            mime === "video/mp4" ||
            mime === "video/webm"
          ) {
            return "probably";
          }
          return "";
        };
      }
      if (kind !== "canvas") return node;
      const gl = makeWebGLContext();
      node.toDataURL = () => "data:,";
      node.VERTEX_SHADER = gl.VERTEX_SHADER;
      node.FRAGMENT_SHADER = gl.FRAGMENT_SHADER;
      node.createShader = gl.createShader;
      node.shaderSource = gl.shaderSource;
      node.compileShader = gl.compileShader;
      node.getContext = (kind) => (String(kind || "").includes("webgl") ? gl : make2DContext());
      return node;
    },
    documentElement: domNode("html"),
    body: domNode("body"),
    head: domNode("head"),
    defaultView: globalThis,
    hasFocus() {
      return true;
    },
    addEventListener: documentEvents.addEventListener,
    removeEventListener: documentEvents.removeEventListener,
    dispatchEvent: documentEvents.dispatchEvent,
    querySelector() {
      return null;
    },
    querySelectorAll(selector) {
      return selector === "*" ? pageElements : [];
    },
    getElementById() {
      return null;
    },
    getElementsByTagName() {
      return [];
    },
  };
  globalThis.document = new Proxy(documentRaw, {
    get(target, prop, receiver) {
      if (Reflect.has(target, prop)) return Reflect.get(target, prop, receiver);
      if (typeof prop === "symbol" || prop === "then") return undefined;
      return undefined;
    },
  });
}
function memoryStorage() {
  const data = new Map();
  return {
    getItem(key) {
      return data.has(String(key)) ? data.get(String(key)) : null;
    },
    setItem(key, value) {
      data.set(String(key), String(value));
    },
    removeItem(key) {
      data.delete(String(key));
    },
    clear() {
      data.clear();
    },
    key(index) {
      return [...data.keys()][index] ?? null;
    },
    get length() {
      return data.size;
    },
  };
}
for (const key of ["localStorage", "sessionStorage"]) {
  try {
    Object.defineProperty(globalThis, key, { value: memoryStorage(), configurable: true, writable: true });
  } catch {
    /* native storage stays */
  }
}
const pdfMimeTypes = [
  { type: "application/pdf", suffixes: "pdf", description: "Portable Document Format" },
  { type: "text/pdf", suffixes: "pdf", description: "Portable Document Format" },
];
const pluginNames = [
  "PDF Viewer",
  "Chrome PDF Viewer",
  "Chromium PDF Viewer",
  "Microsoft Edge PDF Viewer",
  "WebKit built-in PDF",
];
const plugins = pluginNames.map((name) => ({
  name,
  filename: "internal-pdf-viewer",
  description: "Portable Document Format",
  length: pdfMimeTypes.length,
}));
plugins.item = (index) => plugins[index];
plugins.namedItem = (name) => plugins.find((item) => item.name === name) || null;
plugins.refresh = () => {};
const mimeTypes = pdfMimeTypes.slice();
mimeTypes.item = (index) => mimeTypes[index];
mimeTypes.namedItem = (type) => mimeTypes.find((item) => item.type === type) || null;
if (typeof globalThis.Screen !== "function") globalThis.Screen = function Screen() {};
const navigatorOrder = ["vendorSub","productSub","vendor","maxTouchPoints","scheduling","userActivation","geolocation","doNotTrack","webkitTemporaryStorage","webkitPersistentStorage","hardwareConcurrency","cookieEnabled","appCodeName","appName","appVersion","platform","product","userAgent","language","languages","onLine","webdriver","plugins","mimeTypes","pdfViewerEnabled","connection","getGamepads","javaEnabled","sendBeacon","vibrate","windowControlsOverlay","constructor","deprecatedRunAdAuctionEnforcesKAnonymity","protectedAudience","clipboard","credentials","keyboard","managed","mediaDevices","serviceWorker","virtualKeyboard","wakeLock","deviceMemory","userAgentData","locks","storage","gpu","login","ink","mediaCapabilities","devicePosture","hid","mediaSession","permissions","presentation","serial","usb","xr","storageBuckets","adAuctionComponents","runAdAuction","canLoadAdAuctionFencedFrame","clearAppBadge","getBattery","getUserMedia","requestMIDIAccess","requestMediaKeySystemAccess","setAppBadge","webkitGetUserMedia","clearOriginJoinedAdInterestGroups","createAuctionNonce","joinAdInterestGroup","leaveAdInterestGroup","updateAdInterestGroups","deprecatedReplaceInURN","deprecatedURNToURL","getInstalledRelatedApps","getInterestGroupAdAuctionData","registerProtocolHandler","unregisterProtocolHandler"];
const navigatorMethods = new Set(["getGamepads","javaEnabled","sendBeacon","vibrate","adAuctionComponents","runAdAuction","canLoadAdAuctionFencedFrame","clearAppBadge","getBattery","getUserMedia","requestMIDIAccess","requestMediaKeySystemAccess","setAppBadge","webkitGetUserMedia","clearOriginJoinedAdInterestGroups","createAuctionNonce","joinAdInterestGroup","leaveAdInterestGroup","updateAdInterestGroups","deprecatedReplaceInURN","deprecatedURNToURL","getInstalledRelatedApps","getInterestGroupAdAuctionData","registerProtocolHandler","unregisterProtocolHandler"]);
function nativeNavigatorGetter(name, read) {
  const fn = function navigatorGetter() { return read(); };
  nativeFunctionText.set(fn, `function get ${name}() { [native code] }`);
  return fn;
}
function nativeNavigatorMethod(name, impl) {
  const fn = function navigatorMethod(...args) { return impl(...args); };
  nativeFunctionText.set(fn, `function ${name}() { [native code] }`);
  return fn;
}
const navigatorReads = {
  vendorSub: () => pageNavigator.vendorSub,
  productSub: () => pageNavigator.productSub,
  vendor: () => pageNavigator.vendor,
  maxTouchPoints: () => 0,
  hardwareConcurrency: () => pageNavigator.hardwareConcurrency,
  cookieEnabled: () => true,
  appCodeName: () => pageNavigator.appCodeName,
  appName: () => pageNavigator.appName,
  appVersion: () => pageNavigator.appVersion,
  platform: () => pageNavigator.platform,
  product: () => pageNavigator.product,
  userAgent: () => pageNavigator.userAgent,
  language: () => pageNavigator.language,
  languages: () => pageNavigator.languages,
  onLine: () => true,
  webdriver: () => false,
  plugins: () => plugins,
  mimeTypes: () => mimeTypes,
  pdfViewerEnabled: () => true,
  doNotTrack: () => null,
  deviceMemory: () => pageNavigator.deviceMemory,
  userAgentData: () => pageNavigator.userAgentData,
  gpu: () => ({
    requestAdapter() { return Promise.resolve(null); },
    wgslLanguageFeatures: [
      "packed_4x8_integer_dot_product",
      "unrestricted_pointer_parameters",
      "subgroup_uniformity",
      "subgroup_id",
      "pointer_composite_access",
      "readonly_and_readwrite_storage_textures",
      "uniform_buffer_standard_layout",
    ],
  }),
};
const navigatorPrototype = globalThis.Navigator.prototype;
for (const key of Object.getOwnPropertyNames(navigatorPrototype)) delete navigatorPrototype[key];
nativeFunctionText.set(Navigator, "function Navigator() { [native code] }");
for (const name of navigatorOrder) {
  if (name === "constructor") {
    Object.defineProperty(navigatorPrototype, name, { value: Navigator, writable: true, configurable: true });
    continue;
  }
  if (navigatorMethods.has(name)) {
    const impl = name === "getGamepads" ? () => [] : name === "javaEnabled" ? () => false : name === "sendBeacon" ? () => true : name === "vibrate" ? () => true : () => undefined;
    Object.defineProperty(navigatorPrototype, name, { value: nativeNavigatorMethod(name, impl), writable: true, configurable: true });
    continue;
  }
  const read = navigatorReads[name] || (() => undefined);
  Object.defineProperty(navigatorPrototype, name, { get: nativeNavigatorGetter(name, read), configurable: true });
}

class KeyEvent {
  constructor(type, init = {}) {
    this.type = type;
    this.timeStamp = init.timeStamp ?? performance.now();
    this.target = init.target || null;
    this.currentTarget = globalThis.document;
    this.srcElement = this.target;
    this.clientX = init.clientX ?? 0;
    this.clientY = init.clientY ?? 0;
    this.pageX = this.clientX;
    this.pageY = this.clientY;
    this.button = init.button ?? 0;
    this.buttons = init.buttons ?? 0;
    this.key = init.key || "";
    this.code = init.code || "";
    this.keyCode = init.keyCode || 0;
    this.charCode = init.charCode || 0;
    this.which = this.keyCode;
    this.altKey = false;
    this.ctrlKey = false;
    this.metaKey = false;
    this.shiftKey = false;
    this.isTrusted = true;
    this.bubbles = true;
    this.cancelable = true;
    this.view = globalThis;
    this.detail = init.detail ?? 0;
    this.inputType = init.inputType || "";
    this.data = init.data ?? null;
    this.isPrimary = true;
    this.pointerId = 1;
    this.pointerType = "mouse";
  }
  preventDefault() {}
  stopPropagation() {}
  stopImmediatePropagation() {}
  getCoalescedEvents() {
    return [this];
  }
  composedPath() {
    return [this.target, globalThis.document, globalThis];
  }
}
globalThis.KeyboardEvent = KeyEvent;
globalThis.MouseEvent = KeyEvent;
globalThis.PointerEvent = KeyEvent;
let html = "";
let pageStatus = 0;
try {
  const page = await fetch("https://accounts.x.ai/sign-up?redirect=grok-com&return_to=%2F", {
    headers: {
      "user-agent":
        castleUa,
      referer: "https://grok.com/",
    },
  });
  pageStatus = page.status;
  if (page.ok) html = await page.text();
} catch {
  pageStatus = 0;
}
if (!html) {
  try {
    html = fs.readFileSync(process.env.CASTLE_PAGE_FILE || "/tmp/castle-page.html", "utf8");
  } catch {
    html = "";
  }
}
const HTML_VOID = new Set(["area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"]);
function scriptsFromHtml(source) {
  const found = [];
  const pattern = /<script\b([^>]*)>([\s\S]*?)<\/script>/gi;
  let match = pattern.exec(source);
  while (match) {
    const attrs = match[1] || "";
    const srcMatch = attrs.match(/\bsrc\s*=\s*["']([^"']*)["']/i);
    const names = attrs.replace(/"[^"]*"|'[^']*'/g, " ").match(/[^\s"'=\/<>]+(?=\s*=)/g) || [];
    found.push({
      src: srcMatch ? srcMatch[1] : "",
      text: srcMatch ? "" : match[2] || "",
      attr_count: names.length,
    });
    match = pattern.exec(source);
  }
  const gtm = source.match(/GTM-[A-Z0-9]+/);
  if (gtm) {
    found.unshift({
      src: `https://www.googletagmanager.com/gtm.js?id=${gtm[0]}`,
      text: "",
      attr_count: 2,
    });
  }
  return found;
}
function elementsFromHtml(source) {
  const flat = [];
  const stack = [];
  const pattern = /<!--[\s\S]*?-->|<\/([a-zA-Z0-9:-]+)>|<([a-zA-Z0-9:-]+)(\s[^<>]*?)?(\/?)>/g;
  let foreign = 0;
  let match = pattern.exec(source);
  while (match) {
    if (match[0].startsWith("<!--")) {
      match = pattern.exec(source);
      continue;
    }
    if (match[1]) {
      const closed = match[1].toLowerCase();
      if (closed === "svg" || closed === "math") foreign = Math.max(0, foreign - 1);
      while (stack.length && stack[stack.length - 1].raw !== closed) stack.pop();
      stack.pop();
      match = pattern.exec(source);
      continue;
    }
    const raw = match[2].toLowerCase();
    const inForeign = foreign > 0 || raw === "svg" || raw === "math";
    const node = {
      tagName: inForeign ? raw : raw.toUpperCase(),
      childElementCount: 0,
      raw,
    };
    const parent = stack[stack.length - 1];
    if (parent) parent.childElementCount += 1;
    flat.push(node);
    const selfClosing = Boolean(match[4]) || HTML_VOID.has(raw);
    if (!selfClosing) {
      stack.push(node);
      if (raw === "svg" || raw === "math") foreign += 1;
    }
    match = pattern.exec(source);
  }
  return flat;
}
function installPageSnapshot(source) {
  pageScripts.length = 0;
  pageElements.length = 0;
  const scriptRows = styleProfile.scripts.length ? styleProfile.scripts : scriptsFromHtml(source);
  for (const row of scriptRows) {
    let src = "";
    if (row.src) {
      try {
        src = new URL(String(row.src), "https://accounts.x.ai").href;
      } catch {
        src = String(row.src);
      }
    }
    pageScripts.push({
      src,
      textContent: src ? "" : String(row.text || row.textContent || ""),
      attributes: { length: Number(row.attr_count ?? row.attributes?.length) || (src ? 1 : 0) },
    });
  }
  const rows = styleProfile.elements.length
    ? styleProfile.elements.map((row) => ({
        tagName: String(row.tag || row.tagName || "DIV"),
        childElementCount: Number(row.children ?? row.childElementCount) || 0,
      }))
    : elementsFromHtml(source);
  for (const row of rows) pageElements.push(row);
  const head = pageElements.find((el) => el.tagName === "HEAD");
  if (head && pageElements.length === 164 && head.childElementCount === 84) {
    // The captured signup document had these counts when Castle read it.
    head.childElementCount = 97;
    pageElements.length = 151;
  }
  for (const name of styleProfile.marks || []) {
    try {
      performance.mark(String(name));
    } catch {
      /* the page had no such mark */
    }
  }
  console.log("dom_snapshot", "scripts", pageScripts.length, "elements", pageElements.length, "sheets", styleSheets.length);
}
const dplMatch = html.match(/dpl=([A-Za-z0-9]{40})/);
const scriptUrl = dplMatch
  ? `https://accounts.x.ai/_next/static/chunks/02bh2mv7efkj1.js?dpl=${dplMatch[1]}`
  : "";
installPageSnapshot(html);
console.log("script_url_len", scriptUrl.length, "style_names", styleNames.length, "style_sheets", styleSheets.length);
if (globalThis.document && scriptUrl) {
  globalThis.document.currentScript = {
    src: scriptUrl,
    async: true,
    defer: false,
    nodeName: "SCRIPT",
    tagName: "SCRIPT",
    getAttribute(name) {
      return name === "src" ? scriptUrl : null;
    },
  };
}
const chunks = [];
const queue = [];
queue.push = (item) => {
  chunks.push(item);
  return queue.length;
};
globalThis.TURBOPACK = queue;
const patched = sdk
  .replace("mJ=1500", "mJ=20000")
  .replace("mN=3e3", "mN=20000")
  .replace("new Blob([u])", "new Blob([(globalThis.__plainBytes=u,u)])");
eval(patched);
const factory = chunks[0] && chunks[0][2];
const exported = {};
factory({ i: () => ({}), e() {}, g: globalThis }, {}, exported);
console.log(
  "listeners",
  ["pointerdown", "keydown", "click", "mousedown"].map((type) => `${type}=${(documentEvents.counts.get(type) || []).length}`).join(" "),
);
const emailInput = globalThis.document.createElement("input");
emailInput.id = "email";
emailInput.type = "email";
const buttonToggle = globalThis.document.createElement("button");
buttonToggle.type = "button";
const buttonSubmit = globalThis.document.createElement("button");
buttonSubmit.type = "submit";
const linkNode = globalThis.document.createElement("a");
const divNode = globalThis.document.createElement("div");
function fire(type, init) {
  const event = new KeyEvent(type, {
    ...init,
    timeStamp: init.timeStamp ?? performance.now(),
    target: init.target || emailInput,
  });
  documentEvents.dispatchEvent(event);
  windowEvents.dispatchEvent(event);
}
const gestureStarted = Date.now();
// Sample slot 98 is twelve records: button click, email focus, a 2s gap with the
// tenth mousemove, a div click, two 1s gaps around a link focus, then email
// focus, click, one key, and the submit click.
fire("click", { target: buttonToggle, timeStamp: 0 });
fire("focusin", { target: emailInput, timeStamp: 0 });
for (let step = 0; step < 10; step += 1) {
  fire("mousemove", { target: divNode, timeStamp: step === 9 ? 2000 : 1000 });
}
fire("click", { target: divNode, timeStamp: 2000 });
fire("focusin", { target: linkNode, timeStamp: 3100 });
fire("focusin", { target: emailInput, timeStamp: 4200 });
fire("click", { target: emailInput, timeStamp: 4200 });
// code Delete selects that key class; key A sets the extra uppercase bit.
fire("keydown", { target: emailInput, timeStamp: 4200, key: "A", code: "Delete", keyCode: 46 });
fire("click", { target: buttonSubmit, timeStamp: 4200 });
console.log("gestures_ms", Date.now() - gestureStarted);
const pkMatch = html.match(/pk_[A-Za-z0-9]{8,}/);
const pk = pkMatch ? pkMatch[0] : "";
console.log("pk", pk ? "yes" : "no", "page", pageStatus || (html ? "file" : 0));
if (typeof exported.configure === "function" && pk) {
  let configured = false;
  for (const arg of [pk, { pk }]) {
    try {
      exported.configure(arg);
      configured = true;
      console.log("configure", typeof arg === "string" ? "string" : "object");
      break;
    } catch (err) {
      console.log("configure_error", err && err.message);
    }
  }
  if (!configured) console.log("configure", "failed");
}
function summarizeSlots(slots) {
  if (!Array.isArray(slots)) {
    console.log("slots", "missing");
    return;
  }
  const counts = {};
  let preset165 = 0;
  let preset161 = 0;
  for (const value of slots) {
    const kind = value === null ? "null" : Array.isArray(value) ? "array" : typeof value;
    counts[kind] = (counts[kind] || 0) + 1;
    if (value === 165) preset165 += 1;
    if (value === 161) preset161 += 1;
  }
  console.log("slots", slots.length, JSON.stringify(counts), "p165", preset165, "p161", preset161);
  const placeholder = "13cf1397159f159f13b715bf1567155f159f1537";
  for (const index of [3, 63, 98, 116, 215, 242, 324, 645]) {
    const value = slots[index];
    const kind = value === null ? "null" : Array.isArray(value) ? "array" : typeof value;
    let extra = "";
    if (typeof value === "string" || value instanceof Uint8Array) {
      const text = typeof value === "string" ? value : Buffer.from(value).toString("latin1");
      extra = ` len=${text.length}`;
      if (text.includes(placeholder)) extra += " placeholder";
      if (text.length <= 48 && /^[\x20-\x7e]*$/.test(text)) extra += ` ascii=${text}`;
      else if (text.length <= 48) {
        const codes = [...text].map((ch) => ch.charCodeAt(0));
        extra += codes.every((code) => code < 128)
          ? ` units=${codes.map((code) => String.fromCharCode(code)).join("")}`
          : ` codes=${codes.slice(0, 12).join(",")}`;
      }
      if (index === 98 && text.length >= 4) {
        extra += ` c0=${text.charCodeAt(0)} c1=${text.charCodeAt(1)} c2=${text.charCodeAt(2)} c3=${text.charCodeAt(3)}`;
      }
    } else if (typeof value === "number" || typeof value === "boolean") {
      extra = ` v=${value}`;
    } else if (value && typeof value === "object") {
      extra = ` keys=${Object.keys(value).slice(0, 6).join(",")}`;
    }
    console.log("slot", index, kind + extra);
  }
}

const mint = exported.createRequestToken;
if (typeof mint !== "function") {
  console.log("no_export");
  process.exit(2);
}
try {
  const token = await Promise.race([
    Promise.resolve().then(() => mint()),
    new Promise((_, reject) => setTimeout(() => reject(new Error("mint_timeout")), 20000)),
  ]);
  console.log("token_len", token ? String(token).length : 0);
  try {
  const rawPlain = globalThis.__plainBytes;
  const plainBytes =
    rawPlain instanceof Uint8Array
      ? rawPlain
      : Array.isArray(rawPlain)
        ? Uint8Array.from(rawPlain)
        : new Uint8Array();
  console.log("plain_len", plainBytes.length, "plain_head", [...plainBytes.slice(0, 8)].join(","));
  let cursor = 0;
  const readVarint = () => {
    let value = 0;
    let shift = 0;
    while (cursor < plainBytes.length && shift <= 28) {
      const byte = plainBytes[cursor++];
      value |= (byte & 0x7f) << shift;
      if ((byte & 0x80) === 0) return value;
      shift += 7;
    }
    return null;
  };
  const count = readVarint();
  const wanted = new Set([3, 63, 98, 116, 215, 242, 324, 645]);
  const tagCounts = {};
  let badTag = -1;
  let decoded = 0;
  while (decoded < (count || 0) && cursor < plainBytes.length) {
    const tag = plainBytes[cursor++];
    let size = 0;
    if (tag === 1 || tag === 2 || tag === 8 || tag === 9) {
      const length = readVarint();
      size = length || 0;
      if (tag === 1 || tag === 8) size *= 2;
      cursor += size;
    } else if (tag === 3) {
      size = 4;
      cursor += 4;
    } else if (tag === 4) {
      size = 8;
      cursor += 8;
    } else if (tag === 7) {
      const start = cursor;
      const nested = readVarint() || 0;
      for (let child = 0; child < nested && cursor < plainBytes.length; child += 1) {
        const childTag = plainBytes[cursor++];
        if (childTag === 1 || childTag === 2 || childTag === 8 || childTag === 9) {
          let childLength = readVarint() || 0;
          if (childTag === 1 || childTag === 8) childLength *= 2;
          cursor += childLength;
        } else if (childTag === 3) cursor += 4;
        else if (childTag === 4) cursor += 8;
        else if (childTag === 10) readVarint();
      }
      size = cursor - start;
    } else if (tag === 10) {
      const start = cursor;
      readVarint();
      size = cursor - start;
    }
    tagCounts[tag] = (tagCounts[tag] || 0) + 1;
    if (badTag < 0 && (tag > 10 || tag === undefined)) badTag = decoded;
    if (wanted.has(decoded)) console.log("plain_slot", decoded, "tag", tag, "size", size);
    decoded += 1;
  }
  console.log("plain_decoded", decoded, "left", plainBytes.length - cursor, "bad", badTag);
  } catch (err) {
    console.log("plain_error", String((err && err.message) || err).slice(0, 120));
  }
  summarizeSlots(globalThis.__castleSlots);
  const events = globalThis.__castleEvents || [];
  console.log(
    "event_ids",
    events
      .map((event) => {
        const id = event && event.eventId;
        const gap = event && event.flags ? "g" : "";
        return `${typeof id === "number" ? id : "?"}${gap}`;
      })
      .join(","),
  );
  const tokenFile = process.env.CASTLE_TOKEN_FILE || "";
  if (tokenFile && token) {
    fs.writeFileSync(tokenFile, String(token), { mode: 0o600 });
    console.log("token_file", "written");
  }
  stopChromeDeflate();
} catch (err) {
  console.log("mint_error", err && err.message);
  stopChromeDeflate();
  process.exit(3);
}
