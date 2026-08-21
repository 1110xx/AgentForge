"""Configuration loader — delegates to config.toml reader + provider factory.

这是旧版兼容入口，内部委托给新的 ``config_reader.py`` + ``provider_factory.py``。
新代码请直接使用：
  from enterprise_agent_platform.platform import ConfigReader, ProviderFactory, load_provider
"""
from __future__ import annotations

import os
from typing import Optional

from enterprise_agent_platform.execution.session import RunSessionProvider
from enterprise_agent_platform.platform.config_reader import (
    AppConfig,
    ConfigReader,
    ProviderType,
)
from enterprise_agent_platform.platform.provider_factory import (
    ProviderFactory,
    load_provider as _load_provider,
)


class ConfigLoader:
    """Configuration loader — reads config.toml with environment variable fallback."""

    @staticmethod
    def load_settings() -> AppConfig:
        """Load settings from config.toml (or environment)."""
        reader = ConfigReader()
        return reader.read()

    @staticmethod
    def load_deepseek_api_key() -> str:
        """Load DeepSeek API key: config.toml > environment variables."""
        # First check config.toml
        reader = ConfigReader()
        config = reader.read()
        api_key = config.resolve_api_key()
        if api_key:
            return api_key

        # Fallback: check environment directly
        for key in ("DEEPSEEK_API_KEY", "AGENT_PLATFORM_DEEPSEEK_API_KEY"):
            value = os.getenv(key)
            if value and value.strip():
                return value.strip()

        raise ValueError(
            "No DeepSeek API key found. Please either:\n"
            "  1. Set it in config.toml: api_key = \"...\"\n"
            "  2. Set environment variable: export DEEPSEEK_API_KEY=\"...\"\n"
            "  3. Use demo mode: set provider.type = \"reference\" in config.toml"
        )

    @staticmethod
    def configure_with_deepseek(
        api_key: Optional[str] = None,
        config_path: Optional[str] = None,
    ) -> tuple[AppConfig, RunSessionProvider]:
        """Configure platform with DeepSeek provider from config.toml.

        Args:
            api_key: Override API key (takes precedence over config.toml/env).
            config_path: Path to config.toml (auto-detected if None).

        Returns:
            Tuple of (config, model_provider)
        """
        if api_key:
            os.environ["AGENT_PLATFORM_DEEPSEEK_API_KEY"] = api_key

        config, provider = _load_provider(config_path)
        return config, provider

    @staticmethod
    def create_demo_config() -> tuple[AppConfig, RunSessionProvider]:
        """Create demo configuration — force reference provider (no API key)."""
        reader = ConfigReader()
        config = reader.read()
        factory = ProviderFactory(config)
        # Override to force reference
        from enterprise_agent_platform.reference import ReferenceModelSessionProvider
        provider = ReferenceModelSessionProvider()
        return config, provider


__all__ = ["ConfigLoader"]