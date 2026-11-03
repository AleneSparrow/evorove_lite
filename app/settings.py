"""One .env file; process environment overrides file values."""
import os
import secrets
from urllib.parse import urlparse
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(repr=False)
class Settings:
    values: dict = field(repr=False)

    def get(self, name, default=""):
        return self.values.get(name) or default

    @property
    def secret(self):
        return self.get("SECRET_KEY")

    @property
    def base_url(self):
        return self.get("APP_BASE_URL", "https://localhost:8000").rstrip("/")

    @property
    def db_path(self):
        return self.get("DB_PATH", "evorove.sqlite3")


def load(path=".env"):
    values = {}
    if Path(path).exists():
        for line in Path(path).read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                key, sep, value = line.partition("=")
                if not sep:
                    raise ValueError("Invalid .env entry")
                values[key.strip()] = value.strip().strip("\"'")
    values.update(os.environ)
    settings = Settings(values)
    if not settings.secret and urlparse(settings.base_url).hostname in {"localhost", "127.0.0.1"}:
        directory = Path(".local")
        directory.mkdir(mode=0o700, exist_ok=True)
        key_file = directory / "dev-secret"
        try:
            with open(key_file, "x", opener=lambda path, flags: os.open(path, flags, 0o600)) as file:
                file.write(secrets.token_urlsafe(48))
        except FileExistsError:
            pass
        values["SECRET_KEY"] = key_file.read_text().strip()
    if len(settings.secret) < 32:
        raise ValueError("SECRET_KEY must contain at least 32 characters")
    return settings


def password_cipher(settings):
    """Fernet with a domain-separated key; cryptography approved by the owner."""
    import base64
    import hashlib
    import hmac
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        from fastapi import HTTPException
        raise HTTPException(503, 'Mailbox password encryption is not installed') from None
    key = hmac.new(settings.secret.encode(), b'evorove:mailbox-password:v1', hashlib.sha256).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_password(settings, password):
    return password_cipher(settings).encrypt(password.encode()).decode()


def decrypt_password(settings, encrypted):
    return password_cipher(settings).decrypt(encrypted.encode()).decode()
