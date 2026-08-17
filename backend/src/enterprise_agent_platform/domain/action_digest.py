"""Canonical digest binding every approved external-action authority fact."""
from __future__ import annotations

import hashlib
import json


def compute_action_request_digest(
    *,
    action_ref: str,
    tool_name: str,
    tool_spec_version: str,
    tool_spec_digest: str,
    connector_name: str,
    required_scopes: tuple[str, ...],
    canonical_target: str,
    canonical_payload_digest: str,
    risk_class: str,
) -> str:
    if (
        not all(
            (
                action_ref,
                tool_name,
                tool_spec_version,
                tool_spec_digest,
                connector_name,
                canonical_target,
                canonical_payload_digest,
                risk_class,
            )
        )
        or not required_scopes
    ):
        raise ValueError("external-action authority facts are required")
    scopes = tuple(sorted(set(required_scopes)))
    if scopes != required_scopes:
        raise ValueError("required scopes must be sorted and unique")
    canonical = json.dumps(
        {
            "action_ref": action_ref,
            "canonical_payload_digest": canonical_payload_digest,
            "canonical_target": canonical_target,
            "connector_name": connector_name,
            "required_scopes": scopes,
            "risk_class": risk_class,
            "tool_name": tool_name,
            "tool_spec_digest": tool_spec_digest,
            "tool_spec_version": tool_spec_version,
        },
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def compute_effect_key(*, tenant_id: str, run_id: str, action_ref: str, request_digest: str) -> str:
    if not all((tenant_id, run_id, action_ref, request_digest)):
        raise ValueError("Effect identity facts are required")
    canonical = f"{tenant_id}\x00{run_id}\x00{action_ref}\x00{request_digest}"
    return f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"


__all__ = ["compute_action_request_digest", "compute_effect_key"]
