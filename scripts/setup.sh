#!/bin/sh
set -eu
PYTHON_BIN="${PYTHON_BIN:-python3.12}"
"$PYTHON_BIN" -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
mkdir -p .local
if [ ! -f .local/key.pem ]; then
    openssl req -x509 -newkey rsa:2048 -nodes -keyout .local/key.pem -out .local/cert.pem -days 365 -subj /CN=localhost -addext 'subjectAltName=DNS:localhost,IP:127.0.0.1'
    chmod 600 .local/key.pem
fi
