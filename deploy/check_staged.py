#!/usr/bin/env python3
"""Fail closed on staged runtime data and credential-shaped source values."""
import re
import subprocess
import sys


FORBIDDEN = re.compile(r"(^|/)(?:\.env(?:\..*)?|config\.(?:json|yaml)|accounts|cpa_auth|grok2api_auth|node_modules|\.venv|log|logs|.*\.(?:db|sqlite|sqlite3)(?:-.*)?|.*\.har|.*\.bak.*)(?:/|$)")
SECRET = re.compile(rb"(?:gh[pousr]_[A-Za-z0-9]{30,}|-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----|eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,})")
PROXY = re.compile(rb"(?:https?|socks[45]h?)://[^\s/:\"']+:[^\s/@\"']+@([^\s/:\"']+)")
EMAIL = re.compile(rb"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})")


def example_domain(raw):
    domain = raw.lower()
    return domain.endswith((b".test", b".example", b".invalid")) or domain in {b"example.com", b"example.org", b"example.net", b"localhost"}


def violations(name, content):
    result = []
    if FORBIDDEN.search(name) and not name.endswith((".env.example", "/.gitkeep")):
        result.append("runtime path")
    if len(content) > 2 * 1024 * 1024 or (b"\x00" in content and name.startswith("deploy/")):
        result.append("large or binary deployment artifact")
    if name.startswith("deploy/") and ("/backup" in name or name.endswith((".tar", ".tar.gz", ".zip", ".image.tar"))):
        result.append("historical deployment archive")
    if SECRET.search(content):
        result.append("credential-shaped value")
    if any(not example_domain(host) for host in PROXY.findall(content)):
        result.append("proxy authentication outside reserved fixture domain")
    fixture = "/tests/" in "/" + name or name.endswith("_test.go")
    if not fixture and any(not example_domain(domain) for domain in EMAIL.findall(content)):
        result.append("non-example identity; replace or explicitly review before staging")
    return result


def main():
    names = subprocess.check_output(["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"]).decode().split("\0")
    errors = []
    for name in filter(None, names):
        content = subprocess.check_output(["git", "show", ":" + name])
        errors.extend(name + ": " + issue for issue in violations(name, content))
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    return subprocess.run(["git", "diff", "--cached", "--check"]).returncode


if __name__ == "__main__":
    raise SystemExit(main())
