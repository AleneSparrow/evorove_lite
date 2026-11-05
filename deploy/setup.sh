#!/bin/sh
set -eu

APP_ROOT=/opt/evorove
APP_USER=evorove
APP_GROUP=evorove
DATA_DIR=/var/lib/evorove
CONFIG_DIR=/etc/evorove
LITESTREAM_VERSION=0.5.14
SSH_PORT=${SSH_PORT:-22}

if [ "$(id -u)" -ne 0 ]; then
    echo "setup.sh must be run as root" >&2
    exit 1
fi

if [ ! -r /etc/os-release ]; then
    echo "Cannot identify the operating system" >&2
    exit 1
fi

# shellcheck source=/dev/null
. /etc/os-release
if [ "${ID:-}" != "ubuntu" ] || [ "${VERSION_ID:-}" != "24.04" ]; then
    echo "Ubuntu 24.04 is required" >&2
    exit 1
fi

SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
SOURCE_ROOT=$(CDPATH='' cd -- "$SCRIPT_DIR/.." && pwd)
if [ "$SOURCE_ROOT" != "$APP_ROOT" ]; then
    echo "Clone or copy the repository to $APP_ROOT before running setup.sh" >&2
    exit 1
fi

case "$SSH_PORT" in
    ''|*[!0-9]*)
        echo "SSH_PORT must be a number" >&2
        exit 1
        ;;
esac
if [ "$SSH_PORT" -lt 1 ] || [ "$SSH_PORT" -gt 65535 ]; then
    echo "SSH_PORT must be between 1 and 65535" >&2
    exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates curl git python3.12 python3.12-venv python3-pip ufw

install -d -m 0755 /usr/share/keyrings
if [ ! -s /usr/share/keyrings/cloudflare-main.gpg ]; then
    curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg \
        -o /usr/share/keyrings/cloudflare-main.gpg
fi
printf '%s\n' \
    'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared noble main' \
    > /etc/apt/sources.list.d/cloudflared.list
apt-get update
apt-get install -y cloudflared

if ! getent group "$APP_GROUP" >/dev/null 2>&1; then
    groupadd --system "$APP_GROUP"
fi
if ! id -u "$APP_USER" >/dev/null 2>&1; then
    useradd --system --gid "$APP_GROUP" --home-dir "$DATA_DIR" --create-home \
        --shell /usr/sbin/nologin "$APP_USER"
fi
if ! getent group cloudflared >/dev/null 2>&1; then
    groupadd --system cloudflared
fi
if ! id -u cloudflared >/dev/null 2>&1; then
    useradd --system --gid cloudflared --home-dir /nonexistent --no-create-home \
        --shell /usr/sbin/nologin cloudflared
fi

install -d -o "$APP_USER" -g "$APP_GROUP" -m 0700 "$DATA_DIR"
install -d -o root -g "$APP_GROUP" -m 0750 "$CONFIG_DIR"
install -d -o root -g cloudflared -m 0750 /etc/cloudflared

if [ ! -e "$CONFIG_DIR/litestream.env" ]; then
    install -o root -g "$APP_GROUP" -m 0640 /dev/null "$CONFIG_DIR/litestream.env"
fi
if [ ! -e /etc/cloudflared/evorove.token ]; then
    install -o cloudflared -g cloudflared -m 0600 /dev/null /etc/cloudflared/evorove.token
fi

ARCH=$(dpkg --print-architecture)
case "$ARCH" in
    amd64)
        LITESTREAM_ARCH=x86_64
        LITESTREAM_SHA256=2039868cd4b8a95e1d674bbb6546a4db3ea7a014d0c3bd1ff98db01f03efb788
        ;;
    arm64)
        LITESTREAM_ARCH=arm64
        LITESTREAM_SHA256=998acfc8724b0de5a23e9dde7fe16276adba5ad2a7acd44a8e2901861d4b7784
        ;;
    *)
        echo "Unsupported architecture for Litestream: $ARCH" >&2
        exit 1
        ;;
esac

if ! litestream version 2>/dev/null | grep -Fq "$LITESTREAM_VERSION"; then
    LITESTREAM_DEB=/tmp/litestream-${LITESTREAM_VERSION}-${LITESTREAM_ARCH}.deb
    curl -fsSL \
        "https://github.com/benbjohnson/litestream/releases/download/v${LITESTREAM_VERSION}/litestream-${LITESTREAM_VERSION}-linux-${LITESTREAM_ARCH}.deb" \
        -o "$LITESTREAM_DEB"
    if ! printf '%s  %s\n' "$LITESTREAM_SHA256" "$LITESTREAM_DEB" \
        | sha256sum --check --strict; then
        rm -f "$LITESTREAM_DEB"
        echo "Litestream package checksum verification failed" >&2
        exit 1
    fi
    dpkg -i "$LITESTREAM_DEB"
    rm -f "$LITESTREAM_DEB"
fi

chown -R "$APP_USER:$APP_GROUP" "$APP_ROOT"
if [ ! -e "$APP_ROOT/.env" ]; then
    install -o root -g "$APP_GROUP" -m 0640 "$APP_ROOT/.env.example" "$APP_ROOT/.env"
fi
if [ ! -x "$APP_ROOT/.venv/bin/python" ]; then
    runuser -u "$APP_USER" -- python3.12 -m venv "$APP_ROOT/.venv"
fi
runuser -u "$APP_USER" -- "$APP_ROOT/.venv/bin/pip" install \
    --requirement "$APP_ROOT/requirements.txt"

install -o root -g root -m 0644 "$SCRIPT_DIR/evorove.service" \
    /etc/systemd/system/evorove.service
install -o root -g root -m 0644 "$SCRIPT_DIR/cloudflared/cloudflared.service" \
    /etc/systemd/system/cloudflared.service
install -o root -g root -m 0644 "$SCRIPT_DIR/litestream.service" \
    /etc/systemd/system/litestream.service
install -o root -g root -m 0644 "$SCRIPT_DIR/litestream.yml" /etc/litestream.yml

if [ ! -e /swapfile ]; then
    fallocate -l 2G /swapfile
    chmod 0600 /swapfile
    mkswap /swapfile
fi
if ! swapon --show=NAME --noheadings | grep -Fxq /swapfile; then
    swapon /swapfile
fi
if ! grep -Eq '^/swapfile[[:space:]]' /etc/fstab; then
    printf '%s\n' '/swapfile none swap sw 0 0' >> /etc/fstab
fi

ufw default deny incoming
ufw default allow outgoing
ufw allow "$SSH_PORT/tcp" comment SSH
ufw --force enable

systemctl daemon-reload
systemctl enable evorove.service cloudflared.service litestream.service

echo "Installation complete. Fill $APP_ROOT/.env, $CONFIG_DIR/litestream.env, and the tunnel token."
echo "Services were enabled but not started. Follow docs/DEPLOY_RU.md."
