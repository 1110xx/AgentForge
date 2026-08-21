/**
 * agent-ui-react — React bindings for the AgentForge embedded panel.
 *
 * Composition (docs/embedding-guide.md §5.2):
 *   <AgentPlatformProvider client={client} hostBridge={hostBridge}>
 *     <AgentPanel runId="run_stable_id" />
 *   </AgentPlatformProvider>
 *
 * The provider owns the Host Bridge boundary: surfaces can only trigger the
 * three host capabilities (token, navigation, authorized downloads); the
 * browser Action command never carries approval_id or credentials
 * (docs/embedding-guide.md §5.3, §6.2).
 *
 * Phase 3: AgentPanel now embeds FollowupPanel at the bottom.
 */
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
  type CSSProperties,
  type ReactElement,
  type ReactNode,
} from "react";
import {
  RunProjectionStore,
  RunProjectionSynchronizer,
  createIdempotencyKey,
  type RunProjectionSnapshot,
} from "@platform/agent-ui-client";
import type { AgentPlatformClient } from "@platform/agent-ui-client";
import {
  EAP_THEME,
  renderSurfaceDocument,
  type ArtifactDownloadRequest,
  type SurfaceActionRequest,
  type SurfaceDocument,
  type SurfaceRenderContext,
} from "@platform/agent-ui-catalog";
import type { HostBridgeCapabilities } from "@platform/agent-ui-protocol/host";
import { FollowupPanel } from "./followup-panel.js";

export type { RunProjectionSnapshot } from "@platform/agent-ui-client";
export type {
  ArtifactDownloadRequest,
  SurfaceActionRequest,
} from "@platform/agent-ui-catalog";
export { EAP_THEME } from "@platform/agent-ui-catalog";
export type { HostBridgeCapabilities } from "@platform/agent-ui-protocol/host";
export { FollowupPanel } from "./followup-panel.js";
export {
  useFollowupHistory,
  type FollowupEntry,
} from "./use-followup-history.js";

/* ------------------------------------------------------------------ */
/* Provider                                                            */
/* ------------------------------------------------------------------ */

export interface AgentPlatformProviderProps {
  client: AgentPlatformClient;
  hostBridge: HostBridgeCapabilities;
  children?: ReactNode;
}

export interface AgentPlatformContextValue {
  client: AgentPlatformClient;
  hostBridge: HostBridgeCapabilities;
}

const AgentPlatformContext = createContext<AgentPlatformContextValue | null>(
  null,
);

export function AgentPlatformProvider({
  client,
  hostBridge,
  children,
}: AgentPlatformProviderProps): ReactElement {
  return (
    <AgentPlatformContext.Provider value={{ client, hostBridge }}>
      {children}
    </AgentPlatformContext.Provider>
  );
}

export function useAgentPlatform(): AgentPlatformContextValue {
  const value = useContext(AgentPlatformContext);
  if (value === null) {
    throw new Error(
      "useAgentPlatform must be used within AgentPlatformProvider",
    );
  }
  return value;
}

/* ------------------------------------------------------------------ */
/* Projection hook                                                     */
/* ------------------------------------------------------------------ */

/**
 * Subscribe to the persistent run projection for a run id. Starts the
 * snapshot -> SSE -> replay/resync synchronizer and tears it down on unmount
 * or run id change.
 */
export function useRunProjection(runId: string): RunProjectionSnapshot {
  const { client } = useAgentPlatform();
  const storeRef = useRef<RunProjectionStore | null>(null);
  if (storeRef.current === null || storeRef.current.getSnapshot().runId !== runId) {
    storeRef.current = new RunProjectionStore(runId);
  }
  const store = storeRef.current;
  const snapshot = useSyncExternalStore(
    store.subscribe,
    store.getSnapshot,
    store.getSnapshot,
  );
  useEffect(() => {
    const controller = new AbortController();
    const synchronizer = new RunProjectionSynchronizer({
      client,
      runId,
      store,
      signal: controller.signal,
    });
    synchronizer.start();
    return () => {
      controller.abort();
    };
  }, [client, runId, store]);
  return snapshot;
}

/* ------------------------------------------------------------------ */
/* AgentPanel                                                          */
/* ------------------------------------------------------------------ */

export interface AgentPanelProps {
  runId: string;
}

const panelStyle: CSSProperties = {
  width: "100%",
  maxWidth: "420px",
  boxSizing: "border-box",
  color: EAP_THEME.text,
  fontSize: "14px",
  fontFamily: "inherit",
};

const headerStyle: CSSProperties = {
  display: "flex",
  alignItems: "center",
  justifyContent: "space-between",
  gap: "8px",
  padding: "12px",
  borderBottom: `1px solid ${EAP_THEME.border}`,
};

const titleStyle: CSSProperties = {
  margin: 0,
  fontSize: "14px",
  fontWeight: 600,
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
};

const progressStyle: CSSProperties = {
  padding: "12px",
  borderBottom: `1px solid ${EAP_THEME.border}`,
};

const sectionStyle: CSSProperties = {
  padding: "4px 12px 12px",
};

const badgeStyle: CSSProperties = {
  padding: "2px 8px",
  borderRadius: EAP_THEME.radius,
  background: EAP_THEME.surface,
  border: `1px solid ${EAP_THEME.border}`,
  fontSize: "12px",
  fontWeight: 600,
  whiteSpace: "nowrap",
};

const loadingStyle: CSSProperties = {
  padding: "24px 12px",
  color: EAP_THEME.secondaryText,
  textAlign: "center",
};

export function AgentPanel({ runId }: AgentPanelProps): ReactElement | null {
  const { client, hostBridge } = useAgentPlatform();
  const projection = useRunProjection(runId);
  const [submitting, setSubmitting] = useState<ReadonlySet<string>>(
    () => new Set(),
  );

  const setSurfaceSubmitting = useCallback(
    (surfaceId: string, value: boolean) => {
      setSubmitting((previous) => {
        const next = new Set(previous);
        if (value) {
          next.add(surfaceId);
        } else {
          next.delete(surfaceId);
        }
        return next;
      });
    },
    [],
  );

  const handleAction = useCallback(
    async (request: SurfaceActionRequest): Promise<void> => {
      setSurfaceSubmitting(request.surface_id, true);
      try {
        await client.submitAction(runId, {
          action: {
            surface_id: request.surface_id,
            surface_revision: request.surface_revision,
            action_ref: request.action_ref,
            displayed_digest: request.displayed_digest ?? null,
          },
          idempotencyKey: createIdempotencyKey("ui-action"),
        });
      } finally {
        setSurfaceSubmitting(request.surface_id, false);
      }
    },
    [client, runId, setSurfaceSubmitting],
  );

  const handleDownload = useCallback(
    async (request: ArtifactDownloadRequest): Promise<void> => {
      const authorization = await client.getArtifactDownloadAuthorization(
        runId,
        request.artifact_id,
        request.version,
      );
      await hostBridge.downloadAuthorizedArtifact({ authorization });
    },
    [client, hostBridge, runId],
  );

  // runEnded checks both the snapshot status (from REST) and the event-derived
  // runStatus (from SSE), because the store only ingests full snapshots on
  // resync, while run.status.changed SSE events update runStatus directly.
  const runEnded = useMemo(
    () =>
      projection.run?.status === "SUCCEEDED" ||
      projection.run?.status === "FAILED" ||
      projection.run?.status === "CANCELLED" ||
      projection.runStatus === "SUCCEEDED" ||
      projection.runStatus === "FAILED" ||
      projection.runStatus === "CANCELLED",
    [projection.run?.status, projection.runStatus],
  );

  const effectSummary = useMemo(() => {
    // Extract summary from the first surface document title
    const surfaces = projection.surfaces;
    if (surfaces.size === 0) return undefined;
    const firstSurface = surfaces.values().next().value;
    if (!firstSurface) return undefined;
    const doc = firstSurface.document as unknown as {
      props?: { title?: string };
    };
    return doc?.props?.title;
  }, [projection.surfaces]);

  const run = projection.run;
  if (run === null) {
    return (
      <section style={panelStyle} data-agent-panel="loading">
        <div style={loadingStyle}>Loading run…</div>
      </section>
    );
  }

  const surfaces = [...projection.surfaces.entries()].sort(([a], [b]) =>
    a < b ? -1 : a > b ? 1 : 0,
  );

  return (
    <section style={panelStyle} data-agent-panel={run.run_id}>
      <div style={headerStyle}>
        <h2 style={titleStyle} title={run.view.intent}>
          {run.view.intent || run.run_id}
        </h2>
        <span style={badgeStyle}>{projection.runStatus}</span>
      </div>
      <div style={progressStyle}>
        {run.view.execution_units.map((unit) => (
          <div key={unit.execution_unit_id} style={{ display: "flex", gap: "8px" }}>
            <span>{unit.role}</span>
            <span style={{ color: EAP_THEME.secondaryText }}>{unit.status}</span>
          </div>
        ))}
        {run.view.attempts.map((attempt) => (
          <div key={attempt.attempt_id} style={{ display: "flex", gap: "8px" }}>
            <span>{attempt.attempt_id.slice(0, 12)}</span>
            <span style={{ color: EAP_THEME.secondaryText }}>{attempt.status}</span>
          </div>
        ))}
      </div>
      <div style={sectionStyle}>
        {surfaces.length === 0 ? (
          <div style={{ color: EAP_THEME.secondaryText, padding: "8px 0" }}>
            No surface published yet.
          </div>
        ) : (
          surfaces.map(([surfaceId, revision]) => {
            const ctx: SurfaceRenderContext = {
              runId,
              surface_id: surfaceId,
              surface_revision: revision.revision,
              submitting: submitting.has(surfaceId),
              onAction: handleAction,
              onDownloadAuthorizationRequest: handleDownload,
            };
            ctx.renderChild = (document: SurfaceDocument) =>
              renderSurfaceDocument(document, ctx);
            const document = revision.document as unknown as SurfaceDocument;
            return (
              <div key={surfaceId}>
                {renderSurfaceDocument(document, ctx)}
              </div>
            );
          })
        )}
      </div>

      {/* Phase 3: FollowupPanel */}
      <FollowupPanel
        runId={runId}
        runEnded={runEnded}
        effectSummary={effectSummary}
      />
    </section>
  );
}
