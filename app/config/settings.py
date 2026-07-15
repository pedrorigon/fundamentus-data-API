from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from app import __version__


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="FUNDAMENTUS_API_",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "Fundamentus Data API"
    environment: str = "local"
    bind_host: str = "127.0.0.1"
    bind_port: int = 8000
    timezone: str = "America/Sao_Paulo"

    fundamentus_base_url: str = "https://www.fundamentus.com.br"
    b3_bdi_base_url: str = "https://arquivos.b3.com.br/bdi"
    status_invest_base_url: str = "https://statusinvest.com.br"
    brapi_base_url: str = "https://brapi.dev"
    brapi_token: SecretStr | None = None
    alpha_vantage_base_url: str = "https://www.alphavantage.co"
    alpha_vantage_api_key: SecretStr | None = None
    bazin_minimum_yield_percent: Decimal = Decimal("6")
    user_agent: str = (
        f"fundamentus-data-api/{__version__} local scraper "
        "(contact: local-development; purpose: personal local API)"
    )
    request_timeout_seconds: float = 10.0
    max_connections: int = 8
    max_keepalive_connections: int = 4
    upstream_concurrency: int = 4
    upstream_min_interval_seconds: float = 0.15

    retry_attempts: int = 3
    retry_backoff_seconds: float = 0.25
    circuit_breaker_failures: int = 5
    circuit_breaker_recovery_seconds: float = 60.0

    market_data_ttl_seconds: int = 300
    fundamentals_ttl_seconds: int = 3600
    dividends_ttl_seconds: int = 21600
    cache_headers_max_age_seconds: int = 60
    opportunity_cache_ttl_seconds: int = 900
    instrument_data_ttl_seconds: int = 86400

    sqlite_cache_enabled: bool = True
    sqlite_cache_path: Path = Field(default=Path(".cache/fundamentus_cache.sqlite3"))

    batch_limit: int = 20
    cache_invalidate_token: SecretStr | None = None

    @property
    def details_ttl_seconds(self) -> int:
        return min(self.market_data_ttl_seconds, self.fundamentals_ttl_seconds)


@lru_cache
def get_settings() -> Settings:
    return Settings()
