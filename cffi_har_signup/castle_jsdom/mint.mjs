#!/usr/bin/env node
import { JSDOM, VirtualConsole } from "jsdom";
import castlePkg from "@castleio/castle-js";
const Castle = castlePkg.default || castlePkg;
const pk = process.argv[2] || process.env.CASTLE_PK || "";
const ua = process.argv[3] || "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:152.0) Gecko/20100101 Firefox/152.0";
if (!pk.startsWith("pk_")) {
  console.error("usage: mint.mjs <castle_pk> [user_agent]");
  process.exit(2);
}
const virtualConsole = new VirtualConsole();
virtualConsole.on("jsdomError", () => {});
const dom = new JSDOM("<!doctype html><html><body></body></html>", {
  url: "https://accounts.x.ai/sign-up?redirect=grok-com",
  pretendToBeVisual: true,
  userAgent: ua,
  virtualConsole,
});
const { window } = dom;
const proto = window.HTMLCanvasElement && window.HTMLCanvasElement.prototype;
if (proto && !proto.__cffiPatched) {
  proto.__cffiPatched = true;
  proto.getContext = function getContext() {
    const canvas = this;
    return {
      canvas,
      fillStyle: "#000",
      font: "14px Arial",
      textBaseline: "top",
      fillRect() {},
      fillText() {},
      beginPath() {},
      closePath() {},
      rect() {},
      arc() {},
      stroke() {},
      fill() {},
      moveTo() {},
      lineTo() {},
      measureText(text) {
        return { width: String(text || "").length * 7 };
      },
      getImageData() {
        const data = new Uint8ClampedArray(16);
        for (let i = 0; i < data.length; i += 1) data[i] = (i * 17) % 256;
        return { data, width: 2, height: 2 };
      },
      createImageData(w, h) {
        return { data: new Uint8ClampedArray((w || 1) * (h || 1) * 4), width: w, height: h };
      },
    };
  };
  proto.toDataURL = function toDataURL() {
    return "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==";
  };
}
Object.defineProperty(window.navigator, "userAgent", { get: () => ua });
Object.defineProperty(window.navigator, "language", { get: () => "en-US" });
Object.defineProperty(window.navigator, "languages", { get: () => ["en-US", "en"] });
Object.defineProperty(window.screen, "width", { get: () => 1920 });
Object.defineProperty(window.screen, "height", { get: () => 1080 });
Castle.configure({ pk, window, verbose: false, timeout: 2500 });
const token = await Castle.createRequestToken();
if (!token) {
  console.error("empty castle token");
  process.exit(1);
}
process.stdout.write(String(token));
