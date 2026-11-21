"""Deploy-gate fault-injection proxy for the worker crash-loop unit (§8.1).

A TLS-terminating proxy the worker talks to instead of prod. Mode is read from a
control file on EVERY request, so a fault can be applied to a worker that is
already running, with no restart — which is what keeps step 5's NRestarts
assertion meaningful.

Modes, and the exception each produces on the worker per the §8.1 table:

  forward   normal proxying to prod                       (no fault)
  hold      forward to prod so the claim COMMITS, then    httpx.ReadTimeout
            hold the connection open sending nothing
  close     forward to prod, then close with no response  httpx.RemoteProtocolError
  502       answer 502 without forwarding                 httpx.HTTPStatusError (transient)
  401       answer 401 without forwarding                 httpx.HTTPStatusError (permanent)
  blackhole accept TCP, never complete the TLS handshake  httpx.ConnectTimeout
  down      refuse connections entirely                   httpx.ConnectError
  badcert   serve a cert for the wrong name               ConnectError + SSLCertVerificationError

`hold` and `close` forward FIRST on purpose: rule 6's orphan demonstration needs
a claim genuinely committed server-side before the response is lost.
"""
import http.client
import json
import socket
import ssl
import sys
import threading
import time
from pathlib import Path

GATE = Path(__file__).resolve().parent
MODE_FILE = GATE / "mode"
LOG_FILE = GATE / "proxy.log"
UPSTREAM = "privatelifedashboard.com"
LISTEN = ("127.0.0.1", 8443)


def mode():
    try:
        return MODE_FILE.read_text().strip() or "forward"
    except FileNotFoundError:
        return "forward"


def log(**fields):
    line = json.dumps({"ts": round(time.time(), 3), **fields}, default=str)
    with LOG_FILE.open("a") as fh:
        fh.write(line + "\n")
    print(line, flush=True)


def read_request(rfile):
    """Return (method, path, headers, body) or None when the peer closed."""
    line = rfile.readline()
    if not line:
        return None
    try:
        method, path, _ = line.decode("latin-1").strip().split(" ", 2)
    except ValueError:
        return None
    headers = {}
    while True:
        h = rfile.readline()
        if h in (b"\r\n", b"\n", b""):
            break
        k, _, v = h.decode("latin-1").partition(":")
        headers[k.strip()] = v.strip()
    body = b""
    n = int(headers.get("Content-Length", 0) or 0)
    if n:
        body = rfile.read(n)
    return method, path, headers, body


def forward(method, path, headers, body):
    """Send the request to the real prod server. Returns (status, hdrs, body)."""
    ctx = ssl.create_default_context()
    conn = http.client.HTTPSConnection(UPSTREAM, 443, timeout=25, context=ctx)
    fwd = {k: v for k, v in headers.items() if k.lower() not in ("host", "connection")}
    conn.request(method, path, body=body or None, headers=fwd)
    resp = conn.getresponse()
    data = resp.read()
    out = (resp.status, dict(resp.getheaders()), data)
    conn.close()
    return out


def respond(wfile, status, body=b"", ctype="application/json"):
    reason = {200: "OK", 204: "No Content", 401: "Unauthorized",
              502: "Bad Gateway"}.get(status, "Status")
    head = f"HTTP/1.1 {status} {reason}\r\n"
    if status != 204:
        head += f"Content-Type: {ctype}\r\nContent-Length: {len(body)}\r\n"
    else:
        head += "Content-Length: 0\r\n"
    head += "Connection: keep-alive\r\n\r\n"
    wfile.write(head.encode("latin-1") + (body if status != 204 else b""))
    wfile.flush()


def handle(raw):
    m = mode()
    if m == "blackhole":
        # Accept the TCP connection and never complete the TLS handshake, so
        # httpcore's CONNECT timeout (which also covers start_tls) runs out.
        log(event="blackhole_hold", note="TLS handshake deliberately stalled")
        time.sleep(120)
        raw.close()
        return

    cert = "srv-badname.crt" if m == "badcert" else "srv.crt"
    key = "srv-badname.key" if m == "badcert" else "srv.key"
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(GATE / cert, GATE / key)
    try:
        sock = ctx.wrap_socket(raw, server_side=True)
    except (ssl.SSLError, OSError) as exc:
        log(event="tls_handshake_failed", error=str(exc), mode=m)
        raw.close()
        return

    rfile, wfile = sock.makefile("rb"), sock.makefile("wb")
    try:
        while True:
            req = read_request(rfile)
            if req is None:
                return
            method, path, headers, body = req
            m = mode()                          # re-read per request
            short = path.split("?")[0]
            if m in ("hold", "close"):
                status, _, data = forward(method, path, headers, body)
                log(event="forwarded_then_" + m, method=method, path=short,
                    upstream_status=status, upstream_len=len(data),
                    note="claim committed upstream; response withheld")
                if m == "close":
                    sock.close()
                    return
                time.sleep(120)                 # outlast the worker's 30s read
                sock.close()
                return
            if m == "502":
                log(event="injected", status=502, path=short)
                respond(wfile, 502, b'{"error":"gate-injected bad gateway"}')
                continue
            if m == "401":
                log(event="injected", status=401, path=short)
                respond(wfile, 401, b'{"error":"gate-injected unauthorized"}')
                continue
            status, _, data = forward(method, path, headers, body)
            log(event="forwarded", method=method, path=short, status=status)
            if status == 204:
                respond(wfile, 204)
            else:
                respond(wfile, status, data)
    except (ConnectionResetError, BrokenPipeError, ssl.SSLError, OSError) as exc:
        log(event="conn_closed", error=str(exc))
    finally:
        try:
            sock.close()
        except OSError:
            pass


def main():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(LISTEN)
    srv.listen(16)
    log(event="proxy_listening", addr=f"{LISTEN[0]}:{LISTEN[1]}", mode=mode())
    while True:
        if mode() == "down":
            # Refuse by closing immediately: the worker sees a connect failure.
            srv.close()
            log(event="proxy_down", note="listener closed; expect ConnectError")
            while mode() == "down":
                time.sleep(0.2)
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(LISTEN)
            srv.listen(16)
            log(event="proxy_relistening", mode=mode())
            continue
        srv.settimeout(0.5)
        try:
            raw, _ = srv.accept()
        except socket.timeout:
            continue
        threading.Thread(target=handle, args=(raw,), daemon=True).start()


if __name__ == "__main__":
    sys.exit(main())
