"""Local-only transport observation smoke test.

Uses one persistent downstream connection to exercise real upstream reuse and
retirement, plus a silent TLS peer. No credentials, installed ports, or public
upstreams are used. This tests observation, not unavailable transport controls.
"""
import concurrent.futures
import http.client
import json
import os
from pathlib import Path
import shutil
import socket
import socketserver
import sqlite3
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
SECRET = "MASKIT_SYNTHETIC_ENTITY"


class Upstream(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), Handler)
        self.accepts = 0
        self.requests = []
        self.guard = threading.Lock()

    def get_request(self):
        sock, addr = super().get_request()
        with self.guard:
            self.accepts += 1
        return sock, addr


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        with self.server.guard:
            self.server.requests.append((self.path, self.client_address, body))
        text = json.loads(body)["messages"][0]["content"]
        if self.path.endswith("/stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for piece in (text[: len(text) // 2], text[len(text) // 2 :]):
                data = ("data: " + json.dumps({"choices": [{"delta": {"content": piece}}]}) + "\n\n").encode()
                self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
                self.wfile.flush()
                time.sleep(0.1)
            end = b"data: [DONE]\n\n"
            self.wfile.write(f"{len(end):x}\r\n".encode() + end + b"\r\n0\r\n\r\n")
            self.wfile.flush()
            return
        data = json.dumps({"choices": [{"message": {"content": text}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        if self.path.endswith("/close"):
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)
        self.wfile.flush()


class SilentTLS(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(4)
        try:
            record = self.request.recv(16384)
            if record and record[0] == 22:
                self.server.hello.set()
                self.server.stop.wait(8)
        except OSError:
            pass


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def chat(conn, path="/v1/chat/completions"):
    body = ('{ "model": "local-test", "messages": [{"role": "user", '
            '"content": "' + SECRET + '"}], "stream": ' +
            ("true" if path.endswith("/stream") else "false") + ' }').encode()
    conn.request("POST", path, body, {"Content-Type": "application/json"})
    response = conn.getresponse()
    return response.status, response.read()


def events(data):
    db = data / "shield-events.sqlite3"
    if not db.exists():
        return []
    try:
        with sqlite3.connect(db.as_uri() + "?mode=ro", uri=True) as conn:
            return [json.loads(row[0]) for row in conn.execute("SELECT payload FROM events")]
    except sqlite3.OperationalError:
        return []


def main():
    upstream = Upstream()
    silent = socketserver.ThreadingTCPServer(("127.0.0.1", 0), SilentTLS)
    silent.daemon_threads = True
    silent.hello = threading.Event()
    silent.stop = threading.Event()
    for srv in (upstream, silent):
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    ports = [free_port(), free_port()]
    while ports[0] == ports[1]:
        ports[1] = free_port()
    process = None
    client = None
    try:
        with tempfile.TemporaryDirectory(prefix="maskit-transport-smoke-") as temp:
            data = Path(temp)
            config = {
                "capture_mode": "reverse", "http2": False, "ner_enabled": False,
                "sensitive": {"TEST": [SECRET]}, "filter_enabled": True,
                "stream_response": True, "response_scan": False, "debug": False,
                "audit": {"enabled": False},
                "upstreams": [
                    {"name": "local-http", "port": ports[0], "paths": ["/v1"],
                     "target": f"http://127.0.0.1:{upstream.server_port}", "use_proxy": False},
                    {"name": "silent-tls", "port": ports[1], "paths": ["/v1"],
                     "target": f"https://127.0.0.1:{silent.server_address[1]}", "use_proxy": False},
                ],
            }
            (data / "config.json").write_text(json.dumps(config), encoding="utf-8")
            env = {k: v for k, v in os.environ.items()
                   if not k.upper().endswith("_PROXY")
                   and not k.startswith(("LLM_SHIELD_", "MASKIT_"))}
            env.update(LLM_SHIELD_DATA_DIR=temp, PYTHONPATH=str(ROOT / "engine"),
                       PYTHONIOENCODING="utf-8")
            mitmdump = shutil.which("mitmdump")
            if not mitmdump:
                raise RuntimeError("mitmdump is required")
            args = [mitmdump, "-s", str(ROOT / "engine/transparent.py"),
                    "--set", f"confdir={temp}", "--set", "connection_strategy=lazy",
                    "--set", "http2=false", "--set", "flow_detail=0",
                    "--set", "termlog_verbosity=warn"]
            for port in ports:
                args += ["--mode", f"regular@127.0.0.1:{port}"]
            unsafe = subprocess.run(args + ["--set", "stream_large_bodies=1m"],
                                    cwd=ROOT / "engine", env=env, capture_output=True,
                                    text=True, timeout=8)
            assert unsafe.returncode != 0, "unsafe automatic request streaming was accepted"
            assert "Maskit requires stream_large_bodies" in unsafe.stdout + unsafe.stderr
            assert not upstream.requests, "unsafe startup sent an HTTP request"
            print("PASS: automatic request streaming rejected before serving traffic")
            log_path = data / "proxy.log"
            with log_path.open("w", encoding="utf-8") as log:
                process = subprocess.Popen(args, cwd=ROOT / "engine", env=env,
                                           stdout=log, stderr=subprocess.STDOUT)
                deadline = time.monotonic() + 20
                while True:
                    if process.poll() is not None:
                        raise RuntimeError(log_path.read_text()[-3000:])
                    try:
                        with socket.create_connection(("127.0.0.1", ports[0]), timeout=.2):
                            break
                    except OSError:
                        if time.monotonic() > deadline:
                            raise TimeoutError("isolated proxy startup")
                        time.sleep(.1)
                client = http.client.HTTPConnection("127.0.0.1", ports[0], timeout=5)
                for _ in range(2):
                    status, body = chat(client)
                    assert status == 200 and SECRET.encode() in body
                with upstream.guard:
                    assert upstream.accepts == 1, "test did not exercise an actual reused connection"
                    assert len(upstream.requests) == 2
                    assert upstream.requests[0][2] == upstream.requests[1][2], "stable masked bytes changed"
                    assert all(SECRET.encode() not in r[2] for r in upstream.requests)
                print("PASS: persistent client reused one upstream connection; masking and byte stability preserved")

                chat(client, "/v1/close")
                chat(client)
                with upstream.guard:
                    assert upstream.accepts == 2, "normal upstream close was not retired"
                    assert len(upstream.requests) == 4, "unexpected POST replay"
                print("PASS: ordinary close obtains a new upstream connection without replay")

                def tls_wait():
                    conn = http.client.HTTPConnection("127.0.0.1", ports[1], timeout=1.5)
                    try:
                        chat(conn)
                        raise AssertionError("silent TLS peer unexpectedly completed")
                    except TimeoutError:
                        return
                    finally:
                        conn.close()

                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    blocked = pool.submit(tls_wait)
                    assert silent.hello.wait(3), "TLS ClientHello never reached stub"
                    status, body = chat(client, "/v1/stream")
                    assert status == 200 and b"[DONE]" in body and SECRET.encode() in body
                    blocked.result(timeout=4)
                print("PASS: healthy SSE completes while another connection waits in TLS")
                until = time.monotonic() + 5
                rows = []
                while time.monotonic() < until:
                    rows = events(data)
                    if any(e.get("transport", {}).get("phase") == "tls_handshake"
                           for e in rows if e.get("type") in ("ERR", "CANCEL")):
                        break
                    time.sleep(.1)
                assert any(e.get("transport", {}).get("reused") is True for e in rows), "reuse evidence missing"
                assert any(e.get("transport", {}).get("phase") == "tls_handshake"
                           for e in rows if e.get("type") in ("ERR", "CANCEL")), "TLS failure phase missing"
                assert upstream.accepts == 2 and len(upstream.requests) == 5
                print("PASS: persisted evidence distinguishes actual reuse and TLS failure")
                client.close()
                client = None
                observed_at = time.time()
                until = time.monotonic() + 6
                idle = False
                while time.monotonic() < until:
                    try:
                        metrics = json.loads((data / "engine-runtime.json").read_text())
                        idle = (metrics.get("generated_at", 0) > observed_at
                                and metrics.get("transport", {}).get("inflight") == 0
                                and metrics.get("aux_pool", {}).get("reserved_jobs") == 0)
                        if idle:
                            break
                    except (OSError, ValueError):
                        pass
                    time.sleep(.1)
                assert idle, "idle heartbeat did not refresh or resources did not return"
                print("PASS: idle heartbeat refreshes and in-flight reservations return to zero")
                process.terminate()
                process.wait(timeout=5)
            print("TRANSPORT SMOKE OK")
    finally:
        if client is not None:
            client.close()
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        silent.stop.set()
        for srv in (upstream, silent):
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    main()
