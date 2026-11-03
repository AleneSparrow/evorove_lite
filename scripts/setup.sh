#!/bin/sh
set -eu
PYTHON_BIN="${PYTHON_BIN:-python3.12}"
"$PYTHON_BIN" -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
if [ ! -f .env ]; then
    cp .env.example .env
    .venv/bin/python -c 'import secrets; from pathlib import Path; p=Path(".env"); p.write_text(p.read_text().replace("REPLACE_WITH_RANDOM_SECRET",secrets.token_urlsafe(48)))'
    chmod 600 .env
fi
mkdir -p .local
if [ ! -f .local/key.pem ]; then
    openssl req -x509 -newkey rsa:2048 -nodes -keyout .local/key.pem -out .local/cert.pem -days 365 -subj /CN=localhost -addext 'subjectAltName=DNS:localhost,IP:127.0.0.1'
    chmod 600 .local/key.pem
fi
