"""Configuration and factory for model providers.

This module provides the default model provider configuration and factory functions.
"""
from __future__ import annotations

from typing import Optional

from enterprise_agent_platform.config import PlatformSettings
from enterprise_agent_platform.execution.session import RunSessionProvider
from enterprise_agent_platform.reference import (
    DeepSeekModelSessionProvider,
    InMemoryRunSessionProvider,
    ReferenceModelSessionProvider,
)


class ModelProviderConfig:
    """Configuration for model providers with fallback chain."""
    
    def __init__(self, settings: PlatformSettings):
        self.settings = settings
    
    def create_default_provider(self) -> RunSessionProvider:
        """Create the default model provider based on configuration.
        
        Priority:
        1. DeepSeek provider if API key is configured
        2. Reference model provider for demo
        3. In-memory provider as fallback
        """
        # Try to create DeepSeek provider if API key is available
        if self.settings.deepseek_api_key.strip():
            return self._create_deepseek_provider()
        
        # Fall back to reference model provider for demo functionality
        return self._create_reference_provider()
    
    def _create_deepseek_provider(self) -> DeepSeekModelSessionProvider:
        """Create DeepSeek model provider."""
        return DeepSeekModelSessionProvider(
            api_key=self.settings.deepseek_api_key,
            base_url=self.settings.deepseek_base_url
        )
    
    def _create_reference_provider(self) -> ReferenceModelSessionProvider:
        """Create reference model provider for demo."""
        return ReferenceModelSessionProvider()
    
    def create_fallback_provider(self) -> InMemoryRunSessionProvider:
        """Create in-memory provider as ultimate fallback."""
        return InMemoryRunSessionProvider()


# Global factory function
def create_model_provider(settings: Optional[PlatformSettings] = None) -> RunSessionProvider:
    """Create the configured model provider.
    
    Args:
        settings: Platform settings. If None, creates default settings.
    
    Returns:
        Configured RunSessionProvider instance
    """
    if settings is None:
        settings = PlatformSettings()
    
    config = ModelProviderConfig(settings)
    return config.create_default_provider()


def create_deepseek_provider_only(settings: Optional[PlatformSettings] = None) -> DeepSeekModelSessionProvider:
    """Create DeepSeek provider only (no fallback).
    
    Args:
        settings: Platform settings. If None, creates default settings.
    
    Returns:
        DeepSeekModelSessionProvider instance
    """
    if settings is None:
        settings = PlatformSettings()
    
    return DeepSeekModelSessionProvider(
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek_base_url
    )


__all__ = [
    "ModelProviderConfig",
    "create_model_provider",
    "create_deepseek_provider_only"
]