"""Konfiguration aus Umgebungsvariablen bzw. .env-Datei."""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

APP_VERSION = "0.32.0"
APP_RELEASE_DATE = "2026-10-02"  # Erscheinungstag – für „Updates bis“ der Lizenz
APP_NAME = "DaLiegt’s"  # Anzeigename (technisch bleibt es „lagerwerk“: Paket, /opt/lagerwerk, Datenbank)
EXPORT_FORMAT_VERSION = 1
BASE_DIR = Path(__file__).resolve().parent


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass
class Settings:
    data_dir: Path
    base_url: str
    secret_key: str
    proxy_headers: bool = False
    backup_keep: int = 7
    nas_backup_dir: str = ""
    wled_timeout: float = 1.5
    provider_keys: dict = field(default_factory=dict)
    ha_ingress: bool = False  # als Home-Assistant-App (Ingress) gestartet

    @property
    def db_path(self) -> Path:
        return self.data_dir / "lagerwerk.sqlite"

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.db_path}"

    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups"


def load_settings() -> Settings:
    _load_dotenv(Path(os.environ.get("LAGERWERK_ENV_FILE", Path.cwd() / ".env")))
    data_dir = Path(os.environ.get("LAGERWERK_DATA_DIR", Path.cwd() / "data")).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    secret_file = data_dir / ".secret_key"
    secret = os.environ.get("LAGERWERK_SECRET_KEY", "")
    if not secret:
        if secret_file.exists():
            secret = secret_file.read_text().strip()
        else:
            secret = secrets.token_urlsafe(48)
            secret_file.write_text(secret)
            try:
                secret_file.chmod(0o600)
            except OSError:
                pass
    s = Settings(
        data_dir=data_dir,
        base_url=os.environ.get("LAGERWERK_BASE_URL", "http://lager.local").rstrip("/"),
        secret_key=secret,
        proxy_headers=os.environ.get("LAGERWERK_PROXY_HEADERS", "0") == "1",
        backup_keep=int(os.environ.get("LAGERWERK_BACKUP_KEEP", "7")),
        nas_backup_dir=os.environ.get("LAGERWERK_NAS_BACKUP_DIR", ""),
        wled_timeout=float(os.environ.get("LAGERWERK_WLED_TIMEOUT", "1.5")),
        provider_keys={k: v for k, v in os.environ.items() if k.startswith("LAGERWERK_PROVIDER_")},
        ha_ingress=os.environ.get("LAGERWERK_HA_INGRESS", "0") == "1",
    )
    s.media_dir.mkdir(parents=True, exist_ok=True)
    s.backup_dir.mkdir(parents=True, exist_ok=True)
    return s


settings = load_settings()
