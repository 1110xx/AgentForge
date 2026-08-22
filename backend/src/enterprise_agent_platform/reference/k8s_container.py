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
    ReferenceLocalAuth,
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


def create_container() -> AgentPlatformContainer:
    """Fresh API container: durable store + reference integrations + sessions."""
    return create_in_memory_container(
        auth_context_provider=ReferenceLocalAuth(),
        resource_resolver=ReferenceSyntheticResources(),
        host_context_verifier=ReferenceHostContextVerifier(),
        policy_context_provider=ReferenceAllowAllPolicy(),
        store=create_store(),
        run_sessions=InMemoryRunSessionProvider(),
    )


__all__ = [
    "create_container",
    "create_store",
]