"""One .env file; process environment overrides file values."""
import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(repr=False)
class Settings:
    values: dict = field(repr=False)

    def get(self, name, default=""):
        return self.values.get(name, default)

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
    if len(settings.secret) < 32:
        raise ValueError("SECRET_KEY must contain at least 32 characters")
    return settings
