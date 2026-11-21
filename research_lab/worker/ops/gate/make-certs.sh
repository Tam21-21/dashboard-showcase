#!/usr/bin/env bash
# Gate CA + leaf certs + the concatenated trust bundle. Idempotent.
set -euo pipefail
cd "$(dirname "$0")"
CERTIFI=$(../../.venv/bin/python -c "import certifi; print(certifi.where())")

openssl req -x509 -newkey rsa:2048 -nodes -keyout ca.key -out ca.crt -days 2 \
  -subj "/CN=worker-gate-CA" \
  -addext "basicConstraints=critical,CA:TRUE" \
  -addext "keyUsage=critical,keyCertSign,cRLSign" 2>/dev/null

printf 'subjectAltName=IP:127.0.0.1,DNS:localhost\nbasicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n' > leaf.ext
printf 'subjectAltName=DNS:wrong.example.invalid\nbasicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n' > bad.ext

openssl req -newkey rsa:2048 -nodes -keyout srv.key -out srv.csr -subj "/CN=127.0.0.1" 2>/dev/null
openssl x509 -req -in srv.csr -CA ca.crt -CAkey ca.key -CAcreateserial -out srv.crt -days 2 -extfile leaf.ext 2>/dev/null

openssl req -newkey rsa:2048 -nodes -keyout srv-badname.key -out bad.csr -subj "/CN=wrong.example.invalid" 2>/dev/null
openssl x509 -req -in bad.csr -CA ca.crt -CAkey ca.key -CAcreateserial -out srv-badname.crt -days 2 -extfile bad.ext 2>/dev/null

# CONCATENATED, not replaced: SSL_CERT_FILE sets cafile, which replaces the
# default store, so a bare gate CA would break every leg that talks to prod.
cat "$CERTIFI" ca.crt > bundle.pem
echo "bundle.pem: $(grep -c 'BEGIN CERTIFICATE' bundle.pem) certs (certifi + gate CA)"
