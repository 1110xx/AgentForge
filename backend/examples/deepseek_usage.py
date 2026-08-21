#!/usr/bin/env python3
"""
Example: Using DeepSeek Model Provider in the Enterprise Agent Platform

This example demonstrates how to:
1. Configure the platform to use DeepSeek v4 Flash
2. Create and manage sessions
3. Execute tasks and handle follow-up questions
4. Handle errors and fallback scenarios
"""
import asyncio
import os
from pathlib import Path

# Add the src directory to Python path
src_path = Path(__file__).parent.parent / "src"
os.sys.path.insert(0, str(src_path))

from enterprise_agent_platform.execution.session import SessionHandle
from enterprise_agent_platform.platform.config_loader import ConfigLoader
from enterprise_agent_platform.reference import DeepSeekModelSessionProvider


async def example_deepseek_integration():
    """Example of DeepSeek integration with proper error handling."""
    print("🚀 DeepSeek Integration Example")
    print("=" * 40)
    
    # Check if API key is available
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        print("⚠️  DEEPSEEK_API_KEY not found. Using demo mode.")
        await example_demo_mode()
        return
    
    try:
        # Configure with DeepSeek
        settings, model_provider = ConfigLoader.configure_with_deepseek()
        
        print(f"✅ Configured with DeepSeek")
        print(f"   Model: {settings.default_model}")
        print(f"   Base URL: {settings.deepseek_base_url}")
        
        # Example 1: Basic task execution
        await example_basic_task(model_provider)
        
        # Example 2: Follow-up questions
        await example_followup_questions(model_provider)
        
        # Example 3: Multiple sessions
        await example_multiple_sessions(model_provider)
        
    except Exception as e:
        print(f"❌ DeepSeek integration failed: {e}")
        print("🔄 Falling back to demo mode...")
        await example_demo_mode()


async def example_basic_task(model_provider: DeepSeekModelSessionProvider):
    """Example of basic task execution."""
    print("\n📝 Example 1: Basic Task Execution")
    print("-" * 35)
    
    run_id = "basic-task-001"
    intent = "Analyze the quarterly sales report and identify key trends"
    
    try:
        # Open session
        handle = await model_provider.open(
            run_id=run_id,
            intent=intent,
            resource_refs=("sales_q4_2023", "market_research"),
            host_context_ref=None
        )
        
        print(f"✅ Session opened: {handle.session_id}")
        
        # Execute task (placeholder - in real implementation this would do actual work)
        await model_provider.run_task(handle)
        print("✅ Task execution completed")
        
        # Close session
        await model_provider.close(handle)
        print("✅ Session closed")
        
    except Exception as e:
        print(f"❌ Basic task failed: {e}")
        raise


async def example_followup_questions(model_provider: DeepSeekModelSessionProvider):
    """Example of handling follow-up questions."""
    print("\n💬 Example 2: Follow-up Questions")
    print("-" * 35)
    
    run_id = "followup-demo-001"
    intent = "Create a marketing campaign for product launch"
    
    try:
        # Open session
        handle = await model_provider.open(
            run_id=run_id,
            intent=intent,
            resource_refs=("product_specs", "target_audience"),
            host_context_ref=None
        )
        
        # Execute main task
        await model_provider.run_task(handle)
        print("✅ Main task completed")
        
        # Ask follow-up questions
        questions = [
            "What's the budget allocation?",
            "How will we measure success?",
            "What are the risks involved?"
        ]
        
        for i, question in enumerate(questions, 1):
            print(f"\n   Q{i}: {question}")
            answer = await model_provider.followup(handle, question)
            print(f"   A{i}: {answer[:100]}...")
        
        # Close session
        await model_provider.close(handle)
        print("✅ Session closed")
        
    except Exception as e:
        print(f"❌ Follow-up demo failed: {e}")
        raise


async def example_multiple_sessions(model_provider: DeepSeekModelSessionProvider):
    """Example of managing multiple sessions."""
    print("\n🔄 Example 3: Multiple Sessions")
    print("-" * 35)
    
    sessions = []
    
    try:
        # Create multiple sessions
        for i in range(2):
            run_id = f"multi-session-{i+1}"
            intent = f"Process customer feedback batch {i+1}"
            
            handle = await model_provider.open(
                run_id=run_id,
                intent=intent,
                resource_refs=(f"feedback_batch_{i+1}",),
                host_context_ref=None
            )
            
            sessions.append((run_id, handle))
            print(f"✅ Session {i+1} opened: {handle.session_id}")
            
            # Execute task
            await model_provider.run_task(handle)
            
            # Ask one follow-up question
            answer = await model_provider.followup(handle, "What was the overall sentiment?")
            print(f"   Answer: {answer[:50]}...")
            
            # Keep session open for now
        
        # Close all sessions
        for run_id, handle in sessions:
            await model_provider.close(handle)
            print(f"✅ Session closed: {run_id}")
        
    except Exception as e:
        print(f"❌ Multi-session demo failed: {e}")
        # Ensure cleanup
        for _, handle in sessions:
            try:
                await model_provider.close(handle)
            except:
                pass
        raise


async def example_demo_mode():
    """Example using demo mode (no API key required)."""
    print("\n🎭 Example 4: Demo Mode")
    print("-" * 25)
    
    from enterprise_agent_platform.reference import ReferenceModelSessionProvider
    
    # Use reference provider for demo
    model_provider = ReferenceModelSessionProvider()
    
    run_id = "demo-run-001"
    intent = "Analyze synthetic test data"
    
    try:
        # Open session
        handle = await model_provider.open(
            run_id=run_id,
            intent=intent,
            resource_refs=("synthetic_data", "test_cases"),
            host_context_ref=None
        )
        
        print(f"✅ Demo session opened: {handle.session_id}")
        
        # Execute task
        await model_provider.run_task(handle)
        print("✅ Demo task completed")
        
        # Ask follow-up
        answer = await model_provider.followup(handle, "What was the result?")
        print(f"💬 Demo answer: {answer}")
        
        # Close session
        await model_provider.close(handle)
        print("✅ Demo session closed")
        
    except Exception as e:
        print(f"❌ Demo mode failed: {e}")
        raise


async def main():
    """Main function."""
    await example_deepseek_integration()
    
    print("\n🎉 Examples completed!")
    print("\n💡 Tips:")
    print("   • Set DEEPSEEK_API_KEY environment variable for production use")
    print("   • The platform automatically falls back to demo mode if no API key is available")
    print("   • All operations are asynchronous and use proper error handling")
    print("   • Sessions provide memory for follow-up questions within the same Run")


if __name__ == "__main__":
    asyncio.run(main())