"""Tests for DeepSeek model provider integration."""
import pytest
from unittest.mock import AsyncMock, patch

from enterprise_agent_platform.execution.session import SessionHandle, SessionProviderError
from enterprise_agent_platform.platform.config_loader import ConfigLoader
from enterprise_agent_platform.reference.deepseek_provider import DeepSeekModelSessionProvider


class TestDeepSeekModelProvider:
    """Test cases for DeepSeek model provider."""
    
    @pytest.fixture
    def mock_api_key(self):
        """Mock API key for testing."""
        return "test-deepseek-api-key"
    
    @pytest.fixture
    def provider(self, mock_api_key):
        """Create a DeepSeek provider instance with the LLM call stubbed."""
        p = DeepSeekModelSessionProvider(api_key=mock_api_key)
        # Cap all LLM calls to a canned answer so unit tests never hit the network.
        p._call_api = AsyncMock(return_value="mocked answer")  # type: ignore[method-assign]
        return p
    
    @pytest.mark.asyncio
    async def test_open_session(self, provider):
        """Test opening a new session."""
        run_id = "test-run-001"
        intent = "Test task intent"
        
        handle = await provider.open(
            run_id=run_id,
            intent=intent,
            resource_refs=("test_resource",),
            host_context_ref=None
        )
        
        assert isinstance(handle, SessionHandle)
        assert handle.run_id == run_id
        assert handle.session_id == f"session:{run_id}"
        
        # Verify session was created
        assert handle.session_id in provider._sessions
        assert provider._sessions[handle.session_id].run_id == run_id
        assert provider._sessions[handle.session_id].intent == intent
    
    @pytest.mark.asyncio
    async def test_open_duplicate_session(self, provider):
        """Test opening a duplicate session raises error."""
        run_id = "test-run-duplicate"
        intent = "Test intent"
        
        # Open first session
        await provider.open(
            run_id=run_id,
            intent=intent,
            resource_refs=(),
            host_context_ref=None
        )
        
        # Try to open duplicate session
        with pytest.raises(SessionProviderError) as exc_info:
            await provider.open(
                run_id=run_id,
                intent="Different intent",
                resource_refs=(),
                host_context_ref=None
            )
        
        assert exc_info.value.code == "SESSION_ALREADY_OPEN"
    
    @pytest.mark.asyncio
    async def test_followup_before_task(self, provider):
        """Test followup before task execution."""
        run_id = "test-followup-early"
        intent = "Test intent"
        
        handle = await provider.open(
            run_id=run_id,
            intent=intent,
            resource_refs=(),
            host_context_ref=None
        )
        
        # Followup should work even before task execution
        answer = await provider.followup(handle, "What's the weather like?")
        assert isinstance(answer, str)
        assert len(answer) > 0
    
    @pytest.mark.asyncio
    async def test_close_session(self, provider):
        """Test closing a session."""
        run_id = "test-close-session"
        intent = "Test intent"
        
        handle = await provider.open(
            run_id=run_id,
            intent=intent,
            resource_refs=(),
            host_context_ref=None
        )
        
        # Close session
        await provider.close(handle)
        
        # Verify session is closed
        assert handle.session_id in provider._closed
        assert handle.session_id in provider._sessions
        
        # Try to use closed session
        with pytest.raises(SessionProviderError) as exc_info:
            await provider.followup(handle, "Test message")
        
        assert exc_info.value.code == "SESSION_CLOSED"
    
    @pytest.mark.asyncio
    async def test_followup_read_only(self, provider):
        """Test that followup is read-only."""
        run_id = "test-read-only"
        intent = "Test intent"
        
        handle = await provider.open(
            run_id=run_id,
            intent=intent,
            resource_refs=(),
            host_context_ref=None
        )
        
        # Test with read_only=True (default)
        answer1 = await provider.followup(handle, "Question 1", read_only=True)
        answer2 = await provider.followup(handle, "Question 2", read_only=True)
        
        assert isinstance(answer1, str)
        assert isinstance(answer2, str)
        assert len(answer1) > 0
        assert len(answer2) > 0
        
        # Session should still be open
        assert handle.session_id not in provider._closed
        
        await provider.close(handle)
    
    @pytest.mark.asyncio
    async def test_config_loader_with_api_key(self, mock_api_key):
        """Test config loader with API key (env-only; config.toml bypassed)."""
        from enterprise_agent_platform.platform.config_reader import ConfigReader

        with patch.object(ConfigReader, "_resolve_path", return_value=None), patch.dict(
            "os.environ", {"DEEPSEEK_API_KEY": mock_api_key}
        ):
            settings, provider = ConfigLoader.configure_with_deepseek()
            assert isinstance(provider, DeepSeekModelSessionProvider)
            assert settings.resolve_api_key() == mock_api_key
            assert provider.api_key == mock_api_key
    
    @pytest.mark.asyncio
    async def test_config_loader_without_api_key(self):
        """Test config loader without API key falls back to demo."""
        with patch('os.environ', {}):
            settings, provider = ConfigLoader.create_demo_config()
            
            # Should use reference provider in demo mode
            from enterprise_agent_platform.reference import ReferenceModelSessionProvider
            assert isinstance(provider, ReferenceModelSessionProvider)
    
    @pytest.mark.asyncio
    async def test_api_error_handling(self, provider):
        """Test API error handling."""
        # Mock the LLM call to raise an error
        with patch.object(provider, '_call_api', side_effect=Exception("API Error")):
            run_id = "test-api-error"
            intent = "Test intent"
            
            handle = await provider.open(
                run_id=run_id,
                intent=intent,
                resource_refs=(),
                host_context_ref=None
            )
            
            # Should raise SessionProviderError
            with pytest.raises(SessionProviderError) as exc_info:
                await provider.followup(handle, "Test question")
            
            assert exc_info.value.code == "FOLLOWUP_FAILED"
            assert "API Error" in str(exc_info.value)
    
    @pytest.mark.asyncio
    async def test_session_context_maintenance(self, provider):
        """Test that session context is maintained across followups."""
        run_id = "test-context-maintenance"
        intent = "Analyze customer feedback"
        
        handle = await provider.open(
            run_id=run_id,
            intent=intent,
            resource_refs=("customer_feedback",),
            host_context_ref=None
        )
        
        # Multiple followups should maintain context
        questions = [
            "What are the main complaints?",
            "What are the positive comments?",
            "What trends do you see?"
        ]
        
        for question in questions:
            answer = await provider.followup(handle, question)
            assert isinstance(answer, str)
            assert len(answer) > 0
        
        # Verify all messages are stored
        session = provider._sessions[handle.session_id]
        assert len(session.messages) > len(questions)  # Includes system message
        
        await provider.close(handle)
    
    @pytest.mark.asyncio
    async def test_concurrent_sessions(self, provider):
        """Test handling multiple concurrent sessions."""
        handles = []
        
        # Create multiple sessions
        for i in range(3):
            run_id = f"concurrent-{i}"
            intent = f"Task {i}"
            
            handle = await provider.open(
                run_id=run_id,
                intent=intent,
                resource_refs=(f"resource-{i}",),
                host_context_ref=None
            )
            
            handles.append(handle)
            
            # Add a followup to each session
            answer = await provider.followup(handle, f"Question for task {i}")
            assert isinstance(answer, str)
        
        # All sessions should be independent
        for handle in handles:
            assert handle.session_id in provider._sessions
            assert handle.session_id not in provider._closed
        
        # Close all sessions
        for handle in handles:
            await provider.close(handle)
        
        # Verify all sessions are closed
        for handle in handles:
            assert handle.session_id in provider._closed


class TestDeepseekProviderIntegration:
    """Integration tests for DeepSeek provider with the platform."""
    
    @pytest.mark.asyncio
    async def test_platform_container_integration(self):
        """Test that DeepSeek provider integrates with platform container."""
        from enterprise_agent_platform.fastapi.dependencies import AgentPlatformContainer
        from enterprise_agent_platform.platform.model_provider_config import create_model_provider
        
        # Create provider
        provider = create_model_provider()
        
        # Create container with provider
        container = AgentPlatformContainer(
            store=AsyncMock(),  # Mock store
            control=AsyncMock(),  # Mock control service
            auth_context_provider=AsyncMock(),  # Mock auth provider
            resource_resolver=AsyncMock(),  # Mock resource resolver
            host_context_verifier=AsyncMock(),  # Mock host verifier
            policy_context_provider=AsyncMock(),  # Mock policy provider
            run_sessions=provider,
            followups=AsyncMock()  # Mock followup handler
        )
        
        # Verify container has the provider
        assert container.run_sessions is provider
        assert container.followups is not None
    
    @pytest.mark.asyncio
    async def test_provider_lifecycle(self):
        """Test complete provider lifecycle."""
        api_key = "test-key"
        provider = DeepSeekModelSessionProvider(api_key=api_key)
        # Stub the LLM call so the lifecycle test never hits the network
        provider._call_api = AsyncMock(return_value="mocked answer")  # type: ignore[method-assign]

        try:
            # Open session
            handle = await provider.open(
                run_id="lifecycle-test",
                intent="Test lifecycle",
                resource_refs=(),
                host_context_ref=None
            )
            
            # Use session
            await provider.run_task(handle)
            await provider.followup(handle, "Test question")
            
            # Close session
            await provider.close(handle)
            
            # Verify cleanup
            assert handle.session_id in provider._closed
            
        finally:
            # Ensure cleanup (client may be None when the LLM call is stubbed)
            if provider._client is not None:
                await provider._client.aclose()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])