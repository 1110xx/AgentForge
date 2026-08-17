"""Runtime configuration for the standalone platform."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class PlatformSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENT_PLATFORM_", extra="ignore")

    database_url: str = (
        "postgresql+asyncpg://agent_platform:agent_platform@localhost:5432/agent_platform"
    )

    def database_dsn(self) -> str:
        return self.database_url
