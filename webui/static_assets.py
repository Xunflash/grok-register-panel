"""Content-addressed assets generated once from the legacy page shell."""
import hashlib
import re


def compile_assets(html, fonts):
    assets = {}

    def asset(content, suffix, content_type):
        path = "/assets/" + hashlib.sha256(content).hexdigest()[:20] + "." + suffix
        assets[path] = (content, content_type)
        return path

    for path, source in fonts.items():
        if source.is_file():
            html = html.replace(path, asset(source.read_bytes(), "woff2", "font/woff2"))
    html = re.sub(r"<style>(.*?)</style>", lambda match: '<link rel="stylesheet" href="' + asset(match[1].encode(), "css", "text/css; charset=utf-8") + '">', html, flags=re.S)
    html = re.sub(r"<script>(.*?)</script>", lambda match: '<script src="' + asset(match[1].encode(), "js", "text/javascript; charset=utf-8") + '"></script>', html, flags=re.S)
    return html, assets
