"""
config.py
─────────
Centralised application settings loaded from environment variables / .env file.

WHY PYDANTIC SETTINGS?
  • Validates every setting at startup – bad config fails fast with a clear error.
  • Secret values are typed SecretStr so they never appear in logs or repr().
  • Single source of truth; imported everywhere instead of os.getenv() calls.

RISK MITIGATED:
  • Hard-coded secrets  → SECRET_KEY must come from the environment.
  • Weak default tokens → minimum-length validation enforced.
"""

from functools import lru_cache
from typing import Optional
from pydantic import field_validator, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict



class Settings(BaseSettings):
    # ── App meta ──────────────────────────────────────────────────────────
    APP_NAME: str = "Uza Pap POS"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = False

    # ── Security ──────────────────────────────────────────────────────────
    SECRET_KEY: SecretStr  # Required – no default so startup fails if missing
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 480
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # ── Database ──────────────────────────────────────────────────────────
    DATABASE_URL: str = "sqlite:///./pharmacy_pos.db"

    # ── OpenFDA (WHO-aligned drug data) ───────────────────────────────────
    OPENFDA_BASE_URL: str = "https://api.fda.gov/drug"
    OPENFDA_API_KEY: SecretStr = SecretStr("")

    # ── Bootstrap admin ───────────────────────────────────────────────────
    # Set AUTO_CREATE_FIRST_ADMIN=False if you want pharmacies to sign up
    # and pay for their own first admin account via the landing page
    # (POST /auth/register/initiate) instead of it being created from
    # .env on every startup. The two setup paths are mutually exclusive —
    # the auto-created admin would otherwise always exist first, blocking
    # the paid self-registration flow's "fresh install" check.
    AUTO_CREATE_FIRST_ADMIN: bool = True
    FIRST_ADMIN_EMAIL: str = "admin@pharmacy.local"
    FIRST_ADMIN_PASSWORD: Optional[SecretStr] = None
    FIRST_ADMIN_NAME: str = "System Admin"

    # ── Email (password reset codes, etc.) ───────────────────────────────
    # If SMTP_HOST is left blank, emails are logged to the console instead
    # of actually sent — useful for local/dev use without mail setup.
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: SecretStr = SecretStr("")
    SMTP_FROM_EMAIL: str = "no-reply@pharmacy.local"
    SMTP_FROM_NAME: str = "Uza Pap POS"
    SMTP_USE_TLS: bool = True

    # ── Password reset ────────────────────────────────────────────────────
    PASSWORD_RESET_CODE_EXPIRE_MINUTES: int = 15

    # ── Self-registration (landing page paid signup) ─────────────────────
    REGISTRATION_FEE_KES: int = 300

    # ── Pydantic v2 config ────────────────────────────────────────────────
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
    )

    ALLOWED_ORIGINS: list[str] = [
        "http://localhost:3000",
        "http://localhost:5173",
        "https://uzazpap.memeyai.com",
        "https://uzazpop-by7v.onrender.com"
    ]

    TRUSTED_HOSTS: list[str] = [
        "localhost",
        "127.0.0.1",
        "uzazpop-by7v.onrender.com"
    ]

    # ── Validators ────────────────────────────────────────────────────────
    @field_validator("SECRET_KEY")
    @classmethod
    def secret_key_must_be_long(cls, v: SecretStr) -> SecretStr:
        """Force SECRET_KEY to be at least 32 characters to prevent brute-force."""
        if len(v.get_secret_value()) < 32:
            raise ValueError(
                "SECRET_KEY must be at least 32 characters. "
                "Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\""
            )
        return v

    @field_validator("ACCESS_TOKEN_EXPIRE_MINUTES")
    @classmethod
    def token_expiry_reasonable(cls, v: int) -> int:
        if not (5 <= v <= 1440):
            raise ValueError("ACCESS_TOKEN_EXPIRE_MINUTES must be between 5 and 1440")
        return v

    # ── M-Pesa / Daraja ─
    MPESA_CONSUMER_KEY: SecretStr  # From Daraja portal
    MPESA_CONSUMER_SECRET: SecretStr  # From Daraja portal
    MPESA_SHORTCODE: str  # Your Paybill or Till number
    MPESA_SHORTCODE_TYPE: str = "paybill"  # "paybill" or "till"
    MPESA_PASSKEY: SecretStr  # Lipa Na M-Pesa Online passkey from portal
    MPESA_CALLBACK_URL: str  # Public HTTPS URL Safaricom will POST results to
    MPESA_ENV: str = "sandbox"  # "sandbox" or "production"

    @property
    def MPESA_BASE_URL(self) -> str:
        if self.MPESA_ENV == "production":
            return "https://api.safaricom.co.ke"
        return "https://sandbox.safaricom.co.ke"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    Return a cached singleton Settings object.
    lru_cache means the .env file is read only once per process – efficient and
    consistent.  In tests, call get_settings.cache_clear() to reset.
    """
    return Settings()
