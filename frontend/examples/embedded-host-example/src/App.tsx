import { useMemo, useState, type CSSProperties, type FormEvent } from "react";
import {
  AgentPlatformClient,
  createIdempotencyKey,
  type AgentPlatformClientOptions,
} from "@platform/agent-ui-client";
import {
  AgentLauncher,
  AgentPanel,
  AgentPlatformProvider,
  type HostBridgeCapabilities,
} from "@platform/agent-ui-react";
import { createMockFetch } from "./mock-api.js";

type DemoMode = "demo" | "live";

const pageStyle: CSSProperties = {
  maxWidth: "520px",
  margin: "0 auto",
  padding: "24px 16px 48px",
};

const cardStyle: CSSProperties = {
  background: "#ffffff",
  border: "1px solid #cbd5e1",
  borderRadius: "8px",
  padding: "16px",
  marginBottom: "16px",
};

const fieldStyle: CSSProperties = {
  display: "flex",
  gap: "8px",
  alignItems: "center",
  marginBottom: "8px",
};

const inputStyle: CSSProperties = {
  flex: 1,
  padding: "6px 8px",
  borderRadius: "8px",
  border: "1px solid #cbd5e1",
  fontSize: "14px",
};

const buttonStyle: CSSProperties = {
  padding: "6px 14px",
  borderRadius: "8px",
  border: "1px solid #2563eb",
  background: "#2563eb",
  color: "#ffffff",
  fontSize: "14px",
  cursor: "pointer",
};

const secondaryButton: CSSProperties = {
  ...buttonStyle,
  background: "#ffffff",
  color: "#2563eb",
};

const statusLine: CSSProperties = {
  marginTop: "8px",
  color: "#475569",
  fontSize: "12px",
  wordBreak: "break-all",
};

const summaryStyle: CSSProperties = {
  cursor: "pointer",
  fontWeight: 600,
  color: "#2563eb",
  marginBottom: "12px",
};

export function App() {
  const [mode, setMode] = useState<DemoMode>("demo");
  const [intent, setIntent] = useState("Analyze failure patterns");
  const [resourceRef, setResourceRef] = useState("submission:demo");
  const [liveToken, setLiveToken] = useState("reference-local-demo");
  const [runId, setRunId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [bridgeLog, setBridgeLog] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);

  const client = useMemo(() => {
    const options: AgentPlatformClientOptions = {
      baseUrl: "/api/agent-platform/",
      getAccessToken: () =>
        mode === "demo"
          ? "demo-local-token"
          : liveToken.trim() || "reference-local-demo",
    };
    if (mode === "demo") {
      options.fetchImpl = createMockFetch();
    }
    return new AgentPlatformClient(options);
  }, [liveToken, mode]);

  const hostBridge = useMemo<HostBridgeCapabilities>(
    () => ({
      schema_version: "host-bridge-capabilities/v1",
      navigate: async ({ destination_ref }) => {
        setBridgeLog((previous) => [
          ...previous.slice(-4),
          `navigate → ${destination_ref}`,
        ]);
      },
      downloadAuthorizedArtifact: async ({ authorization }) => {
        setBridgeLog((previous) => [
          ...previous.slice(-4),
          `download ← ${authorization.authorization_id} (${authorization.download_url})`,
        ]);
        window.open(authorization.download_url, "_blank", "noopener");
      },
    }),
    [],
  );

  const createRun = async (event: FormEvent): Promise<void> => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const snapshot = await client.createRun(
        {
          workflow_type: "synthetic-analysis",
          intent: intent.trim(),
          resource_refs: [resourceRef.trim()],
          parameters: {},
        },
        { idempotencyKey: createIdempotencyKey("create-run") },
      );
      setRunId(snapshot.run_id);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "create failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div style={pageStyle}>
      <h1 style={{ fontSize: "18px", margin: "0 0 4px" }}>
        AgentForge Embedded Host Example
      </h1>
      <div style={statusLine}>
        SDK: @platform/agent-ui-* · proxy: /api/agent-platform/ → backend (live
        mode) or in-app mock (demo mode)
      </div>

      <div style={cardStyle}>
        <div style={{ display: "flex", gap: "8px", marginBottom: "12px" }}>
          <button
            type="button"
            style={mode === "demo" ? buttonStyle : secondaryButton}
            onClick={() => setMode("demo")}
          >
            Demo (mock)
          </button>
          <button
            type="button"
            style={mode === "live" ? buttonStyle : secondaryButton}
            onClick={() => setMode("live")}
          >
            Live (backend)
          </button>
        </div>
        <details>
          <summary style={summaryStyle}>
            Advanced: create run manually
          </summary>
          <form onSubmit={(event) => void createRun(event)}>
            <div style={fieldStyle}>
              <label htmlFor="intent" style={{ minWidth: "90px" }}>
                intent
              </label>
              <input
                id="intent"
                style={inputStyle}
                value={intent}
                onChange={(event) => setIntent(event.target.value)}
              />
            </div>
            <div style={fieldStyle}>
              <label htmlFor="resource" style={{ minWidth: "90px" }}>
                resource_ref
              </label>
              <input
                id="resource"
                style={inputStyle}
                value={resourceRef}
                onChange={(event) => setResourceRef(event.target.value)}
              />
            </div>
            {mode === "live" ? (
              <div style={fieldStyle}>
                <label htmlFor="token" style={{ minWidth: "90px" }}>
                  token
                </label>
                <input
                  id="token"
                  style={inputStyle}
                  value={liveToken}
                  onChange={(event) => setLiveToken(event.target.value)}
                />
              </div>
            ) : null}
            <button type="submit" disabled={busy} style={buttonStyle}>
              {busy ? "Creating…" : "Create run"}
            </button>
          </form>
        </details>
        {error !== null ? (
          <div style={{ ...statusLine, color: "#dc2626" }}>{error}</div>
        ) : null}
        {runId !== null ? (
          <div style={statusLine}>run_id: {runId}</div>
        ) : null}
      </div>

      {bridgeLog.length > 0 ? (
        <div style={cardStyle}>
          <div style={{ fontWeight: 600, marginBottom: "4px" }}>Host Bridge log</div>
          {bridgeLog.map((line, index) => (
            <div key={`${index}-${line}`} style={statusLine}>
              {line}
            </div>
          ))}
        </div>
      ) : null}

      <AgentPlatformProvider client={client} hostBridge={hostBridge}>
        {runId === null ? (
          <div style={{ ...cardStyle, color: "#475569" }}>
            使用右下角 💬 Agent 浮窗发起自由对话，或用上方高级选项手动创建
            Run。Demo 模式下 mock 会回放完整参考工作流（progress → evidence
            → artifact → approval → effect → succeeded）。
          </div>
        ) : (
          <AgentPanel runId={runId} />
        )}
        <AgentLauncher onRunCreated={setRunId} />
      </AgentPlatformProvider>
    </div>
  );
}
