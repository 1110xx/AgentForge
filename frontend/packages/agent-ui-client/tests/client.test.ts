/**
 * AgentPlatformClient tests with a mocked fetch: URL/path/header shaping,
 * strict response parsing, api-error mapping and the recover command contract.
 */
import { describe, expect, it } from "vitest";
import type { RunViewSnapshot } from "@platform/agent-ui-protocol";
import {
  AgentPlatformClient,
  AgentPlatformProtocolError,
} from "../src/client.js";

interface Call {
  url: string;
  init: RequestInit;
}

const snapshotFixture = (version = 2, runId = "run_demo"): RunViewSnapshot => ({
  schema_version: "run-view-snapshot/v1",
  run_id: runId,
  status: "RUNNING",
  watermark: 4,
  view: {
    run_id: "run_demo",
    parent_run_id: null,
    workflow_type: "synthetic-analysis",
    intent: "Analyze failure patterns",
    status: "RUNNING",
    status_reason: null,
    version,
    created_at: "2026-08-07T00:00:00Z",
    updated_at: "2026-08-07T00:00:03Z",
    ended_at: null,
    execution_units: [],
    attempts: [],
    current_step: null,
    approvals: [],
    artifacts: [],
    surfaces: [],
    watermark: 4,
  },
});

function makeClient(handler: (call: Call) => Response | Promise<Response>) {
  const calls: Call[] = [];
  const fetchImpl = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const call = { url: String(input), init: init ?? {} };
    calls.push(call);
    return handler(call);
  };
  const client = new AgentPlatformClient({
    baseUrl: "/api/agent-platform/",
    getAccessToken: () => "short-lived-token",
    fetchImpl,
  });
  return { client, calls };
}

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const API_ERROR_409 = {
  schema_version: "api-error/v1",
  code: "VERSION_CONFLICT",
  message: "run version changed",
  trace_id: "trace_9",
  retryable: false,
  details: { expected: "2", actual: "3" },
};

describe("AgentPlatformClient REST", () => {
  it("GET run parses a strict snapshot", async () => {
    const { client, calls } = makeClient(() => jsonResponse(200, snapshotFixture()));
    const snapshot = await client.getRun("run_demo");
    expect(snapshot.view.version).toBe(2);
    expect(calls[0]?.url).toBe("/api/agent-platform/v1/runs/run_demo");
    expect(calls[0]?.init.headers).toMatchObject({ Authorization: "Bearer short-lived-token" });
  });

  it("URL-encodes run and effect ids in recoverFailedEffect and enforces If-Match + Idempotency-Key", async () => {
    const { client, calls } = makeClient((call) => {
      expect(call.url).toContain("/v1/runs/run%2Fdemo/effects/effect%2F1/recover");
      return jsonResponse(202, snapshotFixture(3, "run/demo"));
    });
    const snapshot = await client.recoverFailedEffect("run/demo", "effect/1", {
      expectedRunVersion: 3,
      idempotencyKey: "recover-1",
    });
    expect(snapshot.view.version).toBe(3);
    const init = calls[0]?.init;
    expect(init?.headers).toMatchObject({ "If-Match": '"3"', "Idempotency-Key": "recover-1" });
  });

  it("recoverFailedEffect rejects a mismatched run_id in the response", async () => {
    const { client } = makeClient(() => jsonResponse(202, snapshotFixture()));
    await expect(
      client.recoverFailedEffect("other_run", "effect_1", {
        expectedRunVersion: 2,
        idempotencyKey: "recover-2",
      }),
    ).rejects.toBeInstanceOf(AgentPlatformProtocolError);
  });

  it("maps 409 api-error to AgentPlatformApiError with stable code", async () => {
    const { client } = makeClient(() => jsonResponse(409, API_ERROR_409));
    await expect(client.cancelRun("run_demo", { expectedRunVersion: 2 })).rejects.toMatchObject({
      name: "AgentPlatformApiError",
      code: "VERSION_CONFLICT",
      traceId: "trace_9",
      retryable: false,
      details: { expected: "2", actual: "3" },
    });
  });

  it("createRun sends Idempotency-Key and a strict command body", async () => {
    const { client, calls } = makeClient((call) => {
      expect((call.init.headers as Record<string, string>)["Idempotency-Key"]).toBe("create-1");
      const body = JSON.parse(String(call.init.body));
      expect(body.workflow_type).toBe("synthetic-analysis");
      expect(body.resource_refs).toEqual(["submission:demo"]);
      return jsonResponse(201, snapshotFixture(1));
    });
    await client.createRun(
      {
        workflow_type: "synthetic-analysis",
        intent: "Analyze failure patterns",
        resource_refs: ["submission:demo"],
      },
      { idempotencyKey: "create-1" },
    );
    expect(calls[0]?.url).toBe("/api/agent-platform/v1/runs");
  });

  it("createRun rejects authority-shaped parameters client-side", async () => {
    const { client } = makeClient(() => jsonResponse(201, snapshotFixture()));
    await expect(
      client.createRun(
        {
          workflow_type: "synthetic-analysis",
          intent: "x",
          resource_refs: ["submission:demo"],
          parameters: { apiKey: "secret" },
        },
        { idempotencyKey: "create-2" },
      ),
    ).rejects.toBeInstanceOf(AgentPlatformProtocolError); // zod parse failure mapped
  });

  it("getRunEvents builds the after_event_seq query", async () => {
    const { client, calls } = makeClient(() =>
      jsonResponse(200, {
        schema_version: "run-event-page/v1",
        run_id: "run_demo",
        after_event_seq: 1,
        watermark: 2,
        retention_floor: 0,
        resync_required: false,
        events: [],
      }),
    );
    await client.getRunEvents("run_demo", { afterEventSeq: 1, limit: 50 });
    expect(calls[0]?.url).toContain("after_event_seq=1");
    expect(calls[0]?.url).toContain("limit=50");
  });

  it("getSurfaceRevision returns a strict surface revision", async () => {
    const { client } = makeClient(() =>
      jsonResponse(200, {
        schema_version: "a2ui-surface-revision/v0.9.1",
        surface_id: "surface_summary",
        run_id: "run_demo",
        revision: 2,
        source_attempt_id: "attempt_001",
        source_event_seq: 4,
        document: { component: "EvidenceSummary", props: { title: "Evidence" } },
        checksum: "sha256:abc",
      }),
    );
    const revision = await client.getSurfaceRevision("run_demo", "surface_summary", {
      revision: 2,
    });
    expect(revision.document.component).toBe("EvidenceSummary");
  });

  it("getArtifactDownloadAuthorization parses the authorization", async () => {
    const { client } = makeClient(() =>
      jsonResponse(200, {
        schema_version: "artifact-download-authorization/v1",
        authorization_id: "authz_1",
        artifact_id: "artifact_1",
        version: 1,
        download_url: "https://signed.example/artifact_1?v=1",
        expires_at: "2026-08-08T00:00:00Z",
      }),
    );
    const authorization = await client.getArtifactDownloadAuthorization(
      "run_demo",
      "artifact_1",
      1,
    );
    expect(authorization.download_url).toContain("signed.example");
  });
});

describe("AgentPlatformClient SSE stream", () => {
  it("streams events with Last-Event-ID from the cursor", async () => {
    const frame =
      `id: 4\nevent: ui.surface.committed\n` +
      `data: {"schema_version":"enterprise-event/v1","event_id":"e4","tenant_id":"t","run_id":"run_demo","event_seq":4,"event_type":"ui.surface.committed","occurred_at":"2026-08-07T00:00:00Z","producer_service":"control-plane","payload_schema":"a2ui-surface/v0.9.1","payload":{"kind":"ui.surface.committed","surface_id":"surface_summary","revision":1}}\n\n`;
    const { client, calls } = makeClient((call) => {
      expect((call.init.headers as Record<string, string>)["Last-Event-ID"]).toBe("3");
      return new Response(frame, {
        status: 200,
        headers: { "Content-Type": "text/event-stream" },
      });
    });
    const events = [];
    for await (const event of client.streamRunEvents("run_demo", {
      afterEventSeq: 3,
    })) {
      events.push(event);
    }
    expect(events).toHaveLength(1);
    expect(events[0]?.payload.kind).toBe("ui.surface.committed");
    expect(calls[0]?.url).toContain("/events/stream");
  });

  it("surfaces non-ok SSE responses as api errors", async () => {
    const { client } = makeClient(() =>
      jsonResponse(409, {
        schema_version: "api-error/v1",
        code: "RESYNC_REQUIRED",
        message: "cursor precedes retention floor",
      }),
    );
    await expect(async () => {
      const iterator = client.streamRunEvents("run_demo", { afterEventSeq: 1 });
      await iterator.next();
    }).rejects.toMatchObject({ code: "RESYNC_REQUIRED" });
  });
});

describe("AgentPlatformClient chat (Phase 3.6 frontend launcher)", () => {
  it("POSTs /v1/chat with an Idempotency-Key and parses the snapshot", async () => {
    const { client, calls } = makeClient(() =>
      jsonResponse(201, snapshotFixture()),
    );
    const snapshot = await client.chat({ message: "分析日志中的故障模式" });
    expect(snapshot.schema_version).toBe("run-view-snapshot/v1");
    const call = calls[0];
    expect(call?.url).toBe("/api/agent-platform/v1/chat");
    expect(call?.init.method).toBe("POST");
    const headers = call?.init.headers as Record<string, string>;
    expect(headers["Idempotency-Key"]).toBeTruthy();
    expect(headers["Content-Type"]).toBe("application/json");
    const body = JSON.parse(String(call?.init.body)) as Record<string, unknown>;
    expect(body["message"]).toBe("分析日志中的故障模式");
    // zod materializes the default resource refs (mirror of the backend default)
    expect(body["resource_refs"]).toEqual(["synthetic-case:demo"]);
    expect(body).not.toHaveProperty("workflow_hint");
  });

  it("uses the caller-supplied idempotency key verbatim", async () => {
    const { client, calls } = makeClient(() =>
      jsonResponse(201, snapshotFixture()),
    );
    await client.chat({ message: "hello" }, { idempotencyKey: "chat-stable-1" });
    const headers = calls[0]?.init.headers as Record<string, string>;
    expect(headers["Idempotency-Key"]).toBe("chat-stable-1");
  });

  it("rejects blank messages client-side without sending a request", async () => {
    const { client, calls } = makeClient(() =>
      jsonResponse(201, snapshotFixture()),
    );
    await expect(client.chat({ message: "   " })).rejects.toBeInstanceOf(
      AgentPlatformProtocolError,
    );
    expect(calls).toHaveLength(0);
  });

  it("rejects overlong messages client-side", async () => {
    const { client, calls } = makeClient(() =>
      jsonResponse(201, snapshotFixture()),
    );
    await expect(
      client.chat({ message: "a".repeat(2001) }),
    ).rejects.toBeInstanceOf(AgentPlatformProtocolError);
    expect(calls).toHaveLength(0);
  });

  it("maps non-2xx /v1/chat responses to api errors", async () => {
    const { client } = makeClient(() =>
      jsonResponse(422, {
        schema_version: "api-error/v1",
        code: "REQUEST_VALIDATION_FAILED",
        message: "message cannot be blank",
      }),
    );
    await expect(client.chat({ message: "hello" })).rejects.toMatchObject({
      code: "REQUEST_VALIDATION_FAILED",
    });
  });
});
