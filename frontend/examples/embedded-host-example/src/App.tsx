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

/* ------------------------------------------------------------------ */
/* Curator-style hero (layout language borrowed from the pi-web-access  */
/* search curator, remapped onto the EAP light-blue palette)            */
/* ------------------------------------------------------------------ */

const heroStyle: CSSProperties = {
  marginBottom: "20px",
};

const heroKickerStyle: CSSProperties = {
  margin: "0 0 6px",
  fontSize: "11px",
  fontWeight: 700,
  letterSpacing: "0.1em",
  textTransform: "uppercase",
  color: "#2563eb",
};

const heroTitleStyle: CSSProperties = {
  margin: "0 0 8px",
  fontSize: "28px",
  fontWeight: 700,
  letterSpacing: "-0.01em",
  lineHeight: 1.1,
  color: "#0f172a",
  textWrap: "balance",
};

const heroDescStyle: CSSProperties = {
  margin: "0 0 12px",
  maxWidth: "480px",
  fontSize: "14px",
  lineHeight: 1.5,
  color: "#475569",
};

const heroMetaStyle: CSSProperties = {
  display: "flex",
  alignItems: "center",
  flexWrap: "wrap",
  gap: "8px",
  fontSize: "13px",
  color: "#475569",
};

// Curator-style pill badge for the bound run / mode.
const chipStyle: CSSProperties = {
  padding: "2px 10px",
  borderRadius: "999px",
  background: "rgba(37, 99, 235, 0.10)",
  border: "1px solid rgba(37, 99, 235, 0.30)",
  color: "#2563eb",
  fontSize: "11px",
  fontWeight: 700,
  letterSpacing: "0.03em",
  textTransform: "uppercase",
  whiteSpace: "nowrap",
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
      <header style={heroStyle}>
        <p style={heroKickerStyle}>Embedded Host</p>
        <h1 style={heroTitleStyle}>Agent Platform</h1>
        <p style={heroDescStyle}>
          右下角 💬 Agent 浮窗即前端入口：消息经 POST /v1/chat 创建 Run，
          AgentPanel 自动绑定并实时跟随工作流。Demo 模式走内嵌 mock，Live
          模式直连真实后端（经 dev proxy /api/agent-platform/）。
        </p>
        <div style={heroMetaStyle}>
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
          {runId !== null ? (
            <span style={chipStyle}>run {runId.slice(0, 8)}…</span>
          ) : null}
        </div>
      </header>

      <div style={cardStyle}>
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
            <p
              style={{
                ...heroKickerStyle,
                margin: "0 0 4px",
              }}
            >
              Getting Started
            </p>
            <div style={{ fontWeight: 600, marginBottom: "6px" }}>
              用浮窗发起一次对话
            </div>
            <div style={statusLine}>
              点击右下角 💬 Agent，输入任意问题（如“分析日志中的故障模式”）后
              发送——Demo 模式 mock 回放完整参考工作流（progress → evidence
              → artifact → approval → effect → succeeded）；Live 模式则由真实
              后端创建 Run 并返回 201 + run_id。
            </div>
          </div>
        ) : (
          <AgentPanel runId={runId} />
        )}
        <AgentLauncher onRunCreated={setRunId} />
      </AgentPlatformProvider>
    </div>
  );
}
