"""Enterprise Agent Platform public surface."""
from enterprise_agent_platform.contracts.commands import EffectGrantRequest
from enterprise_agent_platform.control.approvals import ApprovalDecisionService
from enterprise_agent_platform.control.context import RequestContext
from enterprise_agent_platform.control.effect_recovery import FailedEffectRecoveryService
from enterprise_agent_platform.control.service import ControlPlaneService
from enterprise_agent_platform.fastapi.app import create_agent_platform_app
from enterprise_agent_platform.fastapi.dependencies import AgentPlatformContainer
from enterprise_agent_platform.fastapi.router import create_router
from enterprise_agent_platform.integration.host import (
    AuthContextProvider,
    HostContextVerifier,
    HostPortError,
    PolicyContextProvider,
    ResolvedPolicyContext,
    ResolvedResource,
    ResourceResolver,
    VerifiedHostContext,
)
from enterprise_agent_platform.persistence import InMemoryPlatformStore, PlatformStore
from enterprise_agent_platform.reference.provider import ReferenceWorkflowHarness
from enterprise_agent_platform.security.capabilities import (
    CapabilityIssuer,
    CapabilityVerifier,
)
from enterprise_agent_platform.tools.connectors import Connector, CredentialBroker
from enterprise_agent_platform.tools.durable_effects import (
    DurableEffectExecutor,
    EffectCapabilityAuthorizer,
    EffectPayloadResolver,
    EffectReconciliationAuthorizer,
)


def create_in_memory_container(
    *,
    auth_context_provider: AuthContextProvider,
    resource_resolver: ResourceResolver,
    host_context_verifier: HostContextVerifier,
    policy_context_provider: PolicyContextProvider,
    store: PlatformStore | None = None,
) -> AgentPlatformContainer:
    store = store or InMemoryPlatformStore()
    return AgentPlatformContainer(
        store=store,
        control=ControlPlaneService(store),
        auth_context_provider=auth_context_provider,
        resource_resolver=resource_resolver,
        host_context_verifier=host_context_verifier,
        policy_context_provider=policy_context_provider,
    )


def create_app(container: AgentPlatformContainer):
    return create_agent_platform_app(container)


__all__ = [
    "AgentPlatformContainer",
    "ApprovalDecisionService",
    "AuthContextProvider",
    "CapabilityIssuer",
    "CapabilityVerifier",
    "Connector",
    "ControlPlaneService",
    "CredentialBroker",
    "DurableEffectExecutor",
    "EffectCapabilityAuthorizer",
    "EffectGrantRequest",
    "EffectPayloadResolver",
    "EffectReconciliationAuthorizer",
    "FailedEffectRecoveryService",
    "HostContextVerifier",
    "HostPortError",
    "InMemoryPlatformStore",
    "PlatformStore",
    "PolicyContextProvider",
    "ReferenceWorkflowHarness",
    "RequestContext",
    "ResolvedPolicyContext",
    "ResolvedResource",
    "ResourceResolver",
    "VerifiedHostContext",
    "create_app",
    "create_in_memory_container",
    "create_router",
]
