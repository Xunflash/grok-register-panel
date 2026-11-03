#!/usr/bin/env python3
"""Compress stdin frames with Chromium's CompressionStream("deflate-raw").

Request: uint32 big-endian length, then that many bytes.
Response: uint32 big-endian length, then the compressed bytes.
"""
from __future__ import annotations

import base64
import os
import struct
import sys

from patchright.sync_api import sync_playwright


def main() -> None:
    chrome = os.environ.get("PATCHRIGHT_CHROME") or "/usr/bin/chromium"
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=chrome,
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        page = browser.new_page()
        page.goto("about:blank")
        while True:
            header = stdin.read(4)
            if len(header) < 4:
                break
            (size,) = struct.unpack(">I", header)
            payload = stdin.read(size)
            if len(payload) != size:
                break
            encoded = base64.b64encode(payload).decode("ascii")
            compressed_b64 = page.evaluate(
                """async (encoded) => {
                  const raw = Uint8Array.from(atob(encoded), (ch) => ch.charCodeAt(0));
                  const stream = new Blob([raw]).stream().pipeThrough(new CompressionStream("deflate-raw"));
                  const bytes = new Uint8Array(await new Response(stream).arrayBuffer());
                  let text = "";
                  for (let i = 0; i < bytes.length; i += 1) text += String.fromCharCode(bytes[i]);
                  return btoa(text);
                }""",
                encoded,
            )
            compressed = base64.b64decode(compressed_b64)
            stdout.write(struct.pack(">I", len(compressed)))
            stdout.write(compressed)
            stdout.flush()
        browser.close()


if __name__ == "__main__":
    main()
