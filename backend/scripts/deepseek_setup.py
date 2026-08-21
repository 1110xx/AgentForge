#!/usr/bin/env python3
"""
Setup script for DeepSeek model provider integration.

This script demonstrates how to configure the platform to use DeepSeek v4 Flash
as the default model provider.
"""
import asyncio
import os
import sys
from pathlib import Path

# Add the src directory to Python path
src_path = Path(__file__).parent.parent / "src"
sys.path.insert(0, str(src_path))

from enterprise_agent_platform.platform.config_reader import ConfigReader
from enterprise_agent_platform.platform.provider_factory import ProviderFactory


async def test_deepseek_provider():
    """Test the DeepSeek model provider with a simple conversation."""
    print("[TEST] Testing DeepSeek Model Provider Integration")
    print("=" * 50)
    
    try:
        # Use config.toml approach
        reader = ConfigReader()
        config = reader.read()
        factory = ProviderFactory(config)
        model_provider = factory.create_primary()
        
        print(f"[OK] Configuration loaded successfully")
        print(f"  - Model: {config.provider.model}")
        print(f"  - Provider: {config.resolve_provider_type()}")
        
        # Test the provider
        run_id = "test-run-001"
        intent = "Analyze the latest sales data and identify trends"
        
        print()
        print(f"[TEST] Testing task execution...")
        print(f"  Run ID: {run_id}")
        print(f"  Intent: {intent}")
        
        async with model_provider:
            # Open session
            handle = await model_provider.open(
                run_id=run_id,
                intent=intent,
                resource_refs=("sales_data_v2", "market_analysis"),
                host_context_ref=None
            )
            
            print(f"[OK] Session opened: {handle.session_id}")
            
            # Run task
            await model_provider.run_task(handle)
            print("[OK] Task execution completed")
            
            # Test follow-up questions
            followup_questions = [
                "What were the key findings?",
                "Can you explain the methodology?",
                "What recommendations do you have?"
            ]
            
            print()
            print("[TEST] Testing follow-up questions...")
            for i, question in enumerate(followup_questions, 1):
                print(f"  Q{i}: {question}")
                answer = await model_provider.followup(handle, question)
                print(f"  A{i}: {answer[:80]}...")
            
            # Close session
            await model_provider.close(handle)
            print("[OK] Session closed")
        
        print()
        print("[PASS] DeepSeek provider integration test completed successfully!")
        
    except ValueError as e:
        print(f"[FAIL] Configuration error: {e}")
        print()
        print("[INFO] To fix this issue:")
        print("  1. Set your API key in config.toml or environment variable:")
        print("     DEEPSEEK_API_KEY='your-api-key-here'")
        print("  2. Or use demo mode (set provider.type = 'reference' in config.toml)")
        return False
    except Exception as e:
        print(f"[FAIL] Test failed: {e}")
        import traceback
        traceback.print_exc()
        return False
    
    return True


async def test_demo_mode():
    """Test the platform in demo mode (no API key required)."""
    print()
    print("[TEST] Testing Demo Mode (No API Key Required)")
    print("=" * 50)
    
    try:
        from enterprise_agent_platform.reference import ReferenceModelSessionProvider
        
        model_provider = ReferenceModelSessionProvider()
        
        run_id = "demo-run-001"
        intent = "Analyze synthetic test data"
        
        print(f"[TEST] Demo task:")
        print(f"  Run ID: {run_id}")
        print(f"  Intent: {intent}")
        
        async with model_provider:
            handle = await model_provider.open(
                run_id=run_id,
                intent=intent,
                resource_refs=("synthetic_data", "test_cases"),
                host_context_ref=None
            )
            
            print(f"[OK] Demo session opened: {handle.session_id}")
            
            await model_provider.run_task(handle)
            print("[OK] Demo task completed")
            
            answer = await model_provider.followup(handle, "What was the result?")
            print(f"[ANSWER] Demo follow-up answer: {answer}")
            
            await model_provider.close(handle)
            print("[OK] Demo session closed")
        
        print()
        print("[PASS] Demo mode test completed successfully!")
        return True
        
    except Exception as e:
        print(f"[FAIL] Demo mode test failed: {e}")
        return False


async def main():
    """Main function to test DeepSeek integration."""
    print("=" * 60)
    print("  DeepSeek Model Provider Setup Script")
    print("=" * 60)
    
    # Check config.toml or environment for API key
    reader = ConfigReader()
    config = reader.read()
    api_key = config.resolve_api_key()
    
    if api_key:
        print("[OK] API key found (from config.toml or environment)")
        success = await test_deepseek_provider()
    else:
        print("[INFO] No API key found")
        print("[INFO] Running in demo mode instead...")
        success = await test_demo_mode()
    
    if success:
        print()
        print("[PASS] All tests passed!")
        print()
        print("[INFO] Next steps:")
        print("  1. Set DEEPSEEK_API_KEY environment variable for production use")
        print("  2. Update the FastAPI app to use the configured provider")
        print("  3. Test the complete workflow with the frontend")
    else:
        print()
        print("[FAIL] Some tests failed. Check the output above for details.")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())