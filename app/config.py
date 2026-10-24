from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Beautyfort Sync"
    app_environment: str = "development"
    app_url: str = "http://localhost:8000"
    app_secret: str = "change-me-in-production"
    token_encryption_key: str | None = None
    database_url: str = "sqlite:///./beautyfort.db"

    shopify_client_id: str = ""
    shopify_client_secret: str = ""
    shopify_api_version: str = "2026-10"
    shopify_scopes: str = "write_products,write_inventory,read_locations"
    shopify_shop: str = ""
    shopify_access_token: str = ""
    shopify_location_id: str = ""

    beautyfort_api_url: str = "https://www.beautyfort.com/api/soap"
    beautyfort_username: str = ""
    beautyfort_secret: str = ""
    beautyfort_test_mode: bool = False

    sync_interval_minutes: int = Field(default=60, ge=5)
    worker_poll_seconds: int = Field(default=5, ge=1, le=60)
    automatic_apply: bool = False
    admin_password: str = ""
    port: int = 8000

    @field_validator("shopify_shop")
    @classmethod
    def normalize_shop(cls, value: str) -> str:
        value = value.strip().lower()
        if value and not value.endswith(".myshopify.com"):
            value = f"{value}.myshopify.com"
        return value

    @field_validator("database_url")
    @classmethod
    def normalize_database_url(cls, value: str) -> str:
        value = value.strip()
        if value.startswith("postgres://"):
            return "postgresql+psycopg://" + value.removeprefix("postgres://")
        if value.startswith("postgresql://"):
            return "postgresql+psycopg://" + value.removeprefix("postgresql://")
        return value

    @property
    def oauth_redirect_uri(self) -> str:
        return f"{self.app_url.rstrip('/')}/auth/callback"

    @property
    def app_uninstalled_webhook_uri(self) -> str:
        return f"{self.app_url.rstrip('/')}/webhooks/app/uninstalled"

    @property
    def is_production(self) -> bool:
        return self.app_environment.strip().lower() == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
