# Deploy-gate fault-injection proxy

A TLS-terminating proxy the worker points at instead of prod, so a fault can be
applied to a worker that is already running with no restart. Built for the
worker crash-loop unit's §8 deploy gate and kept as a reusable artifact: future
fault-injection gates use this rather than reinventing one.

## Why a proxy rather than a packet filter

The brief's original §8.1 used a packet filter to drop traffic on the
established flow. This laptop has no `iptables`, `nft` or `socat` and no
non-interactive root, so that is unavailable. The proxy reaches the same
behaviour: the worker's pooled keep-alive connection to it is established, and
`hold` mode withholds the response on that connection, producing a real
`ReadTimeout` with no restart. See the §8 amendment in the spec brief.

## Setup

```
cd <this dir>
./make-certs.sh            # gate CA, leaf for 127.0.0.1, wrong-hostname leaf, bundle
python3 proxy.py &         # listens on 127.0.0.1:8443
```

`make-certs.sh` writes `bundle.pem` — certifi's trust store **concatenated**
with the gate CA. Concatenated, never replaced: `SSL_CERT_FILE` sets `cafile`,
which REPLACES the default store, so a bare gate CA would break every leg of the
gate that talks to real prod.

Point the worker at it by setting `RESEARCH_SERVER_URL=https://127.0.0.1:8443`
and `SSL_CERT_FILE=<this dir>/bundle.pem`, then restart the unit.

## Modes

Write one word into `mode`; it is re-read on every request, so no restart of
either the proxy or the worker is needed.

| mode | worker observes |
|---|---|
| `forward` | normal proxying to prod |
| `hold` | `httpx.ReadTimeout` — forwards first, so the claim COMMITS, then withholds the response |
| `close` | `httpx.RemoteProtocolError` — forwards, then closes with no response |
| `502` | `httpx.HTTPStatusError`, transient |
| `401` | `httpx.HTTPStatusError`, permanent |
| `blackhole` | `httpx.ConnectTimeout` — accepts TCP, stalls the TLS handshake |
| `down` | `httpx.ConnectError` — stops listening |
| `badcert` | `httpx.ConnectError` with an `ssl.SSLCertVerificationError` cause |

`hold` and `close` forward to prod BEFORE withholding on purpose: the orphaned-
claim demonstration needs a claim genuinely committed server-side.

Verify any mode against the brief's §8.1 table before relying on it. That
condition is what surfaced the R-CERT predicate defect: `badcert` proved
`is_cert_verify_fault` returned False on every real certificate failure.

## Teardown

Kill the proxy, restore `RESEARCH_SERVER_URL`, unset `SSL_CERT_FILE`, restart
the unit, and confirm a successful poll on the default trust bundle.
