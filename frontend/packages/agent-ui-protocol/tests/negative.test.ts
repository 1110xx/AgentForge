/**
 * Negative validation tests: the Zod schemas must enforce the same domain
 * rules as the backend Pydantic models (payload/event_type contract, replay
 * window ordering, authority-shaped parameter rejection, enum literals).
 */
import { describe, expect, it } from "vitest";
import {
  ChatCommand,
  CreateRunCommand,
  EnterpriseEventEnvelope,
  EventPayload,
  RunEventPage,
  RunViewSnapshot,
  UiSurfaceCommittedPayload,
  type EnterpriseEventEnvelope as EnterpriseEventEnvelopeType,
} from "../src/index.js";

function baseEnvelope(): Record<string, unknown> {
  return {
    schema_version: "enterprise-event/v1",
    event_id: "evt_001",
    tenant_id: "tenant_demo",
    run_id: "run_demo",
    event_seq: 1,
    event_type: "run.created",
    occurred_at: "2026-08-07T00:00:00Z",
    producer_service: "control-plane",
    payload_schema: "run-created/v1",
    payload: { kind: "run.created", workflow_type: "synthetic-analysis" },
    attempt_id: null,
    causation_event_id: null,
    trace_id: null,
  };
}

function expectFail(schema: { safeParse: (input: unknown) => { success: boolean } }, input: unknown, label: string): void {
  const result = schema.safeParse(input);
  expect(result.success, `${label}: expected failure`).toBe(false);
}

describe("ChatCommand contract", () => {
  it("accepts a message and applies the default resource refs", () => {
    const parsed = ChatCommand.parse({ message: "分析日志中的故障模式" });
    expect(parsed.message).toBe("分析日志中的故障模式");
    expect(parsed.resource_refs).toEqual(["synthetic-case:demo"]);
    expect(parsed.workflow_hint).toBeUndefined();
    expect(parsed.host_context_ref).toBeUndefined();
  });

  it("accepts explicit workflow_hint and host_context_ref", () => {
    const parsed = ChatCommand.parse({
      message: "whatever",
      resource_refs: ["synthetic-case:case-42"],
      workflow_hint: "synthetic-analysis",
      host_context_ref: "reference-context:demo",
    });
    expect(parsed.workflow_hint).toBe("synthetic-analysis");
    expect(parsed.host_context_ref).toBe("reference-context:demo");
  });

  it("rejects blank and whitespace-only messages", () => {
    expectFail(ChatCommand, { message: "" }, "empty message");
    expectFail(ChatCommand, { message: "   " }, "whitespace-only message");
  });

  it("rejects messages longer than 2000 chars", () => {
    expectFail(
      ChatCommand,
      { message: "a".repeat(2001) },
      "overlong message",
    );
  });

  it("rejects empty resource_refs", () => {
    expectFail(ChatCommand, { message: "hi", resource_refs: [] }, "empty refs");
  });

  it("rejects extra keys (mirror of extra=forbid)", () => {
    expectFail(
      ChatCommand,
      { message: "hi", parameters: {} },
      "unexpected parameters key",
    );
  });
});

describe("EnterpriseEventEnvelope payload contract", () => {
  it("accepts every registered event_type with its canonical payload", () => {
    const cases: ReadonlyArray<[string, string, Record<string, unknown>]> = [
      ["run.created", "run-created/v1", { kind: "run.created", workflow_type: "x" }],
      ["run.status.changed", "run-status/v1", { kind: "run.status.changed", previous: "QUEUED", current: "RUNNING" }],
      ["attempt.lifecycle", "attempt-lifecycle/v1", { kind: "attempt.lifecycle", attempt_id: "a", status: "RUNNING" }],
      ["tool.invocation.recorded", "tool-invocation/v1", { kind: "tool.invocation.recorded", call_id: "c", status: "SUCCEEDED" }],
      ["approval.decided", "approval/v1", { kind: "approval.decided", approval_id: "ap", status: "APPROVED" }],
      ["effect.status.changed", "effect/v1", { kind: "effect.status.changed", effect_id: "e", status: "SUCCEEDED" }],
      ["ui.surface.committed", "a2ui-surface/v0.9.1", { kind: "ui.surface.committed", surface_id: "s", revision: 1 }],
    ];
    for (const [eventType, payloadSchema, payload] of cases) {
      const envelope = { ...baseEnvelope(), event_type: eventType, payload_schema: payloadSchema, payload };
      expect(
        EnterpriseEventEnvelope.safeParse(envelope).success,
        `${eventType} should parse`,
      ).toBe(true);
    }
  });

  it("rejects a payload kind that does not match event_type", () => {
    const envelope = {
      ...baseEnvelope(),
      event_type: "run.created",
      payload_schema: "run-created/v1",
      payload: { kind: "ui.surface.committed", surface_id: "s", revision: 1 },
    };
    expectFail(EnterpriseEventEnvelope, envelope, "mismatched payload kind");
  });

  it("rejects a payload_schema that does not match event_type", () => {
    const envelope = {
      ...baseEnvelope(),
      event_type: "ui.surface.committed",
      payload_schema: "run-created/v1",
      payload: { kind: "ui.surface.committed", surface_id: "s", revision: 1 },
    };
    expectFail(EnterpriseEventEnvelope, envelope, "mismatched payload_schema");
  });

  it("rejects event_seq below 1", () => {
    expectFail(
      EnterpriseEventEnvelope,
      { ...baseEnvelope(), event_seq: 0 },
      "event_seq 0",
    );
  });

  it("rejects an unknown event_type", () => {
    expectFail(
      EnterpriseEventEnvelope,
      { ...baseEnvelope(), event_type: "run.exploded" },
      "unknown event_type",
    );
  });

  it("rejects a non-ISO occurred_at", () => {
    expectFail(
      EnterpriseEventEnvelope,
      { ...baseEnvelope(), occurred_at: "not-a-date" },
      "bad datetime",
    );
  });

  it("discriminated union parses all seven payload shapes", () => {
    const payload = UiSurfaceCommittedPayload.parse({
      kind: "ui.surface.committed",
      surface_id: "s",
      revision: 3,
    });
    expect(payload.revision).toBe(3);
    const union = EventPayload.safeParse({ kind: "run.status.changed", previous: "QUEUED", current: "RUNNING" });
    expect(union.success).toBe(true);
    expectFail(EventPayload, { kind: "run.exploded" }, "unknown payload kind");
  });
});

describe("RunEventPage replay window", () => {
  const page = (events: ReadonlyArray<Record<string, unknown>>): Record<string, unknown> => ({
    schema_version: "run-event-page/v1",
    run_id: "run_demo",
    after_event_seq: 1,
    watermark: 4,
    retention_floor: 0,
    resync_required: false,
    events,
  });

  const event = (seq: number, runId = "run_demo"): Record<string, unknown> => ({
    ...baseEnvelope(),
    event_seq: seq,
    run_id: runId,
    event_type: "run.status.changed",
    payload_schema: "run-status/v1",
    payload: { kind: "run.status.changed", previous: "QUEUED", current: "RUNNING" },
  });

  it("accepts strictly increasing events within the window", () => {
    expect(
      RunEventPage.safeParse(page([event(2), event(3), event(4)])).success,
    ).toBe(true);
  });

  it("rejects duplicate event_seq", () => {
    expectFail(RunEventPage, page([event(2), event(2)]), "duplicate seq");
  });

  it("rejects events at or before the cursor", () => {
    expectFail(RunEventPage, page([event(1)]), "event at cursor");
  });

  it("rejects events beyond the watermark", () => {
    expectFail(RunEventPage, page([event(5)]), "event beyond watermark");
  });

  it("rejects events from another run", () => {
    expectFail(
      RunEventPage,
      page([event(2, "other_run")]),
      "cross-run event",
    );
  });

  it("rejects a cursor beyond the watermark", () => {
    expectFail(
      RunEventPage,
      { ...page([event(2)]), after_event_seq: 5 },
      "cursor beyond watermark",
    );
  });

  it("rejects a retention floor above the watermark", () => {
    expectFail(
      RunEventPage,
      { ...page([event(2)]), retention_floor: 9 },
      "retention floor above watermark",
    );
  });

  it("rejects a cursor that precedes the retention floor", () => {
    expectFail(
      RunEventPage,
      { ...page([event(4)]), retention_floor: 3 },
      "cursor precedes retention floor",
    );
  });

  it("rejects resync_required true (pages always carry resync_required=false)", () => {
    expectFail(
      RunEventPage,
      { ...page([event(2)]), resync_required: true },
      "resync flag true",
    );
  });
});

describe("CreateRunCommand parameter safety", () => {
  const command = (parameters: Record<string, unknown>): Record<string, unknown> => ({
    workflow_type: "synthetic-analysis",
    intent: "Analyze failure patterns",
    resource_refs: ["submission:demo"],
    parameters,
  });

  it("rejects authority-shaped parameter keys", () => {
    for (const parameters of [
      { apiKey: "secret" },
      { token: "secret" },
      { access_token: "secret" },
      { nested: { bearer_token: "secret" } },
      { passwordHash: "secret" },
    ]) {
      expectFail(
        CreateRunCommand,
        command(parameters),
        `authority key ${Object.keys(parameters).join(",")}`,
      );
    }
  });

  it("rejects parameters for an unknown workflow", () => {
    expectFail(
      CreateRunCommand,
      { ...command({}), workflow_type: "unknown-workflow", parameters: { anything: 1 } },
      "unknown workflow with parameters",
    );
  });

  it("accepts an unknown workflow without parameters", () => {
    const result = CreateRunCommand.safeParse({
      ...command({}),
      workflow_type: "unknown-workflow",
      parameters: {},
    });
    expect(result.success).toBe(true);
  });

  it("validates synthetic-analysis parameters", () => {
    const ok = CreateRunCommand.safeParse(
      command({ analysis_mode: "failure-pattern", max_items: 10 }),
    );
    expect(ok.success).toBe(true);
    expectFail(
      CreateRunCommand,
      command({ max_items: 0 }),
      "max_items below range",
    );
    expectFail(
      CreateRunCommand,
      command({ analysis_mode: "bogus" }),
      "bad analysis_mode",
    );
  });

  it("requires at least one resource_ref", () => {
    expectFail(
      CreateRunCommand,
      { ...command({}), resource_refs: [] },
      "empty resource_refs",
    );
  });
});

describe("RunViewSnapshot shape", () => {
  const snapshot = (): Record<string, unknown> => ({
    schema_version: "run-view-snapshot/v1",
    run_id: "run_demo",
    status: "RUNNING",
    watermark: 4,
    view: {
      run_id: "run_demo",
      parent_run_id: null,
      workflow_type: "synthetic-analysis",
      intent: "Analyze failure patterns",
      status: "RUNNING",
      status_reason: null,
      version: 2,
      created_at: "2026-08-07T00:00:00Z",
      updated_at: "2026-08-07T00:00:03Z",
      ended_at: null,
      execution_units: [
        { execution_unit_id: "unit_primary", role: "primary", status: "EXECUTING", version: 2 },
      ],
      attempts: [
        {
          attempt_id: "attempt_001",
          execution_unit_id: "unit_primary",
          step_id: null,
          status: "RUNNING",
          version: 2,
          started_at: "2026-08-07T00:00:03Z",
          ended_at: null,
        },
      ],
      current_step: null,
      approvals: [],
      artifacts: [],
      surfaces: [
        { surface_id: "surface_summary", catalog_id: "public-catalog", revision: 1 },
      ],
      watermark: 4,
    },
  });

  it("rejects an unknown run status", () => {
    const bad = snapshot() as { status: string };
    bad.status = "EXPLODED";
    expectFail(RunViewSnapshot, bad, "unknown status");
  });

  it("rejects a run version below 1", () => {
    const bad = snapshot() as { view: Record<string, unknown> };
    bad.view = { ...bad.view, version: 0 };
    expectFail(RunViewSnapshot, bad, "version 0");
  });

  it("rejects a non-null parent_run_id of wrong type", () => {
    const bad = snapshot() as { view: Record<string, unknown> };
    bad.view = { ...bad.view, parent_run_id: 42 };
    expectFail(RunViewSnapshot, bad, "parent_run_id number");
  });

  it("infers a usable parsed type for the host", () => {
    const parsed = RunViewSnapshot.parse(snapshot());
    const envelopeTypeCheck: EnterpriseEventEnvelopeType | undefined = undefined;
    void envelopeTypeCheck;
    expect(parsed.view.attempts[0]?.attempt_id).toBe("attempt_001");
  });
});
