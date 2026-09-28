"""Bounded, cancellable process transport for optional FST native reads.

The MCP process never imports pylibfst. A worker owns one native reader and all
of its decompression state. Closing a request reaps that worker before releasing
its admission slot; client cancellation never leaves an abandoned native scan.
"""
from __future__ import annotations

import importlib.util
import importlib.metadata
import json
import os
from pathlib import Path
import selectors
import select
import subprocess
import sys
import tempfile
import threading
import time

from .cancellation import check_cancelled
from .scope_metadata import ScopeIdentityChanged, file_identity

FST_VERSION = "0.2.1"
FST_BASIC_TOOLS = ("get_waveform_summary", "search_signals", "get_signal_at_time",
                   "get_signal_transitions", "get_signals_around_time")
FST_SUPPORTED_TOOLS = (*FST_BASIC_TOOLS, "get_signals_by_cycle", "period", "verify_window",
    "suggest_handshakes", "suggest_protocol_bundles", "sweep_handshakes",
    "inspect_handshake", "inspect_tlul", "reconstruct_transactions")
FRAME_BYTES = 1024 * 1024
MAX_WORKERS = 4
_slots = threading.BoundedSemaphore(MAX_WORKERS)


class FstError(RuntimeError):
    """An explicit format, dependency, resource, or native-reader boundary."""


def fst_runtime_info():
    try:
        installed = importlib.metadata.version("pylibfst")
    except importlib.metadata.PackageNotFoundError:
        installed = None
    present = importlib.util.find_spec("pylibfst") is not None
    status = ("platform_unsupported" if not sys.platform.startswith("linux") else
              "dependency_missing" if not present or installed is None else
              "version_unsupported" if installed != FST_VERSION else "candidate_ready_unloaded")
    return {"enabled": status == "candidate_ready_unloaded", "dependency": "pylibfst",
            "required_version": FST_VERSION, "installed_version": installed, "status": status,
            "message": ("FST candidate ready (native library not loaded); summary lists validated analyses"
                        if status == "candidate_ready_unloaded" else
                        "FST requires Linux and traceweave-mcp[fst] in the server interpreter (pylibfst 0.2.1)")}


class FstProcess:
    def __init__(self, path, *, timeout_sec=30.0, memory_bytes=512 * 1024 * 1024):
        if not sys.platform.startswith("linux"):
            raise FstError("fst_platform_unsupported: isolated FST reading currently requires Linux")
        if not 0 < timeout_sec <= 300 or not 64 * 1024 * 1024 <= memory_bytes <= 1024**3:
            raise ValueError("invalid FST worker limits")
        self.path = os.path.realpath(path)
        self.identity = file_identity(self.path)
        self.deadline = time.monotonic() + timeout_sec
        self.process = self.selector = self.directory = None
        self.admitted = False
        self.buffer = bytearray()
        self.metrics = {}
        try:
            while not _slots.acquire(timeout=0.05):
                self.check()
            self.admitted = True
            self.check()
            self.directory = tempfile.TemporaryDirectory(prefix="traceweave-fst-")
            env = dict(os.environ, TMPDIR=self.directory.name, PYTHONUNBUFFERED="1")
            self.process = subprocess.Popen(self.command(), stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, cwd=self.directory.name,
                env=env, bufsize=0)
            os.set_blocking(self.process.stdin.fileno(), False)
            self.selector = selectors.DefaultSelector()
            self.selector.register(self.process.stdout, selectors.EVENT_READ)
            self.send({"path": self.path, "identity": self.identity, "memory_bytes": memory_bytes,
                       "timeout_sec": timeout_sec})
            self.header = self.receive()
            if self.header.get("kind") != "ready":
                raise FstError("fst_worker_protocol_error")
        except BaseException:
            self.close()
            raise

    @staticmethod
    def command():
        return [sys.executable, str(Path(__file__).with_name("fst_worker.py"))]

    def check(self):
        check_cancelled()
        if time.monotonic() >= self.deadline:
            raise FstError("fst_timeout: narrow the requested window")
        try:
            current = file_identity(self.path)
        except OSError as exc:
            raise ScopeIdentityChanged("FST disappeared during reading") from exc
        if current != self.identity:
            raise ScopeIdentityChanged("FST changed during reading; obtain a new parser")

    def send(self, value):
        self.check()
        encoded = json.dumps(value, separators=(",", ":")).encode() + b"\n"
        if len(encoded) > 65536:
            raise FstError("fst_request_limit")
        try:
            pending = memoryview(encoded)
            fd = self.process.stdin.fileno()
            while pending:
                self.check()
                try:
                    written = os.write(fd, pending)
                except BlockingIOError:
                    select.select([], [fd], [], 0.05)
                    continue
                pending = pending[written:]
        except (BrokenPipeError, OSError) as exc:
            raise FstError("fst_worker_failed") from exc

    def receive(self):
        while True:
            self.check()
            newline = self.buffer.find(b"\n")
            if newline >= 0:
                raw = bytes(self.buffer[:newline])
                del self.buffer[:newline + 1]
                value = json.loads(raw)
                if value.get("kind") == "error":
                    reason = value.get("reason", "fst_worker_failed")
                    if reason == "file_not_found":
                        raise FileNotFoundError(self.path)
                    if reason == "permission_denied":
                        raise PermissionError(self.path)
                    if reason == "waveform_changed":
                        raise ScopeIdentityChanged("FST changed during reading")
                    raise FstError(reason)
                self.metrics.update(value.get("metrics", {}))
                self.check()
                return value
            if len(self.buffer) >= FRAME_BYTES:
                raise FstError("fst_worker_frame_limit")
            if not self.selector.select(timeout=0.05):
                continue
            data = os.read(self.process.stdout.fileno(), min(65536, FRAME_BYTES - len(self.buffer)))
            if not data:
                raise FstError("fst_worker_failed: native decode failed or exceeded its resource limit")
            self.buffer.extend(data)

    def ask(self, request):
        self.send(request)
        return self.receive()

    def close(self):
        started = time.monotonic()
        try:
            if self.process is not None:
                if self.process.poll() is None:
                    self.process.terminate()
                try:
                    self.process.wait(timeout=0.25)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=2)
                for pipe in (self.process.stdin, self.process.stdout):
                    pipe.close()
                self.process = None
        finally:
            if self.selector is not None:
                self.selector.close()
                self.selector = None
            if self.directory is not None:
                self.directory.cleanup()
                self.directory = None
            self.buffer.clear()
            if self.admitted:
                _slots.release()
                self.admitted = False
            self.metrics["cleanup_ms"] = (time.monotonic() - started) * 1000

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
