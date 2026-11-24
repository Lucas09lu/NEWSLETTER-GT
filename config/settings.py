"""Configuración central del sistema, leída de variables de entorno / `.env`.

Todas las credenciales son opcionales a nivel de carga: cada conector decide
si puede ejecutarse (`Settings.has_*`) y, si no, se registra como "skipped"
sin detener el pipeline. Así `--dry-run` funciona desde el primer día.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Claude ---------------------------------------------------------------
    claude_api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("CLAUDE_API_KEY", "ANTHROPIC_API_KEY")
    )
    claude_model: str = "claude-opus-5-5"

    # --- Brave Search ---------------------------------------------------------
    brave_api_key: SecretStr | None = None
    brave_min_interval_seconds: float = 1.1

    # --- Buzón IMAP -----------------------------------------------------------
    newsletter_inbox_imap: str | None = None
    newsletter_inbox_port: int = 993
    newsletter_inbox_user: str | None = None
    newsletter_inbox_pass: SecretStr | None = None
    newsletter_inbox_folder: str = "INBOX"

    # --- SMTP -----------------------------------------------------------------
    sender_email: str | None = None
    sender_name: str = "Global Transport · Inteligencia Comex"
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_pass: SecretStr | None = None
    smtp_starttls: bool = True
    recipients: str = "wca.sales@gt.com.pe"

    # --- Web App --------------------------------------------------------------
    app_base_url: str = "https://lucas09lu.github.io/NEWSLETTER-GT/"

    # --- Rutas y ajustes ------------------------------------------------------
    sources_xlsx: Path = Path("data/fuentes-comercio-exterior-peru.xlsx")
    data_dir: Path = Path("data")
    output_dir: Path = Path("out")
    http_timeout_seconds: float = 20.0
    log_level: str = "INFO"

    # --- Helpers ----------------------------------------------------------------
    def resolve(self, path: Path) -> Path:
        """Rutas relativas se resuelven contra la raíz del repo."""
        return path if path.is_absolute() else ROOT_DIR / path

    @property
    def sources_path(self) -> Path:
        return self.resolve(self.sources_xlsx)

    @property
    def data_path(self) -> Path:
        return self.resolve(self.data_dir)

    @property
    def output_path(self) -> Path:
        return self.resolve(self.output_dir)

    @property
    def recipient_list(self) -> list[str]:
        return [r.strip() for r in self.recipients.split(",") if r.strip()]

    @property
    def has_claude(self) -> bool:
        return bool(self.claude_api_key and self.claude_api_key.get_secret_value())

    @property
    def has_brave(self) -> bool:
        return bool(self.brave_api_key and self.brave_api_key.get_secret_value())

    @property
    def has_inbox(self) -> bool:
        return bool(
            self.newsletter_inbox_imap
            and self.newsletter_inbox_user
            and self.newsletter_inbox_pass
            and self.newsletter_inbox_pass.get_secret_value()
        )

    @property
    def has_smtp(self) -> bool:
        return bool(self.smtp_host and self.sender_email and self.recipient_list)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
