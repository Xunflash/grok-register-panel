"""Incremental batch log projection; runtime files remain the source of truth."""
from collections import deque
from copy import deepcopy
import re
import threading

from webui.security_utils import mask_email, redact_log_line


class LogIndex:
    def __init__(self):
        self.lock = threading.Lock()
        self.identity = None
        self.offset = 0
        self.pending = b""
        self.value = {}
        self.tail = deque(maxlen=40)
        self.ok_rows = deque(maxlen=80)
        self.fail_rows = deque(maxlen=80)

    def read(self, path):
        if path is None or not path.is_file():
            return {"error": "no log"}
        with self.lock, path.open("rb") as source:
            import os
            stat = os.fstat(source.fileno())
            identity = (str(path), stat.st_dev, stat.st_ino)
            if identity != self.identity or stat.st_size < self.offset:
                self.identity, self.offset, self.pending = identity, 0, b""
                self.value = {name: 0 for name in ("ok", "fail", "domain", "skip", "bot0", "bot1", "bfs")}
                self.value.update(count_target=None, workers=None, ended=None, fail_kinds={}, worker_ok={}, worker_fail={})
                self.tail.clear()
                self.ok_rows.clear()
                self.fail_rows.clear()
            source.seek(self.offset)
            remaining = 4 * 1024 * 1024
            while remaining > 0 and (chunk := source.read(min(65536, remaining))):
                remaining -= len(chunk)
                self.offset += len(chunk)
                lines = (self.pending + chunk).split(b"\n")
                self.pending = lines.pop()
                if len(self.pending) > 1_048_576:
                    self.pending = b""
                for line in lines:
                    self.consume(line.decode("utf-8", errors="replace"))
            result = deepcopy(self.value)
            result.update(log=path.name, log_name=path.name, log_size=stat.st_size, mtime=stat.st_mtime,
                          caught_up=self.offset >= stat.st_size,
                          recent_ok=list(reversed(self.ok_rows)), recent_fail=list(reversed(self.fail_rows)), tail=list(self.tail))
            return result

    def consume(self, line):
        self.tail.append(redact_log_line(line))
        match = re.search(r"\[batch\] count=(\d+) workers=(\d+)|终端模式启动，目标数量:\s*(\d+)\s*\|\s*并发:\s*(\d+)", line)
        if match:
            count, workers = match.groups()[:2] if match.group(1) else match.groups()[2:]
            self.value.update(count_target=int(count), workers=int(workers))
        match = re.search(r"任务结束。成功\s*(\d+)\s*\|\s*失败\s*(\d+)", line)
        if match:
            self.value["ended"] = {"success": int(match.group(1)), "fail": int(match.group(2))}
        worker = re.search(r"\[W(\d+)\]", line)
        worker = "W" + worker.group(1) if worker else "?"
        if worker not in self.value["worker_ok"] and worker not in self.value["worker_fail"] and len(set(self.value["worker_ok"]) | set(self.value["worker_fail"])) >= 100:
            worker = "?"
        stamp = line[1:9] if line.startswith("[") else ""
        if "[+] 注册成功" in line:
            self.value["ok"] += 1
            self.value["worker_ok"][worker] = self.value["worker_ok"].get(worker, 0) + 1
            email = re.search(r"\[\+\] 注册成功(?:（[^）]*）)?:\s*(\S+)", line)
            self.ok_rows.append({"t": stamp, "w": worker, "email": mask_email(email.group(1) if email else "")})
        if "[-] 失败" in line:
            self.value["fail"] += 1
            self.value["worker_fail"][worker] = self.value["worker_fail"].get(worker, 0) + 1
            match = re.search(r"\[-\] 失败 \[([^\]]+)\]:\s*(.*)", line)
            kind, message = match.groups() if match else ("其它", line[-120:])
            if "inputs=none" in message:
                kind = "空页UI"
            if "Turnstile" in message:
                kind = "资料页Turnstile"
            if kind not in self.value["fail_kinds"] and len(self.value["fail_kinds"]) >= 50:
                kind = "其它"
            kind = redact_log_line(kind[:80])
            self.value["fail_kinds"][kind] = self.value["fail_kinds"].get(kind, 0) + 1
            self.fail_rows.append({"t": stamp, "w": worker, "kind": kind, "msg": redact_log_line(message[:160])})
        for name, marker in (("domain", "[-] 域名拒绝"), ("skip", "[-] 卡住跳过"), ("bot0", "botFlagSource=0"), ("bot1", "botFlagSource=1")):
            self.value[name] += int(marker in line)
        self.value["bfs"] += int(bool(re.search(r"JWT bfs 标记|bfs=yes|kind=bfs_flagged|has_bfs", line)))
