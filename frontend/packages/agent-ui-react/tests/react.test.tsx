/**
 * @vitest-environment jsdom
 *
 * End-to-end wiring tests: AgentPlatformProvider + AgentPanel consume a real
 * AgentPlatformClient (mocked fetch) through the projection synchronizer;
 * approval actions and authorized downloads only flow through the Host Bridge.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import {
  AgentPlatformClient,
  createIdempotencyKey,
} from "@platform/agent-ui-client";
import type { RunViewSnapshot } from "@platform/agent-ui-protocol";
import {
  AgentLauncher,
  AgentPanel,
  AgentPlatformProvider,
  useAgentPlatform,
} from "../src/index.js";
import type { HostBridgeCapabilities } from "@platform/agent-ui-protocol/host";

interface Call {
  url: string;
  init: RequestInit;
}

function snapshot(runId = "run_demo"): RunViewSnapshot {
  return {
    schema_version: "run-view-snapshot/v1",
    run_id: runId,
    status: "WAITING_APPROVAL",
    watermark: 4,
    view: {
      run_id: runId,
      parent_run_id: null,
      workflow_type: "synthetic-analysis",
      intent: "Analyze failure patterns",
      status: "WAITING_APPROVAL",
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
  };
}

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function sseResponse(body: string): Response {
  return new Response(body, {
    status: 200,
    headers: { "Content-Type": "text/event-stream" },
  });
}

interface HarnessOptions {
  surfaceDocument: Record<string, unknown>;
  hostBridge?: Partial<HostBridgeCapabilities>;
}

function makeHarness(options: HarnessOptions) {
  const calls: Call[] = [];
  const downloadAuthorizedArtifact = vi.fn(async () => undefined);
  const hostBridge: HostBridgeCapabilities = {
    schema_version: "host-bridge-capabilities/v1",
    navigate: vi.fn(async () => undefined),
    downloadAuthorizedArtifact,
    ...options.hostBridge,
  };
  const fetchImpl = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = String(input);
    const call: Call = { url, init: init ?? {} };
    calls.push(call);
    if (url.includes("/events/stream")) {
      return sseResponse(":heartbeat\n\n");
    }
    if (url.includes("/surfaces/surface_summary")) {
      return jsonResponse(200, {
        schema_version: "a2ui-surface-revision/v0.9.1",
        surface_id: "surface_summary",
        run_id: "run_demo",
        revision: 1,
        source_attempt_id: "attempt_001",
        source_event_seq: 2,
        document: options.surfaceDocument,
        checksum: "sha256:abc",
      });
    }
    if (url.includes("/download-authorization")) {
      return jsonResponse(200, {
        schema_version: "artifact-download-authorization/v1",
        authorization_id: "authz_1",
        artifact_id: "artifact_001",
        version: 1,
        download_url: "https://signed.example/artifact_001",
        expires_at: "2026-08-08T00:00:00Z",
      });
    }
    if (url.endsWith("/actions")) {
      return jsonResponse(202, snapshot());
    }
    if (url.endsWith("/runs/run_demo")) {
      return jsonResponse(200, snapshot());
    }
    if (url.endsWith("/v1/chat")) {
      return jsonResponse(201, snapshot());
    }
    throw new Error(`unhandled url ${url}`);
  };
  const client = new AgentPlatformClient({
    baseUrl: "/api/agent-platform/",
    getAccessToken: () => "short-lived-token",
    fetchImpl,
  });
  return { calls, client, hostBridge, downloadAuthorizedArtifact };
}

function renderPanel(calls: Call[], client: AgentPlatformClient, hostBridge: HostBridgeCapabilities) {
  return render(
    <AgentPlatformProvider client={client} hostBridge={hostBridge}>
      <AgentPanel runId="run_demo" />
    </AgentPlatformProvider>,
  );
}

describe("AgentPlatformProvider boundary", () => {
  it("useAgentPlatform throws outside the provider", () => {
    const { client, hostBridge } = makeHarness({ surfaceDocument: { component: "EvidenceSummary", props: {} } });
    void client;
    void hostBridge;
    expect(() => {
      function Consumer() {
        useAgentPlatform();
        return null;
      }
      render(<Consumer />);
    }).toThrow(/AgentPlatformProvider/);
  });
});

describe("AgentPanel projection wiring", () => {
  it("loads the snapshot, fetches the surface document and renders the catalog", async () => {
    const { calls, client, hostBridge } = makeHarness({
      surfaceDocument: {
        component: "EvidenceSummary",
        props: { title: "Synthetic failure evidence", items: ["submission:demo:A1"] },
      },
    });
    renderPanel(calls, client, hostBridge);
    await waitFor(() => expect(screen.getByText("Analyze failure patterns")).toBeTruthy());
    await waitFor(() =>
      expect(screen.getByText("Synthetic failure evidence")).toBeTruthy(),
    );
    expect(screen.getByText("WAITING_APPROVAL")).toBeTruthy();
    expect(screen.getByText("submission:demo:A1")).toBeTruthy();
    // Initial read flow: snapshot first, SSE from its watermark.
    const streamCall = calls.find((call) => call.url.includes("/events/stream"));
    expect(streamCall).toBeDefined();
    expect(
      (streamCall?.init.headers as Record<string, string>)["Last-Event-ID"],
    ).toBe("4");
  });
});

describe("ApprovalCard action wiring", () => {
  it("sends a Surface-bound action without approval_id or credentials", async () => {
    const { calls, client, hostBridge } = makeHarness({
      surfaceDocument: {
        component: "ApprovalCard",
        props: {
          approval_id: "approval_001",
          approve_key: "approval:approval_001:approve",
          reject_key: "approval:approval_001:reject",
          title: "Create a synthetic reference defect?",
          displayed_digest: "sha256:server-request",
          canonical_request_ref: "proposal:001",
        },
      },
    });
    const user = userEvent.setup();
    renderPanel(calls, client, hostBridge);
    await waitFor(() =>
      expect(screen.getByText("Create a synthetic reference defect?")).toBeTruthy(),
    );
    await user.click(screen.getByRole("button", { name: "Approve" }));
    await waitFor(() => {
      const actionCall = calls.find((call) => call.url.endsWith("/actions"));
      expect(actionCall).toBeDefined();
    });
    const actionCall = calls.find((call) => call.url.endsWith("/actions"));
    const body = JSON.parse(String(actionCall?.init.body)) as Record<string, unknown>;
    expect(body.action_ref).toBe("approval:approval_001:approve");
    expect(body.surface_id).toBe("surface_summary");
    expect(body.surface_revision).toBe(1);
    expect(body.displayed_digest).toBe("sha256:server-request");
    expect(body).not.toHaveProperty("approval_id");
    expect(body).not.toHaveProperty("token");
    expect(body).not.toHaveProperty("credential");
    // client_action_id doubles as the Idempotency-Key (backend invariant).
    const headers = actionCall?.init.headers as Record<string, string>;
    expect(headers["Idempotency-Key"]).toBe(body.client_action_id);
    expect(body.client_action_id).toBeTypeOf("string");
    expect(createIdempotencyKey("probe")).not.toBe(body.client_action_id);
  });
});

describe("ArtifactCard download wiring", () => {
  it("requests an authorization and hands it to the Host Bridge", async () => {
    const { calls, client, hostBridge, downloadAuthorizedArtifact } = makeHarness({
      surfaceDocument: {
        component: "ArtifactCard",
        props: {
          title: "Synthetic analysis report",
          artifact_id: "artifact_001",
          version: 1,
          download_action_ref: "artifact:artifact_001:download",
        },
      },
    });
    const user = userEvent.setup();
    renderPanel(calls, client, hostBridge);
    await waitFor(() =>
      expect(screen.getByText("Synthetic analysis report")).toBeTruthy(),
    );
    await user.click(screen.getByRole("button", { name: "Download" }));
    await waitFor(() =>
      expect(downloadAuthorizedArtifact).toHaveBeenCalledTimes(1),
    );
    expect(calls.some((call) => call.url.includes("/download-authorization"))).toBe(true);
    const argument = downloadAuthorizedArtifact.mock.calls[0]?.[0] as {
      authorization: { authorization_id: string; download_url: string };
    };
    expect(argument.authorization.authorization_id).toBe("authz_1");
    expect(argument.authorization.download_url).toContain("signed.example");
  });
});

describe("AgentLauncher chat entry", () => {
  it("expands, sends a message via /v1/chat and reports the created run", async () => {
    const { calls, client, hostBridge } = makeHarness({ surfaceDocument: {} });
    const onRunCreated = vi.fn();
    const user = userEvent.setup();
    render(
      <AgentPlatformProvider client={client} hostBridge={hostBridge}>
        <AgentLauncher onRunCreated={onRunCreated} />
      </AgentPlatformProvider>,
    );
    // collapsed by default: pill only, no message input
    expect(screen.queryByRole("textbox", { name: /chat message/i })).toBeNull();
    await user.click(screen.getByRole("button", { name: /open agent chat/i }));
    await user.type(
      screen.getByRole("textbox", { name: /chat message/i }),
      "分析日志中的故障模式",
    );
    await user.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() =>
      expect(calls.some((call) => call.url.endsWith("/v1/chat"))).toBe(true),
    );
    await waitFor(() =>
      expect(onRunCreated).toHaveBeenCalledWith("run_demo"),
    );
    await waitFor(() =>
      expect(screen.getByText("分析日志中的故障模式")).toBeTruthy(),
    );
    // entry badge shows the created run id prefix
    await waitFor(() =>
      expect(screen.getByText(/run run_demo/)).toBeTruthy(),
    );
    // Idempotency-Key header is present (SDK derives it per message)
    const chatCall = calls.find((call) => call.url.endsWith("/v1/chat"));
    expect(
      (chatCall?.init.headers as Record<string, string>)["Idempotency-Key"],
    ).toBeTruthy();
  });

  it("does not send blank messages", async () => {
    const { calls, client, hostBridge } = makeHarness({ surfaceDocument: {} });
    const onRunCreated = vi.fn();
    const user = userEvent.setup();
    render(
      <AgentPlatformProvider client={client} hostBridge={hostBridge}>
        <AgentLauncher onRunCreated={onRunCreated} />
      </AgentPlatformProvider>,
    );
    await user.click(screen.getByRole("button", { name: /open agent chat/i }));
    await user.type(
      screen.getByRole("textbox", { name: /chat message/i }),
      "   ",
    );
    expect(
      screen.getByRole("button", { name: "Send" }),
    ).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Send" }));
    expect(calls.some((call) => call.url.endsWith("/v1/chat"))).toBe(false);
    expect(onRunCreated).not.toHaveBeenCalled();
  });
});
