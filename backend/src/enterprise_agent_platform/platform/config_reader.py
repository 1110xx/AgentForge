"""Configuration file reader — parses config.toml into platform settings.

加载优先级（后覆盖前）：
  1. config.toml 文件默认值
  2. 环境变量（AGENT_PLATFORM_*）
  3. 用户在 config.toml 中显式填入的值

这样用户可以在 config.toml 中覆盖一切，且环境变量作为"免写文件"的快捷方式。
"""
from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional

ProviderType = Literal["auto", "deepseek", "openai", "anthropic", "reference"]

# Tuple for runtime lookup (Literal cannot be subscripted at runtime)
PROVIDER_TYPES: tuple[str, ...] = ("auto", "deepseek", "openai", "anthropic", "reference")


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    """Provider section from config.toml."""
    type: ProviderType = "auto"
    api_key: str = ""
    base_url: str = ""
    model: str = "deepseek-chat"


@dataclass(frozen=True, slots=True)
class ProviderParameters:
    """Model parameters from config.toml."""
    temperature: float = 0.7
    max_tokens: int = 4096
    top_p: float = 0.95
    frequency_penalty: float = 0.0
    presence_penalty: float = 0.0
    timeout_seconds: int = 60


@dataclass(frozen=True, slots=True)
class SessionConfig:
    """Session management config."""
    enabled: bool = True
    max_history_length: int = 100
    auto_close_on_complete: bool = True
    read_only_followup: bool = True


@dataclass(frozen=True, slots=True)
class LoggingConfig:
    """Logging config."""
    level: str = "INFO"
    log_api_calls: bool = False
    log_prompt_content: bool = False


@dataclass(frozen=True, slots=True)
class FallbackConfig:
    """Fallback strategy config."""
    enable_fallback: bool = True
    fallback_provider_type: ProviderType = "reference"
    max_retries: int = 3
    retry_delay_seconds: float = 1.0


@dataclass(frozen=True, slots=True)
class AppConfig:
    """Complete application configuration parsed from config.toml + env."""
    provider: ProviderConfig = field(default_factory=ProviderConfig)
    parameters: ProviderParameters = field(default_factory=ProviderParameters)
    session: SessionConfig = field(default_factory=SessionConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    fallback: FallbackConfig = field(default_factory=FallbackConfig)

    def resolve_provider_type(self) -> ProviderType:
        """Resolve the effective provider type (auto → detected)."""
        t = self.provider.type
        if t != "auto":
            return t
        # Auto-detect: check which API key is available
        env_keys = {
            "DEEPSEEK_API_KEY": "deepseek",
            "OPENAI_API_KEY": "openai",
            "ANTHROPIC_API_KEY": "anthropic",
        }
        for env_var, provider in env_keys.items():
            if os.getenv(env_var, "").strip() or self.provider.api_key.strip():
                return provider  # type: ignore
        return "reference"

    def resolve_api_key(self) -> str:
        """Resolve API key: file value > env var > empty."""
        if self.provider.api_key.strip():
            return self.provider.api_key
        provider_type = self.resolve_provider_type()
        env_map = {
            "deepseek": "DEEPSEEK_API_KEY",
            "openai": "OPENAI_API_KEY",
            "anthropic": "ANTHROPIC_API_KEY",
        }
        env_var = env_map.get(provider_type)
        if env_var:
            return os.getenv(env_var, "")
        return ""

    def resolve_base_url(self) -> str:
        """Resolve base URL: file value > env var > default."""
        if self.provider.base_url.strip():
            return self.provider.base_url
        env_base_url = os.getenv(f"{self.resolve_provider_type().upper()}_BASE_URL", "")
        if env_base_url:
            return env_base_url
        # Defaults per provider
        defaults = {
            "deepseek": "https://api.deepseek.com/v1",
            "openai": "https://api.openai.com/v1",
            "anthropic": "https://api.anthropic.com/v1",
        }
        return defaults.get(self.resolve_provider_type(), "")


class ConfigReader:
    """Reads and parses config.toml, falls back to environment variables."""

    DEFAULT_PATHS: list[Path] = [
        Path("config.toml"),
        Path("backend/config.toml"),
        Path.home() / ".agent-platform" / "config.toml",
        Path("/etc/agent-platform/config.toml"),
    ]

    def __init__(self, config_path: Optional[str | Path] = None):
        self.config_path = self._resolve_path(config_path)

    @staticmethod
    def _resolve_path(config_path: Optional[str | Path] = None) -> Optional[Path]:
        """Find the first existing config file."""
        if config_path:
            p = Path(config_path)
            if p.exists():
                return p
            print(f"⚠️  Specified config file not found: {config_path}", file=sys.stderr)
            return None

        for candidate in ConfigReader.DEFAULT_PATHS:
            if candidate.exists():
                return candidate
        return None

    def read(self) -> AppConfig:
        """Read and parse the configuration file."""
        if self.config_path is None:
            print("ℹ️  No config.toml found, using defaults + environment variables", file=sys.stderr)
            return AppConfig()

        try:
            raw = self.config_path.read_bytes()
            data = tomllib.loads(raw.decode("utf-8"))
        except (tomllib.TOMLDecodeError, OSError) as e:
            print(f"⚠️  Failed to parse config file: {e}", file=sys.stderr)
            return AppConfig()

        return self._parse(data)

    def _parse(self, data: dict) -> AppConfig:
        """Parse TOML data into AppConfig."""
        provider_data = data.get("provider", {})

        provider = ProviderConfig(
            type=self._resolve_enum(provider_data.get("type", "auto"), PROVIDER_TYPES),
            api_key=str(provider_data.get("api_key", "")),
            base_url=str(provider_data.get("base_url", "")),
            model=str(provider_data.get("model", "deepseek-chat")),
        )

        params_data = provider_data.get("parameters", {}) if isinstance(provider_data, dict) else {}
        if not params_data:
            params_data = data.get("parameters", {})

        parameters = ProviderParameters(
            temperature=float(params_data.get("temperature", 0.7)),
            max_tokens=int(params_data.get("max_tokens", 4096)),
            top_p=float(params_data.get("top_p", 0.95)),
            frequency_penalty=float(params_data.get("frequency_penalty", 0.0)),
            presence_penalty=float(params_data.get("presence_penalty", 0.0)),
            timeout_seconds=int(params_data.get("timeout_seconds", 60)),
        )

        session_data = data.get("session", {})
        session = SessionConfig(
            enabled=bool(session_data.get("enabled", True)),
            max_history_length=int(session_data.get("max_history_length", 100)),
            auto_close_on_complete=bool(session_data.get("auto_close_on_complete", True)),
            read_only_followup=bool(session_data.get("read_only_followup", True)),
        )

        logging_data = data.get("logging", {})
        logging = LoggingConfig(
            level=str(logging_data.get("level", "INFO")),
            log_api_calls=bool(logging_data.get("log_api_calls", False)),
            log_prompt_content=bool(logging_data.get("log_prompt_content", False)),
        )

        fallback_data = data.get("fallback", {})
        fallback = FallbackConfig(
            enable_fallback=bool(fallback_data.get("enable_fallback", True)),
            fallback_provider_type=self._resolve_enum(
                fallback_data.get("fallback_provider_type", "reference"), PROVIDER_TYPES
            ),
            max_retries=int(fallback_data.get("max_retries", 3)),
            retry_delay_seconds=float(fallback_data.get("retry_delay_seconds", 1.0)),
        )

        return AppConfig(
            provider=provider,
            parameters=parameters,
            session=session,
            logging=logging,
            fallback=fallback,
        )

    @staticmethod
    def _resolve_enum(value: str, valid: tuple[str, ...]) -> str:
        """Resolve a string to a valid enum value, case-insensitive."""
        val = str(value).lower().strip()
        for v in valid:
            if v == val:
                return v
        print(f"⚠️  Invalid config value '{value}', using '{valid[0]}'", file=sys.stderr)
        return valid[0]

    @staticmethod
    def find_config_files() -> list[Path]:
        """List all accessible config file paths for user reference."""
        return [p for p in ConfigReader.DEFAULT_PATHS]


__all__ = [
    "AppConfig",
    "ConfigReader",
    "FallbackConfig",
    "LoggingConfig",
    "PROVIDER_TYPES",
    "ProviderConfig",
    "ProviderParameters",
    "ProviderType",
    "SessionConfig",
]