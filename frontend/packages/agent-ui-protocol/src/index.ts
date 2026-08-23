/**
 * Wire types + Zod validators for the AgentForge public contracts.
 *
 * This module mirrors, field for field, the backend contracts:
 *   backend/src/enterprise_agent_platform/contracts/{enums,models,events,commands,errors}.py
 *
 * Every schema is strict (extra keys rejected) to match Pydantic
 * `StrictModel(extra="forbid", frozen=True)`. Keep these schemas in lockstep
 * with the checked-in golden schemas in contracts/schemas/*.json and the
 * golden fixtures in contracts/fixtures/*.json (see scripts/generate-contracts.py).
 *
 * A2UI note: `a2ui-surface/v0.9.1` documents adopt the open A2UI surface
 * document model ({ component, props }), but the wire envelope, event stream
 * framing and the fixed public catalog are AgentForge's own contracts; the
 * reference @ag-ui/core runtime is deliberately NOT a dependency here.
 */
import { z } from "zod";

/* ------------------------------------------------------------------ */
/* Primitives                                                          */
/* ------------------------------------------------------------------ */

/** Pydantic datetime serializes as ISO-8601 with an explicit offset ("Z" or "+00:00"). */
export const IsoDateTime = z.string().datetime({ offset: true });
export const PositiveInt = z.number().int().min(1);
export const NonNegativeInt = z.number().int().min(0);

/* ------------------------------------------------------------------ */
/* JSON values (mirrors typing.JsonValue)                              */
/* ------------------------------------------------------------------ */

export type JsonPrimitive = boolean | number | string | null;
export interface JsonObject {
  [key: string]: JsonValue;
}
export type JsonValue = JsonPrimitive | JsonObject | JsonValue[];

export const JsonValueSchema: z.ZodType<JsonValue> = z.lazy(() =>
  z.union([
    z.string(),
    z.number(),
    z.boolean(),
    z.null(),
    z.array(JsonValueSchema),
    z.record(z.string(), JsonValueSchema),
  ]),
);

export const JsonObjectSchema: z.ZodType<JsonObject> = z.record(
  z.string(),
  JsonValueSchema,
);

/* ------------------------------------------------------------------ */
/* Enums (mirrors contracts/enums.py)                                  */
/* ------------------------------------------------------------------ */

export const RUN_STATES = [
  "QUEUED",
  "RUNNING",
  "WAITING_APPROVAL",
  "RECOVERING",
  "CANCEL_REQUESTED",
  "NEEDS_ATTENTION",
  "SUCCEEDED",
  "FAILED",
  "CANCELLED",
] as const;
export const RunState = z.enum(RUN_STATES);
export type RunState = z.infer<typeof RunState>;

export const STEP_STATES = [
  "PENDING",
  "ACTIVE",
  "WAITING_APPROVAL",
  "NEEDS_ATTENTION",
  "SUCCEEDED",
  "FAILED",
  "SKIPPED",
  "CANCELLED",
] as const;
export const StepState = z.enum(STEP_STATES);
export type StepState = z.infer<typeof StepState>;

export const EXECUTION_UNIT_STATES = [
  "IDLE",
  "DISPATCHABLE",
  "EXECUTING",
  "WAITING_APPROVAL",
  "NEEDS_ATTENTION",
  "RECOVERING",
  "SUCCEEDED",
  "FAILED",
  "CANCELLED",
] as const;
export const ExecutionUnitState = z.enum(EXECUTION_UNIT_STATES);
export type ExecutionUnitState = z.infer<typeof ExecutionUnitState>;

export const ATTEMPT_STATES = [
  "CREATED",
  "PROVISIONING",
  "CLAIMED",
  "RUNNING",
  "CHECKPOINTING",
  "CHECKPOINTED_FOR_APPROVAL",
  "SUCCEEDED",
  "FAILED",
  "LOST",
  "CANCELLED",
] as const;
export const AttemptState = z.enum(ATTEMPT_STATES);
export type AttemptState = z.infer<typeof AttemptState>;

export const TOOL_INVOCATION_STATES = [
  "CREATED",
  "AUTHORIZED",
  "EXECUTING",
  "UNKNOWN",
  "SUCCEEDED",
  "FAILED",
  "REJECTED",
  "CANCELLED",
] as const;
export const ToolInvocationState = z.enum(TOOL_INVOCATION_STATES);
export type ToolInvocationState = z.infer<typeof ToolInvocationState>;

export const APPROVAL_STATES = [
  "PENDING",
  "APPROVED",
  "REJECTED",
  "EXPIRED",
  "CANCELLED",
] as const;
export const ApprovalState = z.enum(APPROVAL_STATES);
export type ApprovalState = z.infer<typeof ApprovalState>;

export const EFFECT_STATES = [
  "PREPARED",
  "EXECUTING",
  "UNKNOWN",
  "SUCCEEDED",
  "FAILED",
] as const;
export const EffectState = z.enum(EFFECT_STATES);
export type EffectState = z.infer<typeof EffectState>;

export const TOOL_RISK_CLASSES = ["LOCAL", "READ", "WRITE"] as const;
export const ToolRiskClass = z.enum(TOOL_RISK_CLASSES);
export type ToolRiskClass = z.infer<typeof ToolRiskClass>;

export const RuntimeCapabilityAudience = z.enum(["tool-gateway"]);
export type RuntimeCapabilityAudience = z.infer<
  typeof RuntimeCapabilityAudience
>;

export const EffectCapabilityAudience = z.enum(["effect-executor"]);
export type EffectCapabilityAudience = z.infer<
  typeof EffectCapabilityAudience
>;

export const EVENT_TYPES = [
  "run.created",
  "run.status.changed",
  "attempt.lifecycle",
  "tool.invocation.recorded",
  "approval.decided",
  "effect.status.changed",
  "ui.surface.committed",
] as const;
export const EventType = z.enum(EVENT_TYPES);
export type EventType = z.infer<typeof EventType>;

/* ------------------------------------------------------------------ */
/* Event payloads (mirrors contracts/events.py)                        */
/* ------------------------------------------------------------------ */

export const RunCreatedPayload = z
  .object({
    kind: z.literal("run.created"),
    workflow_type: z.string(),
  })
  .strict();
export type RunCreatedPayload = z.infer<typeof RunCreatedPayload>;

export const RunStatusChangedPayload = z
  .object({
    kind: z.literal("run.status.changed"),
    previous: RunState,
    current: RunState,
  })
  .strict();
export type RunStatusChangedPayload = z.infer<typeof RunStatusChangedPayload>;

export const AttemptLifecyclePayload = z
  .object({
    kind: z.literal("attempt.lifecycle"),
    attempt_id: z.string(),
    status: AttemptState,
  })
  .strict();
export type AttemptLifecyclePayload = z.infer<typeof AttemptLifecyclePayload>;

export const ToolInvocationRecordedPayload = z
  .object({
    kind: z.literal("tool.invocation.recorded"),
    call_id: z.string(),
    status: ToolInvocationState,
  })
  .strict();
export type ToolInvocationRecordedPayload = z.infer<
  typeof ToolInvocationRecordedPayload
>;

export const ApprovalDecidedPayload = z
  .object({
    kind: z.literal("approval.decided"),
    approval_id: z.string(),
    status: ApprovalState,
  })
  .strict();
export type ApprovalDecidedPayload = z.infer<typeof ApprovalDecidedPayload>;

export const EffectStatusChangedPayload = z
  .object({
    kind: z.literal("effect.status.changed"),
    effect_id: z.string(),
    status: EffectState,
  })
  .strict();
export type EffectStatusChangedPayload = z.infer<
  typeof EffectStatusChangedPayload
>;

export const UiSurfaceCommittedPayload = z
  .object({
    kind: z.literal("ui.surface.committed"),
    surface_id: z.string(),
    revision: PositiveInt,
  })
  .strict();
export type UiSurfaceCommittedPayload = z.infer<
  typeof UiSurfaceCommittedPayload
>;

export const EventPayload = z.discriminatedUnion("kind", [
  RunCreatedPayload,
  RunStatusChangedPayload,
  AttemptLifecyclePayload,
  ToolInvocationRecordedPayload,
  ApprovalDecidedPayload,
  EffectStatusChangedPayload,
  UiSurfaceCommittedPayload,
]);
export type EventPayload = z.infer<typeof EventPayload>;

/**
 * event_type -> (required payload kind, required payload_schema).
 * Mirrors EVENT_PAYLOAD_CONTRACTS in contracts/events.py.
 */
export const EVENT_PAYLOAD_CONTRACTS = {
  "run.created": { kind: "run.created", payloadSchema: "run-created/v1" },
  "run.status.changed": {
    kind: "run.status.changed",
    payloadSchema: "run-status/v1",
  },
  "attempt.lifecycle": {
    kind: "attempt.lifecycle",
    payloadSchema: "attempt-lifecycle/v1",
  },
  "tool.invocation.recorded": {
    kind: "tool.invocation.recorded",
    payloadSchema: "tool-invocation/v1",
  },
  "approval.decided": { kind: "approval.decided", payloadSchema: "approval/v1" },
  "effect.status.changed": {
    kind: "effect.status.changed",
    payloadSchema: "effect/v1",
  },
  "ui.surface.committed": {
    kind: "ui.surface.committed",
    payloadSchema: "a2ui-surface/v0.9.1",
  },
} as const;

export const EnterpriseEventEnvelope = z
  .object({
    schema_version: z.literal("enterprise-event/v1"),
    event_id: z.string(),
    tenant_id: z.string(),
    run_id: z.string(),
    event_seq: PositiveInt,
    event_type: EventType,
    occurred_at: IsoDateTime,
    producer_service: z.string(),
    payload_schema: z.string(),
    payload: EventPayload,
    attempt_id: z.string().nullable().optional(),
    causation_event_id: z.string().nullable().optional(),
    trace_id: z.string().nullable().optional(),
  })
  .strict()
  .superRefine((event, ctx) => {
    const contract = EVENT_PAYLOAD_CONTRACTS[event.event_type];
    if (event.payload.kind !== contract.kind) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: `${event.event_type} requires payload kind ${contract.kind}`,
        path: ["payload"],
      });
    }
    if (event.payload_schema !== contract.payloadSchema) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: `${event.event_type} requires payload_schema ${contract.payloadSchema}`,
        path: ["payload_schema"],
      });
    }
  });
export type EnterpriseEventEnvelope = z.infer<
  typeof EnterpriseEventEnvelope
>;

/* ------------------------------------------------------------------ */
/* Capability claims (mirrors contracts/models.py)                     */
/* ------------------------------------------------------------------ */

export const RuntimeCapabilityClaims = z
  .object({
    schema_version: z.literal("runtime-capability/v1"),
    token_id: z.string(),
    tenant_id: z.string(),
    run_id: z.string(),
    execution_unit_id: z.string(),
    attempt_id: z.string(),
    generation: PositiveInt,
    audience: RuntimeCapabilityAudience,
    scopes: z.array(z.string()),
    expires_at: IsoDateTime,
  })
  .strict();
export type RuntimeCapabilityClaims = z.infer<typeof RuntimeCapabilityClaims>;

export const EffectCapabilityClaims = z
  .object({
    schema_version: z.literal("effect-capability/v1"),
    token_id: z.string(),
    tenant_id: z.string(),
    effect_id: z.string(),
    approval_id: z.string(),
    request_digest: z.string(),
    tool_name: z.string(),
    tool_version: z.string(),
    tool_spec_digest: z.string(),
    connector_name: z.string(),
    canonical_target: z.string(),
    scopes: z.array(z.string()),
    audience: EffectCapabilityAudience,
    expires_at: IsoDateTime,
  })
  .strict();
export type EffectCapabilityClaims = z.infer<typeof EffectCapabilityClaims>;

export const CapabilityClaims = z.union([
  RuntimeCapabilityClaims,
  EffectCapabilityClaims,
]);
export type CapabilityClaims = z.infer<typeof CapabilityClaims>;

/* ------------------------------------------------------------------ */
/* Tool invocation (mirrors contracts/models.py)                       */
/* ------------------------------------------------------------------ */

export const ToolInvocation = z
  .object({
    schema_version: z.literal("tool-invocation/v1"),
    call_id: z.string(),
    attempt_id: z.string(),
    generation: PositiveInt,
    tool_name: z.string(),
    tool_spec_version: z.string(),
    grant_id: z.string(),
    input_digest: z.string(),
    status: ToolInvocationState,
    risk_class: ToolRiskClass,
    resource_ref: z.string(),
    result_ref: z.string().nullable().optional(),
    started_at: IsoDateTime.nullable().optional(),
    ended_at: IsoDateTime.nullable().optional(),
  })
  .strict();
export type ToolInvocation = z.infer<typeof ToolInvocation>;

/* ------------------------------------------------------------------ */
/* Approval (mirrors contracts/models.py)                              */
/* ------------------------------------------------------------------ */

export const Approval = z
  .object({
    schema_version: z.literal("approval/v1"),
    approval_id: z.string(),
    run_id: z.string(),
    request_digest: z.string(),
    status: ApprovalState,
    version: PositiveInt,
    canonical_request_ref: z.string(),
    expires_at: IsoDateTime,
    decided_by: z.string().nullable().optional(),
  })
  .strict();
export type Approval = z.infer<typeof Approval>;

/* ------------------------------------------------------------------ */
/* A2UI surfaces (mirrors contracts/models.py)                         */
/* ------------------------------------------------------------------ */

/**
 * The declarative A2UI surface document: { component, props }.
 * The fixed public catalog (which component names are allowed, and what the
 * server validates) lives in agent-ui-catalog; the protocol layer only pins
 * the generic wire shape.
 */
export const SurfaceDocument = z
  .object({
    component: z.string(),
    props: JsonObjectSchema,
  })
  .strict();
export type SurfaceDocument = z.infer<typeof SurfaceDocument>;

export const UiSurface = z
  .object({
    schema_version: z.literal("a2ui-surface/v0.9.1"),
    surface_id: z.string(),
    run_id: z.string(),
    catalog_id: z.string(),
    revision: PositiveInt,
    source_event_seq: PositiveInt,
    document: JsonObjectSchema,
  })
  .strict();
export type UiSurface = z.infer<typeof UiSurface>;

export const SurfaceRevision = z
  .object({
    schema_version: z.literal("a2ui-surface-revision/v0.9.1"),
    surface_id: z.string(),
    run_id: z.string(),
    revision: PositiveInt,
    source_attempt_id: z.string(),
    source_event_seq: PositiveInt,
    document: JsonObjectSchema,
    checksum: z.string(),
  })
  .strict();
export type SurfaceRevision = z.infer<typeof SurfaceRevision>;

/* ------------------------------------------------------------------ */
/* Run view (mirrors contracts/models.py)                              */
/* ------------------------------------------------------------------ */

export const ExecutionUnitSummary = z
  .object({
    execution_unit_id: z.string(),
    role: z.string(),
    status: ExecutionUnitState,
    version: PositiveInt,
  })
  .strict();
export type ExecutionUnitSummary = z.infer<typeof ExecutionUnitSummary>;

export const AttemptSummary = z
  .object({
    attempt_id: z.string(),
    execution_unit_id: z.string(),
    step_id: z.string().nullable(),
    status: AttemptState,
    version: PositiveInt,
    started_at: IsoDateTime.nullable(),
    ended_at: IsoDateTime.nullable(),
  })
  .strict();
export type AttemptSummary = z.infer<typeof AttemptSummary>;

export const StepSummary = z
  .object({
    step_id: z.string(),
    name: z.string(),
    status: StepState,
    version: PositiveInt,
  })
  .strict();
export type StepSummary = z.infer<typeof StepSummary>;

export const ApprovalSummary = z
  .object({
    approval_id: z.string(),
    status: ApprovalState,
    version: PositiveInt,
  })
  .strict();
export type ApprovalSummary = z.infer<typeof ApprovalSummary>;

export const ArtifactSummary = z
  .object({
    artifact_id: z.string(),
    name: z.string(),
    media_type: z.string(),
    version: PositiveInt,
  })
  .strict();
export type ArtifactSummary = z.infer<typeof ArtifactSummary>;

export const SurfaceSummary = z
  .object({
    surface_id: z.string(),
    catalog_id: z.string(),
    revision: PositiveInt,
  })
  .strict();
export type SurfaceSummary = z.infer<typeof SurfaceSummary>;

export const RunView = z
  .object({
    run_id: z.string(),
    parent_run_id: z.string().nullable(),
    workflow_type: z.string(),
    intent: z.string(),
    status: RunState,
    status_reason: z.string().nullable(),
    version: PositiveInt,
    created_at: IsoDateTime,
    updated_at: IsoDateTime,
    ended_at: IsoDateTime.nullable(),
    execution_units: z.array(ExecutionUnitSummary),
    attempts: z.array(AttemptSummary),
    current_step: StepSummary.nullable().optional(),
    approvals: z.array(ApprovalSummary).default([]),
    artifacts: z.array(ArtifactSummary).default([]),
    surfaces: z.array(SurfaceSummary).default([]),
    watermark: NonNegativeInt,
  })
  .strict();
export type RunView = z.infer<typeof RunView>;

export const RunViewSnapshot = z
  .object({
    schema_version: z.literal("run-view-snapshot/v1"),
    run_id: z.string(),
    status: RunState,
    watermark: NonNegativeInt,
    view: RunView,
  })
  .strict();
export type RunViewSnapshot = z.infer<typeof RunViewSnapshot>;

/* ------------------------------------------------------------------ */
/* Run event page (mirrors contracts/models.py)                        */
/* ------------------------------------------------------------------ */

export const RunEventPage = z
  .object({
    schema_version: z.literal("run-event-page/v1"),
    run_id: z.string(),
    after_event_seq: NonNegativeInt,
    watermark: NonNegativeInt,
    retention_floor: NonNegativeInt,
    resync_required: z.literal(false),
    events: z.array(EnterpriseEventEnvelope),
  })
  .strict()
  .superRefine((page, ctx) => {
    const fail = (message: string): void => {
      ctx.addIssue({ code: z.ZodIssueCode.custom, message });
    };
    if (page.retention_floor > page.watermark) {
      fail("retention floor cannot exceed the page watermark");
    }
    if (page.after_event_seq > page.watermark) {
      fail("replay cursor cannot exceed the page watermark");
    }
    if (page.after_event_seq < page.retention_floor) {
      fail("cursor precedes retention floor; resync is required");
    }
    let previousSeq = page.after_event_seq;
    page.events.forEach((event, index) => {
      if (event.run_id !== page.run_id) {
        fail("replay page cannot contain events from another run");
      }
      if (event.event_seq <= previousSeq) {
        fail("replay event_seq must be strictly increasing after the cursor");
      }
      if (event.event_seq > page.watermark) {
        fail("replay event_seq cannot exceed the page watermark");
      }
      previousSeq = event.event_seq;
      void index;
    });
  });
export type RunEventPage = z.infer<typeof RunEventPage>;

/* ------------------------------------------------------------------ */
/* Artifact download authorization (mirrors contracts/models.py)       */
/* ------------------------------------------------------------------ */

export const ArtifactDownloadAuthorization = z
  .object({
    schema_version: z.literal("artifact-download-authorization/v1"),
    authorization_id: z.string(),
    artifact_id: z.string(),
    version: PositiveInt,
    download_url: z.string(),
    expires_at: IsoDateTime,
  })
  .strict();
export type ArtifactDownloadAuthorization = z.infer<
  typeof ArtifactDownloadAuthorization
>;

/* ------------------------------------------------------------------ */
/* Commands (mirrors contracts/commands.py)                            */
/* ------------------------------------------------------------------ */

const AUTHORITY_KEY_TOKENS = new Set([
  "audience",
  "auth",
  "authentication",
  "authorization",
  "bearer",
  "credential",
  "grant",
  "identity",
  "key",
  "oauth",
  "owner",
  "password",
  "passwd",
  "permission",
  "principal",
  "pwd",
  "role",
  "scope",
  "secret",
  "target",
  "tenant",
  "token",
  "tool",
  "uri",
  "url",
]);

const COMPACT_AUTHORITY_KEY_ALIASES = new Set([
  "accesskey",
  "accesssecret",
  "accesstoken",
  "apicredential",
  "apikey",
  "apisecret",
  "authkey",
  "authtoken",
  "bearertoken",
  "clientcredential",
  "clientcredentials",
  "clientkey",
  "clientsecret",
  "clienttoken",
  "encryptionkey",
  "passwordhash",
  "privatekey",
  "publickey",
  "refreshtoken",
  "secretkey",
  "signingkey",
]);

/** Mirrors `_key_tokens` in contracts/commands.py. */
function keyTokens(key: string): Set<string> {
  const snakeCase = key.replace(/([a-z0-9])([A-Z])/g, "$1_$2");
  const rawTokens = new Set(snakeCase.toLowerCase().match(/[a-z0-9]+/g) ?? []);
  for (const token of [...rawTokens]) {
    if (token.endsWith("s")) {
      rawTokens.add(token.slice(0, -1));
    }
  }
  return rawTokens;
}

/** Mirrors `_is_authority_key` in contracts/commands.py. */
function isAuthorityKey(key: string): boolean {
  const compactKey = key.toLowerCase().replace(/[^a-z0-9]/g, "");
  const compactCandidates = new Set([compactKey]);
  if (compactKey.endsWith("s")) {
    compactCandidates.add(compactKey.slice(0, -1));
  }
  for (const candidate of compactCandidates) {
    if (COMPACT_AUTHORITY_KEY_ALIASES.has(candidate)) {
      return true;
    }
  }
  for (const token of keyTokens(key)) {
    if (AUTHORITY_KEY_TOKENS.has(token)) {
      return true;
    }
  }
  return false;
}

/** Mirrors `_contains_authority_key` in contracts/commands.py. */
export function containsAuthorityKey(value: JsonValue): boolean {
  if (Array.isArray(value)) {
    return value.some(containsAuthorityKey);
  }
  if (typeof value === "object" && value !== null) {
    return Object.entries(value).some(
      ([key, nested]) => isAuthorityKey(key) || containsAuthorityKey(nested),
    );
  }
  return false;
}

export const SyntheticAnalysisDisplayOptions = z
  .object({
    theme: z.enum(["compact", "comfortable"]),
    labels: z.record(z.string(), z.string()).default({}),
  })
  .strict();
export type SyntheticAnalysisDisplayOptions = z.infer<
  typeof SyntheticAnalysisDisplayOptions
>;

export const SyntheticAnalysisOptions = z
  .object({
    display: z.array(SyntheticAnalysisDisplayOptions).default([]),
  })
  .strict();
export type SyntheticAnalysisOptions = z.infer<typeof SyntheticAnalysisOptions>;

export const SyntheticAnalysisParameters = z
  .object({
    analysis_mode: z
      .enum(["summary", "thorough", "failure-pattern"])
      .nullable()
      .optional(),
    max_items: z.number().int().min(1).max(1000).nullable().optional(),
    options: SyntheticAnalysisOptions.nullable().optional(),
  })
  .strict();
export type SyntheticAnalysisParameters = z.infer<
  typeof SyntheticAnalysisParameters
>;

export const CreateRunCommand = z
  .object({
    workflow_type: z.string(),
    intent: z.string(),
    resource_refs: z.array(z.string()).min(1),
    parameters: z.record(z.string(), JsonValueSchema).default({}),
    host_context_ref: z.string().nullable().optional(),
  })
  .strict()
  .superRefine((command, ctx) => {
    if (containsAuthorityKey(command.parameters)) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: "parameters cannot contain authority-shaped keys",
        path: ["parameters"],
      });
    }
    if (command.workflow_type === "synthetic-analysis") {
      const result = SyntheticAnalysisParameters.safeParse(command.parameters);
      if (!result.success) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          message: "parameters do not satisfy the synthetic-analysis schema",
          path: ["parameters"],
        });
      }
    } else if (Object.keys(command.parameters).length > 0) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: `${command.workflow_type} does not accept parameters`,
        path: ["parameters"],
      });
    }
  });
export type CreateRunCommand = z.infer<typeof CreateRunCommand>;

export const UiActionCommand = z
  .object({
    run_id: z.string(),
    surface_id: z.string(),
    surface_revision: PositiveInt,
    action_ref: z.string(),
    client_action_id: z.string(),
    displayed_digest: z.string().nullable().optional(),
    host_context_ref: z.string().nullable().optional(),
  })
  .strict();
export type UiActionCommand = z.infer<typeof UiActionCommand>;

/**
 * POST /v1/chat 请求体（自由对话入口，Phase 3.6 前端 Launcher）
 *
 * Mirrors backend ``ChatCommand`` in contracts/commands.py. The backend parses
 * the message into an intent and creates a Run through the same semantics as
 * POST /runs; ``workflow_hint`` is the explicit escape hatch, and the default
 * ``resource_refs`` mirrors the backend default (reference resolver prefix).
 */
export const ChatCommand = z
  .object({
    message: z.string().min(1).max(2_000),
    resource_refs: z.array(z.string()).min(1).default(["synthetic-case:demo"]),
    workflow_hint: z.string().nullable().optional(),
    host_context_ref: z.string().nullable().optional(),
  })
  .strict()
  .refine(
    (command) => command.message.trim().length > 0,
    { message: "message cannot be blank", path: ["message"] },
  );
export type ChatCommand = z.infer<typeof ChatCommand>;

/**
 * Call-site input type for ``ChatCommand``: ``resource_refs`` may be omitted
 * and is defaulted by the schema at parse time (mirrors the backend default).
 * ``z.infer`` exposes the *output* type, where the default has already been
 * applied, so this input type is what clients should accept in signatures.
 */
export type ChatCommandInput = z.input<typeof ChatCommand>;

/* ------------------------------------------------------------------ */
/* Followup (mirrors contracts/commands.py, contracts/models.py)       */
/* ------------------------------------------------------------------ */

/**
 * POST /v1/runs/{run_id}/followups 请求体
 *
 * Mirrors backend ``FollowupCommand`` in contracts/commands.py.
 * The backend silently enforces ``read_only=True`` — the frontend does
 * NOT send that field; the wire shape is kept in lockstep with the
 * Pydantic ``StrictModel(extra="forbid")``.
 */
export const FollowupCommand = z
  .object({
    run_id: z.string(),
    question: z.string().min(1).max(4_000),
    client_followup_id: z.string(),
  })
  .strict();
export type FollowupCommand = z.infer<typeof FollowupCommand>;

/**
 * POST /v1/runs/{run_id}/followups 响应体
 *
 * Mirrors backend ``FollowupAnswer`` in contracts/models.py.
 */
export const FollowupAnswer = z
  .object({
    schema_version: z.literal("followup-answer/v1"),
    run_id: z.string(),
    session_id: z.string(),
    question: z.string(),
    answer: z.string(),
  })
  .strict();
export type FollowupAnswer = z.infer<typeof FollowupAnswer>;

/** 历史记录条目 */
export const FollowupRecord = z
  .object({
    schema_version: z.literal("followup-record/v1"),
    run_id: z.string(),
    followup_seq: NonNegativeInt,
    question: z.string(),
    answer: z.string(),
    answered_at: IsoDateTime,
    client_followup_id: z.string(),
  })
  .strict();
export type FollowupRecord = z.infer<typeof FollowupRecord>;

/** GET /v1/runs/{run_id}/followups 响应 */
export const FollowupHistoryPage = z
  .object({
    schema_version: z.literal("followup-history-page/v1"),
    run_id: z.string(),
    total_count: NonNegativeInt,
    records: z.array(FollowupRecord),
  })
  .strict();
export type FollowupHistoryPage = z.infer<typeof FollowupHistoryPage>;

/** Future: NewTaskDraft (Phase 5 dual-route) */
export const NewTaskDraft = z
  .object({
    schema_version: z.literal("new-task-draft/v1"),
    run_id: z.string(),
    task_type: z.string(),
    params: JsonObjectSchema,
    summary: z.string(),
  })
  .strict();
export type NewTaskDraft = z.infer<typeof NewTaskDraft>;

/* ------------------------------------------------------------------ */
/* Errors (mirrors contracts/errors.py)                                */
/* ------------------------------------------------------------------ */

export const ApiErrorEnvelope = z
  .object({
    schema_version: z.literal("api-error/v1"),
    code: z.string(),
    message: z.string(),
    trace_id: z.string().nullable().optional(),
    retryable: z.boolean().default(false),
    details: z.record(z.string(), z.string()).default({}),
  })
  .strict();
export type ApiErrorEnvelope = z.infer<typeof ApiErrorEnvelope>;
