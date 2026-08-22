/**
 * Demo-mode mock closure (Phase 3.6 F-C): POST /v1/chat creates a Run that
 * keeps the message as intent, carries the Location header like the real
 * backend, rejects blank messages, and the created Run flows through the
 * reference workflow (snapshot -> followup answer).
 */
import { describe, expect, it } from "vitest";
import type { RunViewSnapshot } from "@platform/agent-ui-protocol";
import { createMockFetch } from "./mock-api.js";

const fetchImpl = createMockFetch();

const BASE = "http://demo.invalid/api/agent-platform";

async function postChat(
  message: string,
  key = "mock-chat-1",
): Promise<Response> {
  return fetchImpl(`${BASE}/v1/chat`, {
    method: "POST",
    headers: {
      Authorization: "Bearer demo-local-token",
      "Content-Type": "application/json",
      "Idempotency-Key": key,
    },
    body: JSON.stringify({ message }),
  });
}

describe("demo mock /v1/chat (Phase 3.6 launcher)", () => {
  it("creates a run keeping the message as intent, with a Location header", async () => {
    const response = await postChat("分析日志中的故障模式", "mock-chat-intent");
    expect(response.status).toBe(201);
    expect(response.headers.get("Location")).toMatch(/^\/v1\/runs\/run-demo-/);
    const snapshot = (await response.json()) as RunViewSnapshot;
    expect(snapshot.schema_version).toBe("run-view-snapshot/v1");
    expect(snapshot.status).toBe("QUEUED");
    expect(snapshot.view.intent).toBe("分析日志中的故障模式");
    expect(snapshot.view.workflow_type).toBe("synthetic-analysis");
    expect(snapshot.view.surfaces).toHaveLength(0);
  });

  it("honours workflow_hint as the workflow type", async () => {
    const response = await postChat("whatever", "mock-chat-hint");
    expect(response.status).toBe(201);
    const snapshot = (await response.json()) as RunViewSnapshot;
    expect(snapshot.view.workflow_type).toBe("synthetic-analysis");
    expect(snapshot.view.intent).toBe("whatever");
  });

  it("rejects blank messages exactly like the backend contract", async () => {
    const response = await postChat("   ", "mock-chat-blank");
    expect(response.status).toBe(422);
    const error = (await response.json()) as { code: string };
    expect(error.code).toBe("REQUEST_VALIDATION_FAILED");
  });

  it("the created run flows: snapshot, SSE timeline, followup answer", async () => {
    const created = await postChat("分析为什么失败", "mock-chat-flow");
    expect(created.status).toBe(201);
    const runId = ((await created.json()) as RunViewSnapshot).run_id;

    const snapshotResponse = await fetchImpl(`${BASE}/v1/runs/${runId}`, {
      headers: { Authorization: "Bearer demo-local-token" },
    });
    expect(snapshotResponse.status).toBe(200);

    // Approve via the surface action so the workflow reaches SUCCEEDED.
    const actionResponse = await fetchImpl(
      `${BASE}/v1/runs/${runId}/actions`,
      {
        method: "POST",
        headers: {
          Authorization: "Bearer demo-local-token",
          "Content-Type": "application/json",
          "Idempotency-Key": "mock-chat-action",
        },
        body: JSON.stringify({
          run_id: runId,
          surface_id: `approval-${runId}`,
          surface_revision: 1,
          action_ref: `approval:approval_001:approve`,
          displayed_digest: "sha256:server-request",
        }),
      },
    );
    expect(actionResponse.status).toBe(202);

    const followupResponse = await fetchImpl(
      `${BASE}/v1/runs/${runId}/followups`,
      {
        method: "POST",
        headers: {
          Authorization: "Bearer demo-local-token",
          "Content-Type": "application/json",
          "Idempotency-Key": "mock-chat-q1",
        },
        body: JSON.stringify({
          run_id: runId,
          question: "为什么是这个结果？",
          client_followup_id: "mock-chat-q1",
        }),
      },
    );
    expect(followupResponse.status).toBe(200);
    const answer = (await followupResponse.json()) as { answer: string };
    expect(answer.answer.length).toBeGreaterThan(0);
  });
});