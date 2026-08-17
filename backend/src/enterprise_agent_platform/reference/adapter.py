"""Deterministic READ adapter and analysis output for the reference vertical."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

from enterprise_agent_platform.reference.dataset import (
    REFERENCE_DATASET_VERSION,
    REFERENCE_RESOURCE_REF,
    SYNTHETIC_CASES,
    SyntheticCase,
)
from enterprise_agent_platform.tools.connectors import (
    ConnectorCallContext,
    CredentialMaterial,
)


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _checksum(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _case_payload(case: SyntheticCase) -> dict[str, object]:
    return asdict(case)


@dataclass(frozen=True, slots=True)
class SyntheticReadResult:
    resource_ref: str
    dataset_version: str
    cases: tuple[SyntheticCase, ...]
    checksum: str

    @property
    def failed_count(self) -> int:
        return sum(case.outcome == "FAILED" for case in self.cases)

    def to_connector_output(self) -> dict[str, object]:
        return {
            "resource_ref": self.resource_ref,
            "dataset_version": self.dataset_version,
            "case_count": len(self.cases),
            "failed_count": self.failed_count,
            "checksum": self.checksum,
            "cases": [_case_payload(case) for case in self.cases],
        }


@dataclass(frozen=True, slots=True)
class EvidenceItem:
    case_id: str
    signal_code: str
    source_ref: str
    source_checksum: str


@dataclass(frozen=True, slots=True)
class DefectProposalDraft:
    action_ref: str
    tool_name: str
    tool_version: str
    connector_name: str
    canonical_target: str
    canonical_payload: dict[str, object]
    required_scope: str


@dataclass(frozen=True, slots=True)
class SyntheticAnalysis:
    source: SyntheticReadResult
    evidence: tuple[EvidenceItem, ...]
    failed_count: int
    report_bytes: bytes
    report_checksum: str
    defect: DefectProposalDraft


class SyntheticAnalysisAdapter:
    """Reference-only adapter whose output is reproducible byte for byte."""

    def read(
        self,
        resource_ref: str,
        *,
        max_items: int,
        suite: str | None = None,
    ) -> SyntheticReadResult:
        if resource_ref != REFERENCE_RESOURCE_REF or "://" in resource_ref:
            raise ValueError("resource ref is not the registered synthetic dataset")
        if type(max_items) is not int or not 1 <= max_items <= 1000:
            raise ValueError("max_items must be between 1 and 1000")
        selected = tuple(
            case for case in SYNTHETIC_CASES if suite is None or case.suite == suite
        )[:max_items]
        payload = _canonical([_case_payload(case) for case in selected])
        return SyntheticReadResult(
            resource_ref=resource_ref,
            dataset_version=REFERENCE_DATASET_VERSION,
            cases=selected,
            checksum=_checksum(payload),
        )

    def analyze(self, source: SyntheticReadResult) -> SyntheticAnalysis:
        if source.resource_ref != REFERENCE_RESOURCE_REF:
            raise ValueError("analysis input is not the reference dataset")
        evidence = tuple(
            EvidenceItem(
                case_id=case.case_id,
                signal_code=case.signal_code or "",
                source_ref=case.source_ref,
                source_checksum=_checksum(_canonical(_case_payload(case))),
            )
            for case in source.cases
            if case.outcome == "FAILED"
        )
        report_payload: dict[str, object] = {
            "schema_version": "synthetic-analysis-report/v1",
            "source": {
                "resource_ref": source.resource_ref,
                "dataset_version": source.dataset_version,
                "checksum": source.checksum,
            },
            "summary": {
                "case_count": len(source.cases),
                "failed_count": len(evidence),
            },
            "evidence": [asdict(item) for item in evidence],
        }
        report_bytes = _canonical(report_payload)
        report_checksum = _checksum(report_bytes)
        proposal = DefectProposalDraft(
            action_ref="defect.create",
            tool_name="defect.create",
            tool_version="v1",
            connector_name="reference-defects",
            canonical_target="project:reference",
            canonical_payload={
                "title": f"Investigate {len(evidence)} synthetic failure signals",
                "report_checksum": report_checksum,
                "source_checksum": source.checksum,
                "evidence_refs": [item.source_ref for item in evidence],
            },
            required_scope="defect:write",
        )
        return SyntheticAnalysis(
            source=source,
            evidence=evidence,
            failed_count=len(evidence),
            report_bytes=report_bytes,
            report_checksum=report_checksum,
            defect=proposal,
        )


class SyntheticReadConnector:
    """Connector-compatible READ boundary used through the real ToolGateway."""

    def __init__(self, adapter: SyntheticAnalysisAdapter) -> None:
        self._adapter = adapter
        self.call_count = 0

    async def invoke(
        self,
        context: ConnectorCallContext,
        operation: str,
        resource_ref: str,
        arguments: dict[str, object],
        credential: CredentialMaterial,
    ) -> dict[str, object]:
        del context
        if (
            operation != "synthetic.read"
            or credential.secret.get("reference_key") != "reference-only"
        ):
            raise ValueError("reference connector invocation is invalid")
        max_items = arguments.get("max_items")
        suite = arguments.get("suite")
        if not isinstance(max_items, int) or (suite is not None and not isinstance(suite, str)):
            raise ValueError("reference connector arguments are invalid")
        self.call_count += 1
        return self._adapter.read(
            resource_ref,
            max_items=max_items,
            suite=suite,
        ).to_connector_output()


__all__ = [
    "DefectProposalDraft",
    "EvidenceItem",
    "SyntheticAnalysis",
    "SyntheticAnalysisAdapter",
    "SyntheticReadConnector",
    "SyntheticReadResult",
]
