"""Provider factory — creates the right model provider based on config.toml.

流程:
  1. 读取 config.toml -> AppConfig
  2. 根据 provider.type 解析出实际使用的提供商
  3. 创建对应的 RunSessionProvider 实例
  4. 返回并支持回退链
"""
from __future__ import annotations

import sys
import time
from typing import Optional

from enterprise_agent_platform.execution.session import (
    RunSessionProvider,
    SessionProviderError,
)
from enterprise_agent_platform.platform.config_reader import (
    AppConfig,
    ConfigReader,
    ProviderType,
)


def _create_deepseek_provider(config: AppConfig) -> RunSessionProvider:
    """Create DeepSeek provider from config."""
    from enterprise_agent_platform.reference.deepseek_provider import (
        DeepSeekModelSessionProvider,
    )

    return DeepSeekModelSessionProvider(
        api_key=config.resolve_api_key(),
        base_url=config.resolve_base_url(),
        app_config=config,
    )


def _create_openai_provider(config: AppConfig) -> RunSessionProvider:
    """Create OpenAI provider from config (placeholder)."""
    from enterprise_agent_platform.reference.deepseek_provider import (
        DeepSeekModelSessionProvider,
    )

    print("[WARN] OpenAI provider not yet implemented, using DeepSeek as fallback", file=sys.stderr)
    return DeepSeekModelSessionProvider(
        api_key=config.resolve_api_key(),
        base_url=config.resolve_base_url(),
        app_config=config,
    )


def _create_anthropic_provider(config: AppConfig) -> RunSessionProvider:
    """Create Anthropic provider from config (placeholder)."""
    from enterprise_agent_platform.reference.deepseek_provider import (
        DeepSeekModelSessionProvider,
    )

    print("[WARN] Anthropic provider not yet implemented, using DeepSeek as fallback", file=sys.stderr)
    return DeepSeekModelSessionProvider(
        api_key=config.resolve_api_key(),
        base_url=config.resolve_base_url(),
        app_config=config,
    )


def _create_reference_provider(config: AppConfig) -> RunSessionProvider:
    """Create reference/demo provider (no API key needed)."""
    from enterprise_agent_platform.reference import ReferenceModelSessionProvider

    return ReferenceModelSessionProvider()


def _create_inmemory_provider(config: AppConfig) -> RunSessionProvider:
    """Create in-memory fallback provider."""
    from enterprise_agent_platform.reference import InMemoryRunSessionProvider

    return InMemoryRunSessionProvider()


# Provider registry — add new providers here
_PROVIDER_REGISTRY: dict[ProviderType, callable] = {
    "deepseek": _create_deepseek_provider,
    "openai": _create_openai_provider,
    "anthropic": _create_anthropic_provider,
    "reference": _create_reference_provider,
}


class ProviderFactory:
    """Factory for creating model providers based on user configuration."""

    def __init__(self, config: Optional[AppConfig] = None, config_path: Optional[str] = None):
        if config is None:
            reader = ConfigReader(config_path)
            config = reader.read()
        self.config = config

    def create_primary(self) -> RunSessionProvider:
        """Create the primary provider based on resolved provider type."""
        resolved_type = self.config.resolve_provider_type()
        print(f"[provider] Creating: {resolved_type} (model: {self.config.provider.model})")

        factory_fn = _PROVIDER_REGISTRY.get(resolved_type)
        if factory_fn is None:
            print(f"[WARN] Unknown provider type '{resolved_type}', falling back to reference")
            return _create_reference_provider(self.config)

        try:
            return factory_fn(self.config)
        except Exception as e:
            print(f"[WARN] Failed to create {resolved_type} provider: {e}")
            if self.config.fallback.enable_fallback:
                return self._create_fallback()
            raise

    def create_fallback(self) -> RunSessionProvider:
        """Create the fallback provider."""
        return self._create_fallback()

    def _create_fallback(self) -> RunSessionProvider:
        """Internal fallback creation."""
        fallback_type = self.config.fallback.fallback_provider_type
        print(f"[provider] Fallback to: {fallback_type}")

        factory_fn = _PROVIDER_REGISTRY.get(fallback_type, _create_reference_provider)
        return factory_fn(self.config)

    def create_with_retry(self, max_retries: Optional[int] = None) -> RunSessionProvider:
        """Create primary provider with retry logic, falls back on failure."""
        retries = max_retries if max_retries is not None else self.config.fallback.max_retries
        delay = self.config.fallback.retry_delay_seconds

        for attempt in range(retries):
            try:
                return self.create_primary()
            except Exception as e:
                print(
                    f"[WARN] Provider creation attempt {attempt + 1}/{retries} failed: {e}",
                    file=sys.stderr,
                )
                if attempt < retries - 1:
                    time.sleep(delay)

        if self.config.fallback.enable_fallback:
            return self.create_fallback()

        raise SessionProviderError(
            "PROVIDER_CREATION_FAILED",
            f"All {retries} attempts to create provider failed",
        )

    @staticmethod
    def from_default_config() -> tuple[AppConfig, RunSessionProvider]:
        """Convenience: read default config and create provider."""
        reader = ConfigReader()
        config = reader.read()
        factory = ProviderFactory(config)
        provider = factory.create_primary()
        return config, provider

    @staticmethod
    def print_config_status(config: AppConfig) -> None:
        """Print current configuration status for user feedback."""
        print("-" * 50)
        print("[Config] Current configuration status")
        print("-" * 50)
        print(f"  Provider type:       {config.provider.type}")
        print(f"  Resolved provider:   {config.resolve_provider_type()}")
        print(f"  Model:               {config.provider.model}")
        print(f"  API Key status:      {'[SET]' if config.resolve_api_key() else '[MISSING]'}")
        print(f"  API endpoint:        {config.resolve_base_url() or '[NOT SET]'}")
        print(f"  Temperature:         {config.parameters.temperature}")
        print(f"  Max tokens:          {config.parameters.max_tokens}")
        print(f"  Session memory:      {'[ON]' if config.session.enabled else '[OFF]'}")
        print(f"  Follow-up read-only: {'[ON]' if config.session.read_only_followup else '[OFF]'}")
        print(f"  Log level:           {config.logging.level}")
        print(f"  Fallback strategy:   {'[ON]' if config.fallback.enable_fallback else '[OFF]'} -> {config.fallback.fallback_provider_type}")
        print("-" * 50)


# Convenience function outside the class
def load_provider(config_path: Optional[str] = None) -> tuple[AppConfig, RunSessionProvider]:
    """Load config and create provider in one call."""
    reader = ConfigReader(config_path)
    config = reader.read()
    factory = ProviderFactory(config)
    provider = factory.create_primary()
    return config, provider


__all__ = [
    "ProviderFactory",
    "load_provider",
]