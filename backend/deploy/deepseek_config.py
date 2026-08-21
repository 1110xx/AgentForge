"""
Production deployment configuration for DeepSeek model provider.

This module provides production-ready configuration for using DeepSeek v4 Flash
as the default model provider in the Enterprise Agent Platform.
"""
import os
from typing import Optional

from enterprise_agent_platform.config import PlatformSettings
from enterprise_agent_platform.execution.session import RunSessionProvider
from enterprise_agent_platform.platform.model_provider_config import ModelProviderConfig


class DeepSeekProductionConfig:
    """Production configuration for DeepSeek model provider."""
    
    def __init__(self, settings: Optional[PlatformSettings] = None):
        self.settings = settings or self._load_production_settings()
    
    def _load_production_settings(self) -> PlatformSettings:
        """Load production settings with validation."""
        settings = PlatformSettings()
        
        # Validate DeepSeek configuration
        if not settings.deepseek_api_key:
            raise ValueError(
                "DEEPSEEK_API_KEY is required for production deployment. "
                "Set it in environment variables or Kubernetes secrets."
            )
        
        # Validate DeepSeek base URL
        if not settings.deepseek_base_url.startswith("https://"):
            raise ValueError("DEEPSEEK_BASE_URL must use HTTPS protocol")
        
        return settings
    
    def create_production_provider(self) -> RunSessionProvider:
        """Create production-ready DeepSeek provider with enhanced features."""
        from enterprise_agent_platform.reference.deepseek_provider import DeepSeekModelSessionProvider
        
        # Create provider with production settings
        provider = DeepSeekModelSessionProvider(
            api_key=self.settings.deepseek_api_key,
            base_url=self.settings.deepseek_base_url
        )
        
        return provider
    
    def create_fallback_chain(self) -> tuple[RunSessionProvider, RunSessionProvider]:
        """Create primary and fallback providers."""
        config = ModelProviderConfig(self.settings)
        
        # Primary provider (DeepSeek if available, otherwise reference)
        primary = config.create_default_provider()
        
        # Fallback provider (always reference)
        fallback = config.create_fallback_provider()
        
        return primary, fallback
    
    def get_kubernetes_config(self) -> dict:
        """Get Kubernetes configuration for DeepSeek provider."""
        return {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {
                "name": "deepseek-api-key",
                "namespace": "agent-platform"
            },
            "type": "Opaque",
            "data": {
                "DEEPSEEK_API_KEY": self.settings.deepseek_api_key.encode()
            }
        }
    
    def get_docker_env(self) -> dict[str, str]:
        """Get Docker environment variables."""
        return {
            "DEEPSEEK_API_KEY": self.settings.deepseek_api_key,
            "DEEPSEEK_BASE_URL": self.settings.deepseek_base_url,
            "DEFAULT_MODEL": self.settings.default_model,
            "DATABASE_URL": self.settings.database_url
        }
    
    def validate_production_readiness(self) -> list[str]:
        """Validate production readiness and return issues found."""
        issues = []
        
        # Check API key
        if not self.settings.deepseek_api_key:
            issues.append("DEEPSEEK_API_KEY not configured")
        
        # Check base URL
        if not self.settings.deepseek_base_url:
            issues.append("DEEPSEEK_BASE_URL not configured")
        elif not self.settings.deepseek_base_url.startswith("https://"):
            issues.append("DEEPSEEK_BASE_URL must use HTTPS")
        
        # Check database URL
        if not self.settings.database_url:
            issues.append("DATABASE_URL not configured")
        elif "postgresql" not in self.settings.database_url:
            issues.append("DATABASE_URL must use PostgreSQL")
        
        # Check for default model
        if not self.settings.default_model:
            issues.append("DEFAULT_MODEL not configured")
        
        return issues


def create_production_deployment() -> DeepSeekProductionConfig:
    """Create production deployment configuration."""
    try:
        return DeepSeekProductionConfig()
    except ValueError as e:
        print(f"❌ Production configuration error: {e}")
        print("\n🔧 To fix this issue:")
        print("   1. Set DEEPSEEK_API_KEY environment variable")
        print("   2. Set DEEPSEEK_BASE_URL environment variable")
        print("   3. Ensure DATABASE_URL is configured")
        print("   4. Set DEFAULT_MODEL environment variable")
        raise


def create_development_config() -> DeepSeekProductionConfig:
    """Create development configuration with fallback."""
    # Allow missing API key in development
    os.environ.setdefault("DEFAULT_MODEL", "deepseek-chat")
    os.environ.setdefault("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
    
    return DeepSeekProductionConfig()


__all__ = [
    "DeepSeekProductionConfig",
    "create_production_deployment",
    "create_development_config"
]