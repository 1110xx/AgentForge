/**
 * In-browser mock of the AgentForge public API for the demo mode.
 *
 * It serves the exact contract shapes (strict-parseable by the SDK) and
 * replays a canned reference workflow: create -> RUNNING -> attempt -> three
 * surfaces -> approval -> effect -> SUCCEEDED. The SSE stream emits the
 * timeline progressively with heartbeats, exactly like the real backend.
 */
import type {
  ArtifactDownloadAuthorization,
  EnterpriseEventEnvelope,
  JsonObject,
  RunEventPage,
  RunViewSnapshot,
  SurfaceRevision,
} from "@platform/agent-ui-protocol";

interface MockFollowupRecord {
  schema_version: "followup-record/v1";
  run_id: string;
  followup_seq: number;
  question: string;
  answer: string | null;
  answered_at: string | null;
  client_followup_id: string;
  status: "PENDING" | "ANSWERED";
}

const OCCURRED_AT = "2026-08-07T00:00:00Z";
const TENANT = "demo-tenant";

interface MockRunState {
  runId: string;
  workflowType: string;
  intent: string;
  version: number;
  approved: boolean;
}

const SURFACE_IDS = {
  evidence: (runId: string) => `evidence-${runId}`,
  artifact: (runId: string) => `artifact-${runId}`,
  approval: (runId: string) => `approval-${runId}`,
} as const;

function envelope(
  seq: number,
  runId: string,
  eventType: EnterpriseEventEnvelope["event_type"],
  payload: EnterpriseEventEnvelope["payload"],
  payloadSchema: string,
  attemptId: string | null = null,
): EnterpriseEventEnvelope {
  return {
    schema_version: "enterprise-event/v1",
    event_id: `evt_${seq}`,
    tenant_id: TENANT,
    run_id: runId,
    event_seq: seq,
    event_type: eventType,
    occurred_at: OCCURRED_AT,
    producer_service: "control-plane",
    payload_schema: payloadSchema,
    payload,
    attempt_id: attemptId,
    causation_event_id: null,
    trace_id: "trace-demo",
  };
}

function timeline(runId: string): EnterpriseEventEnvelope[] {
  return [
    envelope(1, runId, "run.created", { kind: "run.created", workflow_type: "synthetic-analysis" }, "run-created/v1"),
    envelope(2, runId, "run.status.changed", { kind: "run.status.changed", previous: "QUEUED", current: "RUNNING" }, "run-status/v1"),
    envelope(3, runId, "attempt.lifecycle", { kind: "attempt.lifecycle", attempt_id: "attempt_001", status: "RUNNING" }, "attempt-lifecycle/v1", "attempt_001"),
    envelope(4, runId, "ui.surface.committed", { kind: "ui.surface.committed", surface_id: SURFACE_IDS.evidence(runId), revision: 1 }, "a2ui-surface/v0.9.1", "attempt_001"),
    envelope(5, runId, "ui.surface.committed", { kind: "ui.surface.committed", surface_id: SURFACE_IDS.artifact(runId), revision: 1 }, "a2ui-surface/v0.9.1", "attempt_001"),
    envelope(6, runId, "ui.surface.committed", { kind: "ui.surface.committed", surface_id: SURFACE_IDS.approval(runId), revision: 1 }, "a2ui-surface/v0.9.1", "attempt_001"),
    envelope(7, runId, "approval.decided", { kind: "approval.decided", approval_id: "approval_001", status: "APPROVED" }, "approval/v1"),
    envelope(8, runId, "effect.status.changed", { kind: "effect.status.changed", effect_id: "effect_001", status: "SUCCEEDED" }, "effect/v1"),
    envelope(9, runId, "run.status.changed", { kind: "run.status.changed", previous: "RUNNING", current: "SUCCEEDED" }, "run-status/v1"),
  ];
}

function surfacesAt(runId: string, watermark: number): RunViewSnapshot["view"]["surfaces"] {
  const result: RunViewSnapshot["view"]["surfaces"] = [];
  if (watermark >= 4) result.push({ surface_id: SURFACE_IDS.evidence(runId), catalog_id: "public-catalog", revision: 1 });
  if (watermark >= 5) result.push({ surface_id: SURFACE_IDS.artifact(runId), catalog_id: "public-catalog", revision: 1 });
  if (watermark >= 6) result.push({ surface_id: SURFACE_IDS.approval(runId), catalog_id: "public-catalog", revision: 1 });
  return result;
}

function snapshot(state: MockRunState, watermark: number, status: RunViewSnapshot["status"]): RunViewSnapshot {
  return {
    schema_version: "run-view-snapshot/v1",
    run_id: state.runId,
    status,
    watermark,
    view: {
      run_id: state.runId,
      parent_run_id: null,
      workflow_type: state.workflowType,
      intent: state.intent,
      status,
      status_reason: null,
      version: state.version,
      created_at: OCCURRED_AT,
      updated_at: OCCURRED_AT,
      ended_at: status === "SUCCEEDED" ? OCCURRED_AT : null,
      execution_units: [
        { execution_unit_id: "unit_primary", role: "primary", status: status === "SUCCEEDED" ? "SUCCEEDED" : "EXECUTING", version: 1 },
      ],
      attempts: [
        {
          attempt_id: "attempt_001",
          execution_unit_id: "unit_primary",
          step_id: null,
          status: status === "SUCCEEDED" ? "SUCCEEDED" : "RUNNING",
          version: 1,
          started_at: OCCURRED_AT,
          ended_at: status === "SUCCEEDED" ? OCCURRED_AT : null,
        },
      ],
      current_step: null,
      approvals: [],
      artifacts: [],
      surfaces: surfacesAt(state.runId, watermark),
      watermark,
    },
  };
}

function surfaceRevision(runId: string, surfaceId: string): SurfaceRevision {
  const document: JsonObject =
    surfaceId === SURFACE_IDS.evidence(runId)
      ? {
          component: "EvidenceSummary",
          props: {
            title: "Synthetic failure evidence",
            data_ref: `artifact:report:${runId}:1`,
            items: ["submission:demo:A1:sha256:evidence"],
          },
        }
      : surfaceId === SURFACE_IDS.artifact(runId)
        ? {
            component: "ArtifactCard",
            props: {
              title: "Synthetic analysis report",
              artifact_id: "report",
              version: 1,
              download_action_ref: "artifact:report:download",
            },
          }
        : {
            component: "ApprovalCard",
            props: {
              approval_id: "approval_001",
              approve_key: "approval:approval_001:approve",
              reject_key: "approval:approval_001:reject",
              title: "Create a synthetic reference defect?",
              displayed_digest: "sha256:server-request",
              canonical_request_ref: "proposal:001",
            },
          };
  return {
    schema_version: "a2ui-surface-revision/v0.9.1",
    surface_id: surfaceId,
    run_id: runId,
    revision: 1,
    source_attempt_id: "attempt_001",
    source_event_seq: 4,
    document,
    checksum: "sha256:mock-surface",
  };
}

const AUTHORIZATION: ArtifactDownloadAuthorization = {
  schema_version: "artifact-download-authorization/v1",
  authorization_id: "authz-demo-1",
  artifact_id: "report",
  version: 1,
  download_url: "https://demo.example/signed/report?v=1&exp=2026-08-08",
  expires_at: "2026-08-08T00:00:00Z",
};

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function apiError(code: string, message: string, status: number): Response {
  return jsonResponse(status, {
    schema_version: "api-error/v1",
    code,
    message,
    trace_id: "trace-demo",
    retryable: false,
    details: {},
  });
}

/** SSE body: replay timeline events after the cursor, then heartbeat forever. */
function sseStream(state: MockRunState, afterEventSeq: number): Response {
  const encoder = new TextEncoder();
  const frames = timeline(state.runId).filter((event) => event.event_seq > afterEventSeq);
  const stream = new ReadableStream<Uint8Array>({
    async start(controller) {
      for (const event of frames) {
        await new Promise((resolve) => setTimeout(resolve, 700));
        const data = JSON.stringify(event);
        controller.enqueue(
          encoder.encode(`id: ${event.event_seq}\nevent: ${event.event_type}\ndata: ${data}\n\n`),
        );
      }
      // Send a few heartbeats then close the stream so the synchronizer
      // proceeds to refreshSurfaceDocuments() and reconnects (simulating
      // the real backend's idle-timeout behaviour).
      let hbCount = 0;
      const heartbeat = setInterval(() => {
        controller.enqueue(encoder.encode(":heartbeat\n\n"));
        hbCount += 1;
        if (hbCount >= 2) {
          clearInterval(heartbeat);
          controller.close();
        }
      }, 1_000);
    },
  });
  return new Response(stream, {
    status: 200,
    headers: { "Content-Type": "text/event-stream" },
  });
}

/** Build a fetch implementation that serves the demo flow. */
export function createMockFetch(): typeof fetch {
  const runs = new Map<string, MockRunState>();
  const followupStore = new Map<string, MockFollowupRecord[]>();
  const followupIdempotency = new Map<string, MockFollowupRecord>();
  let nextFollowupSeq = 0;
  // One session_id per run so followup-answer responses are stable
  const followupSessionId = new Map<string, string>();

  function generateMockAnswer(question: string): string {
    const q = question.toLowerCase();
    if (q.includes("为什么") || q.includes("why")) {
      return "基于本次分析结果，该决策的考量因素包括：1) 数据完整性要求 2) 执行效率最优 3) 风险控制策略。具体而言，在处理异常模式时，系统优先选择了可验证的路径以确保结果的可追溯性。";
    }
    if (q.includes("数据") || q.includes("data")) {
      return "本次分析共处理了 12,456 条生产环境日志数据，时间跨度 2026-08-17 14:00 至 2026-08-17 15:00。数据来源包括：服务 A（6,230 条）、服务 B（3,847 条）、API Gateway（2,379 条）。";
    }
    if (q.includes("阈值") || q.includes("threshold")) {
      return "当前配置的告警阈值为：CPU 使用率 80%，内存使用率 85%，响应时间 P99 500ms。建议根据本次检测到的异常模式进行优化调整。";
    }
    if (q.includes("具体") || q.includes("detail")) {
      return "根据详细追踪信息，内存泄漏模式的根因是服务 A 连接池在高峰期未正确释放空闲连接。响应延迟尖峰出现在 API Gateway 的 /v1/orders 端点，建议检查上游数据库查询性能。";
    }
    return `针对「${question}」的解答：在本次任务上下文中，系统已完成 4 阶段分析并识别出 3 个高置信度异常模式。如需更多信息，请进一步指定关注点。`;
  }

  const fetchImpl: typeof fetch = async (input, init) => {
    const url = new URL(String(input), "http://demo.invalid");
    const method = init?.method ?? "GET";
    const path = url.pathname.replace(/^\/api\/agent-platform/, "").replace(/^\/+/, "");
    const parts = path.split("/").filter(Boolean); // v1, runs, {run_id}, ...
    const runId = parts[2] ?? "";

    if (method === "POST" && parts[0] === "v1" && parts[1] === "runs" && parts.length === 2) {
      const body = JSON.parse(String(init?.body ?? "{}")) as {
        workflow_type: string;
        intent: string;
      };
      const state: MockRunState = {
        runId: `run-demo-${runs.size + 1}`,
        workflowType: body.workflow_type ?? "synthetic-analysis",
        intent: body.intent ?? "Analyze failure patterns",
        version: 1,
        approved: false,
      };
      runs.set(state.runId, state);
      return jsonResponse(201, snapshot(state, 1, "QUEUED"));
    }

    if (
      method === "POST" &&
      parts[0] === "v1" &&
      parts[1] === "chat" &&
      parts.length === 2
    ) {
      // Phase 3.6 frontend launcher: mirror of POST /v1/chat on the real
      // backend (contract shapes identical, including the Location header).
      const body = JSON.parse(String(init?.body ?? "{}")) as {
        message?: string;
        workflow_hint?: string;
      };
      const message = (body.message ?? "").trim();
      if (!message) {
        return apiError(
          "REQUEST_VALIDATION_FAILED",
          "message cannot be blank",
          422,
        );
      }
      // Demo intent mapping: only synthetic-analysis is registered, so hints
      // and any message both land there (back end classify_intent MVP
      // fallback within the demo scope).
      const state: MockRunState = {
        runId: `run-demo-${runs.size + 1}`,
        workflowType: body.workflow_hint?.trim() || "synthetic-analysis",
        intent: message,
        version: 1,
        approved: false,
      };
      runs.set(state.runId, state);
      const response = jsonResponse(201, snapshot(state, 1, "QUEUED"));
      response.headers.set("Location", `/v1/runs/${state.runId}`);
      return response;
    }

    const state = runs.get(runId);
    if (state === undefined) {
      return apiError("NOT_FOUND", "run was not found", 404);
    }

    if (method === "GET" && parts[1] === "runs" && parts.length === 3) {
      const watermark = state.approved ? 9 : 1;
      const status = state.approved ? "SUCCEEDED" : "QUEUED";
      return jsonResponse(200, snapshot(state, watermark, status));
    }

    if (method === "GET" && parts[1] === "runs" && parts[3] === "events" && parts.length === 4) {
      const after = Number(url.searchParams.get("after_event_seq") ?? "0");
      const events = timeline(runId).filter((event) => event.event_seq > after);
      const page: RunEventPage = {
        schema_version: "run-event-page/v1",
        run_id: runId,
        after_event_seq: after,
        watermark: events.length === 0 ? after : events[events.length - 1]?.event_seq ?? after,
        retention_floor: 0,
        resync_required: false,
        events,
      };
      return jsonResponse(200, page);
    }

    if (method === "GET" && parts[1] === "runs" && parts[3] === "events" && parts[4] === "stream") {
      const cursorHeader = init?.headers instanceof Headers ? init.headers.get("Last-Event-ID") : null;
      const cursor = cursorHeader === null ? Number(url.searchParams.get("after_event_seq") ?? "1") : Number(cursorHeader);
      return sseStream(state, cursor);
    }

    if (method === "GET" && parts[1] === "runs" && parts[3] === "surfaces" && parts.length === 5) {
      return jsonResponse(200, surfaceRevision(runId, parts[4] ?? ""));
    }

    if (method === "POST" && parts[1] === "runs" && parts[3] === "actions") {
      state.approved = true;
      state.version += 1;
      return jsonResponse(202, snapshot(state, 9, "SUCCEEDED"));
    }

    if (method === "GET" && parts[1] === "runs" && parts[3] === "artifacts" && parts[5] === "versions" && parts[7] === "download-authorization") {
      return jsonResponse(200, AUTHORIZATION);
    }

    /* ---- Followup routes ---- */

    if (method === "POST" && parts[1] === "runs" && parts[3] === "followups" && parts.length === 4) {
      const body = JSON.parse(String(init?.body ?? "{}")) as {
        run_id: string;
        question: string;
        client_followup_id: string;
      };
      // Idempotency check
      const existing = followupIdempotency.get(body.client_followup_id);
      if (existing) {
        return jsonResponse(200, {
          schema_version: "followup-answer/v1",
          run_id: runId,
          session_id: followupSessionId.get(runId) ?? "session-demo",
          question: existing.question,
          answer: existing.answer,
        });
      }
      const seq = nextFollowupSeq++;
      const answeredAt = new Date().toISOString();
      const answer = generateMockAnswer(body.question);
      if (!followupSessionId.has(runId)) {
        followupSessionId.set(runId, `session-demo-${runId}`);
      }
      const record: MockFollowupRecord = {
        schema_version: "followup-record/v1",
        run_id: runId,
        followup_seq: seq,
        question: body.question,
        answer,
        answered_at: answeredAt,
        client_followup_id: body.client_followup_id,
        status: "ANSWERED",
      };
      const records = followupStore.get(runId) ?? [];
      records.push(record);
      followupStore.set(runId, records);
      followupIdempotency.set(body.client_followup_id, record);
      return jsonResponse(200, {
        schema_version: "followup-answer/v1",
        run_id: runId,
        session_id: followupSessionId.get(runId) ?? "session-demo",
        question: body.question,
        answer,
      });
    }

    if (method === "GET" && parts[1] === "runs" && parts[3] === "followups" && parts.length === 4) {
      const records = followupStore.get(runId) ?? [];
      // Ensure every record carries schema_version (safety for records stored before this fix)
      const safeRecords = records.map((r) => ({
        schema_version: "followup-record/v1" as const,
        run_id: r.run_id,
        followup_seq: r.followup_seq,
        question: r.question,
        answer: r.answer,
        answered_at: r.answered_at,
        client_followup_id: r.client_followup_id,
        status: r.status,
      }));
      return jsonResponse(200, {
        schema_version: "followup-history-page/v1",
        run_id: runId,
        total_count: safeRecords.length,
        records: safeRecords,
      });
    }

    return apiError("NOT_FOUND", `mock route not found: ${path}`, 404);
  };
  return fetchImpl;
}
