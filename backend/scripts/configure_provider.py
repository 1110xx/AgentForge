#!/usr/bin/env python3
"""Configure model provider -- read config.toml and test connection.

Usage:
  python scripts/configure_provider.py                  # show config status
  python scripts/configure_provider.py --test            # test API connection
  python scripts/configure_provider.py --interactive     # interactive config wizard
"""
import argparse
import asyncio
import os
import sys
from pathlib import Path

# Add src to Python path
src_path = Path(__file__).parent.parent / "src"
sys.path.insert(0, str(src_path.resolve()))

from enterprise_agent_platform.platform.config_reader import (
    AppConfig,
    ConfigReader,
    PROVIDER_TYPES,
)
from enterprise_agent_platform.platform.provider_factory import (
    ProviderFactory,
    load_provider,
)


def show_config(config: AppConfig) -> None:
    """Display current configuration state."""
    resolved = config.resolve_provider_type()
    api_key_status = "[SET]" if config.resolve_api_key() else "[MISSING]"
    base_url_status = "[SET]" if config.resolve_base_url() else "[will use default]"

    print()
    print("=" * 55)
    print("  Current Configuration (config.toml)")
    print("=" * 55)
    print(f"  Provider type:         {config.provider.type}")
    print(f"  Resolved provider:     {resolved}")
    print(f"  Model:                 {config.provider.model}")
    print(f"  API Key:               {api_key_status}")
    print(f"  API Endpoint:          {base_url_status}")
    print(f"  Temperature:           {config.parameters.temperature}")
    print(f"  Max Tokens:            {config.parameters.max_tokens}")
    print(f"  Session Memory:        {'[ON]' if config.session.enabled else '[OFF]'}")
    print(f"  Read-only Followup:    {'[ON]' if config.session.read_only_followup else '[OFF]'}")
    print(f"  Log Level:             {config.logging.level}")
    print(f"  Fallback Strategy:     {'[ON]' if config.fallback.enable_fallback else '[OFF]'} -> {config.fallback.fallback_provider_type}")
    print("=" * 55)
    print()


async def test_connection(config: AppConfig) -> bool:
    """Test API connection with the configured provider."""
    resolved = config.resolve_provider_type()
    api_key = config.resolve_api_key()

    if not api_key and resolved != "reference":
        print("[WARN] No API Key set. Cannot test connection.")
        print("  Please set it via:")
        print("    1. Edit config.toml and add api_key = \"...\"")
        print("    2. Or set environment variable DEEPSEEK_API_KEY")
        print()
        return False

    print(f"[TEST] Testing connection to {resolved} API ...")
    print(f"       Endpoint: {config.resolve_base_url()}")
    print(f"       Model:    {config.provider.model}")
    print()

    try:
        if resolved == "deepseek":
            from enterprise_agent_platform.reference.deepseek_provider import (
                DeepSeekModelSessionProvider,
            )

            provider = DeepSeekModelSessionProvider(
                api_key=api_key,
                base_url=config.resolve_base_url(),
                app_config=config,
            )

            async with provider:
                handle = await provider.open(
                    run_id="test-connection",
                    intent="Test connection and respond with 'pong'",
                    resource_refs=(),
                    host_context_ref=None,
                )
                await provider.run_task(handle)
                answer = await provider.followup(
                    handle,
                    "Reply with ONLY the word: pong",
                )
                print(f"  [PASS] Connection successful! Model response: {answer[:80]}...")
                return True

        elif resolved == "reference":
            from enterprise_agent_platform.reference import (
                ReferenceModelSessionProvider,
            )

            provider = ReferenceModelSessionProvider()
            async with provider:
                handle = await provider.open(
                    run_id="test-connection",
                    intent="Test demo mode",
                    resource_refs=(),
                    host_context_ref=None,
                )
                await provider.run_task(handle)
                answer = await provider.followup(handle, "Test")
                print(f"  [PASS] Reference provider working correctly")
                print(f"  Response: {answer}")
                return True

        else:
            print(f"  [WARN] Provider '{resolved}' test not yet implemented")
            return False

    except Exception as e:
        print(f"  [FAIL] Connection failed: {e}")
        import traceback
        traceback.print_exc()
        return False


def interactive_wizard(config_path: str | None = None) -> None:
    """Interactive configuration wizard."""
    path = config_path or "config.toml"
    config_file = Path(path)

    print()
    print("=" * 55)
    print("  Model Provider Configuration Wizard")
    print("=" * 55)
    print()

    # Step 1: Choose provider type
    print("Step 1: Select model provider")
    print("  1) DeepSeek (default, v4 Flash)")
    print("  2) OpenAI (GPT-4o / GPT-4-turbo)")
    print("  3) Anthropic (Claude 3.5 Sonnet)")
    print("  4) Reference (Demo mode, no API Key needed)")
    print("  5) Auto (detect API Key automatically)")

    choice = input("\n  Enter number [1]: ").strip() or "1"
    provider_map = {
        "1": "deepseek",
        "2": "openai",
        "3": "anthropic",
        "4": "reference",
        "5": "auto",
    }
    provider_type = provider_map.get(choice, "deepseek")

    # Step 2: API Key
    print()
    print("Step 2: API Key (leave empty to use environment variable)")
    api_key = input("  API Key: ").strip()

    # Step 3: Model name
    print()
    print("Step 3: Model name")
    default_model = {
        "deepseek": "deepseek-chat",
        "openai": "gpt-4o",
        "anthropic": "claude-3-5-sonnet-20241022",
    }.get(provider_type, "deepseek-chat")
    model = input(f"  Model [{default_model}]: ").strip() or default_model

    # Step 4: Write config
    print()
    print("Step 4: Generating config.toml")
    print()

    config_content = f"""# =============================================================================
# Enterprise Agent Platform -- Model Provider Configuration
# Generated by configuration wizard
# =============================================================================

[provider]
type = "{provider_type}"
api_key = "{api_key if api_key else ''}"
model = "{model}"

[provider.parameters]
temperature = 0.7
max_tokens = 4096
top_p = 0.95
frequency_penalty = 0.0
presence_penalty = 0.0
timeout_seconds = 60

[session]
enabled = true
max_history_length = 100
auto_close_on_complete = true
read_only_followup = true

[logging]
level = "INFO"
log_api_calls = false
log_prompt_content = false

[fallback]
enable_fallback = true
fallback_provider_type = "reference"
max_retries = 3
retry_delay_seconds = 1.0
"""

    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text(config_content)
    print(f"  [DONE] Configuration written to: {config_file.resolve()}")
    print()

    # Summary
    print("=" * 55)
    print("  Configuration Summary")
    print("=" * 55)
    print(f"  Provider:    {provider_type}")
    print(f"  Model:       {model}")
    print(f"  API Key:     {'[SET]' if api_key else '[needs env variable]'}")
    print(f"  File:        {config_file.resolve()}")
    print("=" * 55)
    print()
    print("Tip: Edit config.toml directly for advanced settings.")
    print()


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Configure model provider -- reads config.toml and tests connection",
    )
    parser.add_argument(
        "--path", "-p",
        default=None,
        help="Path to config.toml (auto-detected by default)",
    )
    parser.add_argument(
        "--test", "-t",
        action="store_true",
        help="Test API connection",
    )
    parser.add_argument(
        "--interactive", "-i",
        action="store_true",
        help="Interactive configuration wizard",
    )
    args = parser.parse_args()

    # Interactive wizard mode
    if args.interactive:
        interactive_wizard(args.path)
        return

    # Read and display config
    reader = ConfigReader(args.path)
    config = reader.read()

    show_config(config)

    # Create provider
    try:
        config_from_file, provider = load_provider(args.path)
        provider_name = provider.__class__.__name__
        print(f"  Provider instance: {provider_name}")
        print()
    except Exception as e:
        print(f"  [ERROR] Failed to create provider: {e}")
        print()

    # Test connection
    if args.test:
        print("Testing connection...")
        print()
        success = await test_connection(config)
        if success:
            print("[PASS] Configuration validated successfully!")
        else:
            print("[FAIL] Configuration validation encountered issues. Check errors above.")
            sys.exit(1)
    else:
        print("Tip: Use --test to test the API connection.")
        print()


if __name__ == "__main__":
    asyncio.run(main())