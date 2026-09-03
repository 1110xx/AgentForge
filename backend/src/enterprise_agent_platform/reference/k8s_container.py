"""Kubernetes-compatible API container factory (Phase 3, L3 gate).

Target for ``AGENT_PLATFORM_CONTAINER_FACTORY`` on the deployed Control-Plane
API (see ``deploy/helm/values.yaml``):

* a **durable store shared with the K8s worker** — the worker process polls
  the same PostgreSQL for QUEUED runs and submits sandbox Jobs, and the Pod's
  runtime talks back to *this* API over HTTP (bootstrap → restore → heartbeat
  → model-call → turn/final checkpoints). Without a shared store the two
  processes would silently diverge.
* the reference integration adapters (auth / synthetic resources / host
  context / allow-all policy) so a disposable Kind gate can run the full
  lifecycle with zero external identity infrastructure.
* an in-memory run-session provider, which is what the API proxies model
  calls through (Internal ``/runtime/model-call``).

``AGENT_PLATFORM_STORE=memory`` opts into the in-memory store for local
smoke runs; anything else without ``AGENT_PLATFORM_DATABASE_URL`` fails
closed (mirrors ``execution/k8s_worker.create_worker_store``).
"""
from __future__ import annotations

import os

from enterprise_agent_platform import (
    AgentPlatformContainer,
    create_in_memory_container,
)
from enterprise_agent_platform.persistence import InMemoryPlatformStore
from enterprise_agent_platform.persistence.protocol import PlatformStore
from enterprise_agent_platform.reference.local_stack import (
    ReferenceAllowAllPolicy,
    ReferenceHostContextVerifier,
    ReferenceSyntheticResources,
)
from enterprise_agent_platform.reference.session import InMemoryRunSessionProvider


def create_store() -> PlatformStore:
    """Durable store for the deployed API (fails closed without a DB URL)."""
    database_url = os.environ.get("AGENT_PLATFORM_DATABASE_URL", "").strip()
    if database_url:
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        from enterprise_agent_platform.persistence.sqlalchemy_store import (
            SqlAlchemyPlatformStore,
        )

        engine = create_async_engine(database_url)
        return SqlAlchemyPlatformStore(async_sessionmaker(engine, expire_on_commit=False))

    if os.environ.get("AGENT_PLATFORM_STORE", "").strip() == "memory":
        return InMemoryPlatformStore()

    raise RuntimeError(
        "AGENT_PLATFORM_DATABASE_URL is required for the Kubernetes API "
        "(set AGENT_PLATFORM_STORE=memory only for local demo runs)"
    )


def _resolve_run_sessions(telemetry):
    """Env-driven run-session provider for the deployed API.

    Attempt-pod model calls are proxied here over the internal
    ``/runtime/model-call`` endpoint, so this provider is the single point
    that decides whether the cluster talks to a real model:

    * ``AGENT_PLATFORM_DEEPSEEK_API_KEY`` set → real ``DeepSeekModelSessionProvider``
      (chat completions against ``AGENT_PLATFORM_DEEPSEEK_BASE_URL``,
      default model from ``AGENT_PLATFORM_DEFAULT_MODEL`` = ``deepseek-chat``);
    * key absent → the deterministic in-memory stub (unchanged demo/gate
      behavior, zero external calls).

    The provider is wrapped with telemetry so RED model-call counters
    (open/run_task/followup/close emit ``agent_platform_model_calls_total``)
    reach the same OTLP collector as the worker and runner pods.
    """
    from enterprise_agent_platform.config import PlatformSettings
    from enterprise_agent_platform.platform.telemetry_service import (
        maybe_wrap_sessions,
    )

    settings = PlatformSettings()
    if settings.deepseek_api_key.strip():
        from enterprise_agent_platform.reference.deepseek_provider import (
            DeepSeekModelSessionProvider,
        )

        provider = DeepSeekModelSessionProvider(
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
            model=settings.default_model or "deepseek-chat",
        )
        # model_id must be a registered label value (see _METRIC_LABEL_REGISTRIES);
        # ``deepseek-chat`` / ``deepseek-reasoner`` are pre-registered.
        model_id = (
            settings.default_model if settings.default_model else "deepseek-chat"
        )
    else:
        provider = InMemoryRunSessionProvider()
        model_id = "deepseek-chat"
    return maybe_wrap_sessions(
        provider,
        telemetry,
        model_id=model_id,
    )


def create_container() -> AgentPlatformContainer:
    """Fresh API container: durable store + reference integrations + sessions.

    Telemetry is wired from the environment (OTLP export to the observability
    collector when ``AGENT_PLATFORM_OTLP_ENDPOINT`` is set) so the API's RED /
    run-lifecycle / model-call streams reach the same collector the worker and
    runner pods already use (round 20: the K8s API factory used to omit
    telemetry entirely — control-plane metrics silently never left the pod).
    """
    from enterprise_agent_platform.platform.telemetry_service import (
        create_telemetry_from_env,
    )
    from enterprise_agent_platform.security.oidc import create_auth_provider_from_env

    telemetry = create_telemetry_from_env(
        service_name="enterprise-agent-platform-api"
    )
    # Session provider carries RED model-call counters (open/run_task/
    # followup/close emit ``agent_platform_model_calls_total``); the K8s
    # factory used to hand the bare provider to the HTTP session surface, so
    # runner model calls never reached the OTLP collector (round 21 gate).
    # DeepSeek is selected when AGENT_PLATFORM_DEEPSEEK_API_KEY is present.
    run_sessions = _resolve_run_sessions(telemetry)

    # External /v1 auth (Phase 5 Step 1): ReferenceLocalAuth (static bearer)
    # stays the default so disposable gates run with zero identity infra;
    # AGENT_PLATFORM_AUTH_PROVIDER=oidc opts into OIDC (Auth0/Keycloak via
    # AGENT_PLATFORM_OIDC_* envs, security/oidc.py). The control plane glue
    # (authenticate_request/require_scope) is provider-agnostic.
    auth_context_provider = create_auth_provider_from_env()

    return create_in_memory_container(
        auth_context_provider=auth_context_provider,
        resource_resolver=ReferenceSyntheticResources(),
        host_context_verifier=ReferenceHostContextVerifier(),
        policy_context_provider=ReferenceAllowAllPolicy(),
        store=create_store(),
        run_sessions=run_sessions,
        telemetry=telemetry,
    )


__all__ = [
    "create_container",
    "create_store",
]