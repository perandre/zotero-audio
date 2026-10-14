"""Remote per-segment speech synthesis over a persistent SSH (or any) pipe.

Only synthesis moves. Planning, the segment cache, assembly, QA and publication
stay on the local worker. The wire format is newline-delimited JSON on the
server's stdin/stdout (logs go to stderr):

    -> {"id": 1, "op": "hello"}
    <- {"id": 1, "ok": true, "result": {"protocol": 1, "config": {...}}}
    -> {"id": 2, "op": "configure", "voice": "af_heart", "speed": 1.0, "language": "a"}
    <- {"id": 2, "ok": true, "result": {"config": {...}}}
    -> {"id": 3, "op": "synthesize", "text": "..."}
    <- {"id": 3, "ok": true, "result": {"wav_b64": "...", "meta": {...}}}

The client's ``config`` is the remote engine's real config, so segment cache
keys equal what the same engine would produce locally. Nothing falls back to
another voice or runtime.
"""
from __future__ import annotations

import argparse
import base64
import importlib
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable

PROTOCOL = 1
DEFAULT_REMOTE_COMMAND = "~/1mp/venv/bin/python -m zotero_audio.remote_speech serve"
REMOTE_PROVIDER = "kokoro-remote"


# ----------------------------------------------------------------- server side

def _reply(out, message: dict[str, Any]) -> None:
    out.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n")
    out.flush()


def serve(backend_factory: Callable[[], Any], stdin, stdout) -> int:
    """Serve requests until stdin closes. The model is loaded once, lazily."""
    backend = None
    for line in stdin:
        if not line.strip():
            continue
        request_id = None
        try:
            request = json.loads(line)
            request_id = request.get("id")
            op = request.get("op")
            if backend is None:
                print("remote speech: loading model", file=sys.stderr, flush=True)
                backend = backend_factory()
            if op == "hello":
                result = {"protocol": PROTOCOL, "config": backend.config}
            elif op == "configure":
                backend.configure(voice=request["voice"], speed=float(request["speed"]), language=request["language"])
                result = {"config": backend.config}
            elif op == "synthesize":
                with tempfile.TemporaryDirectory(prefix="remote-speech-") as directory:
                    path = Path(directory) / "segment.wav"
                    meta = backend.synthesize(request["text"], path)
                    result = {"wav_b64": base64.b64encode(path.read_bytes()).decode("ascii"), "meta": meta}
            else:
                raise ValueError(f"Unknown operation: {op!r}")
            _reply(stdout, {"id": request_id, "ok": True, "result": result})
        except Exception as exc:  # reported to the client; the process stays alive
            print(f"remote speech: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            _reply(stdout, {"id": request_id, "ok": False, "error": f"{type(exc).__name__}: {exc}"})
    return 0


def _load_factory(spec: str | None) -> Callable[[], Any]:
    if not spec:
        def torch_backend():
            from .audio import TorchKokoroBackend
            return TorchKokoroBackend()
        return torch_backend
    module, _, attribute = spec.partition(":")
    return getattr(importlib.import_module(module), attribute)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m zotero_audio.remote_speech")
    commands = parser.add_subparsers(dest="command", required=True)
    server = commands.add_parser("serve", help="Serve speech synthesis on stdin/stdout")
    server.add_argument("--backend", help="module:factory returning a SpeechBackend (tests/extensions)")
    args = parser.parse_args(argv)
    # Keep stdout exclusively for protocol messages: library prints and C-level
    # writes to fd 1 are redirected to stderr.
    protocol_out = os.fdopen(os.dup(1), "w", encoding="utf-8")
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    return serve(_load_factory(args.backend), sys.stdin, protocol_out)


# ----------------------------------------------------------------- client side

class RemoteSpeechError(RuntimeError):
    """The remote engine failed, was unreachable or returned invalid audio."""


class _Transport(Exception):
    pass


def ssh_command(host: str, command: str) -> list[str]:
    if not host or host.startswith("-") or any(c.isspace() for c in host):
        raise RemoteSpeechError(f"Invalid remote speech host: {host!r}")
    return ["ssh", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4",
            "-o", "ConnectTimeout=15", host, command]


def remote_speech_config(runtime: Path | None = None, environ: dict[str, str] | None = None) -> dict[str, Any]:
    """Local-only settings: control/speech.json overlaid by environment variables.

    Kept out of the synchronized app settings, whose cloud schema is strict.
    """
    environ = os.environ if environ is None else environ
    if runtime is None:
        from .app_state import runtime_root
        runtime = runtime_root()
    config: dict[str, Any] = {}
    path = Path(runtime) / "control" / "speech.json"
    if path.is_file():
        try:
            config = json.loads(path.read_text())
        except ValueError as exc:
            raise RemoteSpeechError(f"{path} is not valid JSON") from exc
        if not isinstance(config, dict):
            raise RemoteSpeechError(f"{path} must contain an object")
    for key, variable in (("provider", "ZOTERO_AUDIO_SPEECH_PROVIDER"), ("host", "ZOTERO_AUDIO_REMOTE_SPEECH_HOST"),
                          ("command", "ZOTERO_AUDIO_REMOTE_SPEECH_COMMAND")):
        if environ.get(variable):
            config[key] = environ[variable]
    return config


def configured_provider(runtime: Path | None = None) -> str | None:
    return remote_speech_config(runtime).get("provider")


class RemoteSpeechBackend:
    """SpeechBackend whose synthesis runs in a persistent remote process."""

    def __init__(self, argv: list[str], *, connect_timeout: float = 300.0, request_timeout: float = 900.0,
                 expected_engine: str | None = None) -> None:
        self.argv = list(argv)
        self.connect_timeout = connect_timeout
        self.request_timeout = request_timeout
        self.expected_engine = expected_engine
        self.process: subprocess.Popen | None = None
        self.lines: queue.Queue = queue.Queue()
        self.next_id = 0
        self.lock = threading.Lock()
        self.stderr_tail: list[str] = []
        self.settings: dict[str, Any] | None = None
        self.config: dict[str, Any] = {}
        failure = None
        for _ in range(2):  # one automatic reconnect, as for requests
            try:
                self._connect()
                self.config = self._hello()
                return
            except _Transport as exc:
                failure = f"{exc}; remote stderr: {' | '.join(self.stderr_tail) or 'none'}"
            except BaseException:
                self.close()
                raise
        self.close()
        raise RemoteSpeechError(f"Remote speech is unreachable: {failure}")

    @classmethod
    def from_settings(cls, runtime: Path | None = None, **options) -> "RemoteSpeechBackend":
        config = remote_speech_config(runtime)
        host = config.get("host")
        if not host:
            raise RemoteSpeechError(
                "Remote speech host is not set. Put {\"provider\": \"kokoro-remote\", \"host\": \"user@machine\"} in "
                "<runtime>/control/speech.json or set ZOTERO_AUDIO_REMOTE_SPEECH_HOST.")
        return cls(ssh_command(host, config.get("command") or DEFAULT_REMOTE_COMMAND), **options)

    # -- process management
    def _connect(self) -> None:
        self.close()
        try:
            process = subprocess.Popen(self.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       text=True, encoding="utf-8", bufsize=1)
        except OSError as exc:
            raise RemoteSpeechError(f"Could not start remote speech process: {exc}") from exc
        self.process = process
        self.lines = lines = queue.Queue()
        self.stderr_tail = []

        def pump(stream, sink):
            for line in stream:
                sink(line)
            lines.put(None)

        threading.Thread(target=pump, args=(process.stdout, lines.put), daemon=True).start()

        def keep(line):
            self.stderr_tail = (self.stderr_tail + [line.rstrip()])[-10:]

        def drain(stream):
            for line in stream:
                keep(line)

        threading.Thread(target=drain, args=(process.stderr,), daemon=True).start()

    def close(self) -> None:
        process, self.process = self.process, None
        if process is None:
            return
        try:
            process.stdin.close()
        except OSError:
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()

    def _call_once(self, message: dict[str, Any], timeout: float) -> dict[str, Any]:
        if self.process is None or self.process.poll() is not None:
            raise _Transport("remote process is not running")
        self.next_id += 1
        message = {**message, "id": self.next_id}
        try:
            self.process.stdin.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n")
            self.process.stdin.flush()
        except OSError as exc:
            raise _Transport(f"write failed: {exc}") from exc
        try:
            line = self.lines.get(timeout=timeout)
        except queue.Empty:
            raise _Transport(f"no reply within {timeout:g} s") from None
        if line is None:
            raise _Transport("remote process closed the connection")
        try:
            reply = json.loads(line)
        except ValueError as exc:
            raise _Transport("remote process sent a malformed reply") from exc
        if reply.get("id") != message["id"]:
            raise _Transport("remote process sent an out-of-sequence reply")
        if not reply.get("ok"):
            raise RemoteSpeechError(f"Remote speech engine error: {reply.get('error', 'unknown')}")
        return reply["result"]

    def _hello(self) -> dict[str, Any]:
        result = self._call_once({"op": "hello"}, self.connect_timeout)
        if result.get("protocol") != PROTOCOL or not isinstance(result.get("config"), dict):
            raise RemoteSpeechError("Remote speech protocol mismatch; update the remote install")
        config = result["config"]
        if self.expected_engine and config.get("engine") != self.expected_engine:
            raise RemoteSpeechError(f"Remote engine is {config.get('engine')!r}, expected {self.expected_engine!r}")
        return config

    def _reconnect(self) -> None:
        self._connect()
        config = self._hello()
        if self.settings is not None:
            config = self._call_once({"op": "configure", **self.settings}, self.connect_timeout)["config"]
        if self.config and config != self.config:
            raise RemoteSpeechError("Remote speech engine identity changed across reconnect; refusing to mix audio")
        self.config = config

    def _request(self, message: dict[str, Any], timeout: float) -> dict[str, Any]:
        """One call, with a single automatic reconnect on transport failure."""
        with self.lock:
            try:
                return self._call_once(message, timeout)
            except _Transport as first:
                reason = f"{first}; remote stderr: {' | '.join(self.stderr_tail) or 'none'}"
                try:
                    self._reconnect()
                    return self._call_once(message, timeout)
                except (_Transport, RemoteSpeechError) as second:
                    self.close()
                    raise RemoteSpeechError(
                        f"Remote speech failed ({reason}); reconnect also failed: {second}") from second

    # -- SpeechBackend protocol
    def configure(self, *, voice: str, speed: float, language: str) -> None:
        settings = {"voice": voice, "speed": float(speed), "language": language}
        self.config = self._request({"op": "configure", **settings}, self.connect_timeout)["config"]
        self.settings = settings

    def synthesize(self, text: str, destination: Path) -> dict[str, Any]:
        from .audio import validate_wav
        result = self._request({"op": "synthesize", "text": text}, self.request_timeout)
        try:
            data = base64.b64decode(result["wav_b64"], validate=True)
            meta = result["meta"]
        except (KeyError, ValueError) as exc:
            raise RemoteSpeechError("Remote speech returned an invalid synthesis reply") from exc
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
        temporary_path = Path(temporary)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
            try:
                validate_wav(temporary_path)
            except Exception as exc:
                raise RemoteSpeechError(f"Remote speech returned an invalid WAV: {exc}") from exc
            os.replace(temporary_path, destination)
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise
        return meta

    def status(self) -> dict[str, Any]:
        """Reachability probe for `za doctor`."""
        return {"reachable": True, "config": self.config}


if __name__ == "__main__":
    raise SystemExit(main())
