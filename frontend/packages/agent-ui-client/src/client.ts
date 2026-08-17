/**
 * AgentForge public API client + run projection synchronizer.
 *
 * REST methods map 1:1 to the public /v1 router (docs/implementation.md §5);
 * every response is strict-parsed against the protocol Zod schemas. The SSE
 * stream is bounded, resync-aware and reconnectable; the projection
 * synchronizer drives a RunProjectionStore through the embeddding-guide §4.3
 * read flow (snapshot -> SSE from watermark -> REST replay for gaps ->
 * snapshot resync when the retention floor is crossed).
 */
import type {
  EnterpriseEventEnvelope,
  CreateRunCommand as CreateRunCommandType,
  RunViewSnapshot as RunViewSnapshotType,
  SurfaceRevision as SurfaceRevisionType,
} from "@platform/agent-ui-protocol";
import {
  ArtifactDownloadAuthorization,
  CreateRunCommand,
  RunEventPage,
  RunViewSnapshot,
  SurfaceRevision,
  UiActionCommand,
} from "@platform/agent-ui-protocol";
import {
  AgentPlatformApiError,
  AgentPlatformNetworkError,
  AgentPlatformProtocolError,
  isResyncRequired,
  parseApiError,
} from "./errors.js";
import type { RunProjectionStore } from "./projection.js";
import {
  parseAgentPlatformSse,
  requireEventStreamResponse,
} from "./sse.js";
import { createIdempotencyKey, delay, normalizeBaseUrl } from "./util.js";

export { RunProjectionStore } from "./projection.js";
export type {
  RunProjectionSnapshot,
} from "./projection.js";
export {
  AgentPlatformApiError,
  AgentPlatformNetworkError,
  AgentPlatformProtocolError,
  AgentPlatformSseError,
  API_ERROR_CODES,
  isResyncRequired,
  parseApiError,
} from "./errors.js";
export type { SseErrorCode } from "./errors.js";
export { parseAgentPlatformSse, parseSseFrame } from "./sse.js";
export type { SseFrame } from "./sse.js";
export { createIdempotencyKey } from "./util.js";

export type ClientDebugRecord =
  | { kind: "request"; method: string; path: string }
  | { kind: "response"; method: string; path: string; status: number }
  | { kind: "error"; method: string; path: string; code: string }
  | { kind: "sse"; runId: string; eventSeq: number };

export interface AgentPlatformClientOptions {
  /** Base URL of the public API, e.g. "/api/agent-platform/" (trailing slash optional). */
  baseUrl: string;
  /** Returns a short-lived token with audience=enterprise-agent-platform. */
  getAccessToken: () => string | Promise<string>;
  fetchImpl?: typeof fetch;
  onDebugRecord?: (record: ClientDebugRecord) => void;
}

export interface RequestOptions {
  signal?: AbortSignal;
}

export interface IdempotentRequestOptions extends RequestOptions {
  idempotencyKey?: string;
}

export interface CreateRunOptions extends IdempotentRequestOptions {
  command: CreateRunCommandType;
}

export interface RunEventsOptions extends RequestOptions {
  afterEventSeq: number;
  limit?: number;
}

export interface CancelRunOptions extends RequestOptions {
  expectedRunVersion: number;
  reason?: string;
}

export interface RecoverFailedEffectOptions extends IdempotentRequestOptions {
  expectedRunVersion: number;
}

export interface UiActionInput {
  surface_id: string;
  surface_revision: number;
  action_ref: string;
  displayed_digest?: string | null;
  host_context_ref?: string | null;
}

export interface SubmitActionOptions extends IdempotentRequestOptions {
  action: UiActionInput;
}

export interface SurfaceRevisionOptions extends RequestOptions {
  revision?: number;
}

function encode(value: string): string {
  return encodeURIComponent(value);
}

export class AgentPlatformClient {
  private readonly baseUrl: string;
  private readonly getAccessToken: () => string | Promise<string>;
  private readonly fetchImpl: typeof fetch;
  private readonly record: (record: ClientDebugRecord) => void;

  constructor(options: AgentPlatformClientOptions) {
    this.baseUrl = normalizeBaseUrl(options.baseUrl);
    this.getAccessToken = options.getAccessToken;
    this.fetchImpl = options.fetchImpl ?? globalThis.fetch.bind(globalThis);
    this.record = options.onDebugRecord ?? (() => undefined);
  }

  private url(path: string): string {
    return `${this.baseUrl}${path.replace(/^\//, "")}`;
  }

  private async authHeader(): Promise<string> {
    const token = await this.getAccessToken();
    return `Bearer ${token}`;
  }

  private async request<T>(
    method: string,
    path: string,
    options: {
      signal: AbortSignal | undefined;
      body?: unknown;
      headers?: Record<string, string>;
      parse: (value: unknown) => T;
    },
  ): Promise<T> {
    this.record({ kind: "request", method, path });
    const headers: Record<string, string> = {
      Accept: "application/json",
      Authorization: await this.authHeader(),
      ...options.headers,
    };
    const init: RequestInit = { method, headers };
    if (options.body !== undefined) {
      headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(options.body);
    }
    if (options.signal !== undefined) {
      init.signal = options.signal;
    }
    let response: Response;
    try {
      response = await this.fetchImpl(this.url(path), init);
    } catch (error) {
      if (options.signal?.aborted === true) {
        throw error;
      }
      throw new AgentPlatformNetworkError(`request to ${path} failed`, error);
    }
    this.record({ kind: "response", method, path, status: response.status });
    if (!response.ok) {
      throw await this.parseErrorResponse(response, method, path);
    }
    let value: unknown;
    try {
      value = await response.json();
    } catch {
      throw new AgentPlatformProtocolError(`response from ${path} was not JSON`);
    }
    const parsed = options.parse(value);
    this.record({ kind: "response", method, path, status: response.status });
    return parsed;
  }

  private async parseErrorResponse(
    response: Response,
    method: string,
    path: string,
  ): Promise<AgentPlatformApiError> {
    try {
      const value: unknown = await response.json();
      const error = parseApiError(value);
      this.record({ kind: "error", method, path, code: error.code });
      return error;
    } catch (error) {
      if (error instanceof AgentPlatformApiError) {
        throw error;
      }
      this.record({
        kind: "error",
        method,
        path,
        code: `HTTP_${response.status}`,
      });
      return new AgentPlatformApiError(
        `HTTP_${response.status}`,
        `request to ${path} failed with status ${response.status}`,
        null,
        response.status >= 500,
        {},
      );
    }
  }

  /** POST /v1/runs — create a run with a stable Idempotency-Key. */
  async createRun(
    command: CreateRunCommandType,
    options: IdempotentRequestOptions = {},
  ): Promise<RunViewSnapshotType> {
    const key = options.idempotencyKey ?? createIdempotencyKey("create-run");
    let parsed: CreateRunCommandType;
    try {
      parsed = CreateRunCommand.parse(command); // client-side contract check
    } catch {
      throw new AgentPlatformProtocolError(
        "create-run command did not satisfy the contract",
      );
    }
    return this.request<RunViewSnapshotType>("POST", "/v1/runs", {
      signal: options.signal,
      headers: { "Idempotency-Key": key },
      body: parsed,
      parse: (value) => this.parseRunViewSnapshot(value, "createRun"),
    });
  }

  /** GET /v1/runs/{run_id} — full snapshot and watermark. */
  async getRun(runId: string, options: RequestOptions = {}): Promise<RunViewSnapshotType> {
    return this.request<RunViewSnapshotType>("GET", `/v1/runs/${encode(runId)}`, {
      signal: options.signal,
      parse: (value) => this.parseRunViewSnapshot(value, "getRun"),
    });
  }

  /** GET /v1/runs/{run_id}/events — REST replay page. */
  async getRunEvents(
    runId: string,
    options: RunEventsOptions,
  ): Promise<RunEventPage> {
    if (options.afterEventSeq < 0) {
      throw new AgentPlatformProtocolError("afterEventSeq must be non-negative");
    }
    const params = new URLSearchParams({
      after_event_seq: String(options.afterEventSeq),
      limit: String(options.limit ?? 100),
    });
    return this.request<RunEventPage>(
      "GET",
      `/v1/runs/${encode(runId)}/events?${params.toString()}`,
      {
        signal: options.signal,
        parse: (value) => {
          const result = RunEventPage.safeParse(value);
          if (!result.success) {
            throw new AgentPlatformProtocolError("events response was not a run-event-page/v1");
          }
          return result.data;
        },
      },
    );
  }

  /** POST /v1/runs/{run_id}/cancel — optimistic-concurrency guarded cancel. */
  async cancelRun(
    runId: string,
    options: CancelRunOptions,
  ): Promise<RunViewSnapshotType> {
    if (options.expectedRunVersion < 1) {
      throw new AgentPlatformProtocolError("expectedRunVersion must be >= 1");
    }
    return this.request<RunViewSnapshotType>("POST", `/v1/runs/${encode(runId)}/cancel`, {
      signal: options.signal,
      headers: { "If-Match": `"${options.expectedRunVersion}"` },
      body: options.reason === undefined ? {} : { reason: options.reason },
      parse: (value) => this.parseRunViewSnapshot(value, "cancelRun"),
    });
  }

  /**
   * POST /v1/runs/{run_id}/actions — submit a Surface-bound UI action.
   * The backend requires client_action_id === Idempotency-Key; the SDK derives
   * both from the caller's stable key so the invariant cannot be violated.
   */
  async submitAction(
    runId: string,
    options: SubmitActionOptions,
  ): Promise<RunViewSnapshotType> {
    const key = options.idempotencyKey ?? createIdempotencyKey("ui-action");
    const command = UiActionCommand.parse({
      run_id: runId,
      client_action_id: key,
      surface_id: options.action.surface_id,
      surface_revision: options.action.surface_revision,
      action_ref: options.action.action_ref,
      displayed_digest: options.action.displayed_digest ?? null,
      host_context_ref: options.action.host_context_ref ?? null,
    });
    return this.request<RunViewSnapshotType>(
      "POST",
      `/v1/runs/${encode(runId)}/actions`,
      {
        signal: options.signal,
        headers: { "Idempotency-Key": key },
        body: command,
        parse: (value) => this.parseRunViewSnapshot(value, "submitAction"),
      },
    );
  }

  /**
   * POST /v1/runs/{run_id}/effects/{effect_id}/recover — recover a FAILED
   * effect (docs/embedding-guide.md §5.2). No automatic retry on conflicts.
   */
  async recoverFailedEffect(
    runId: string,
    effectId: string,
    options: RecoverFailedEffectOptions,
  ): Promise<RunViewSnapshotType> {
    if (options.expectedRunVersion < 1) {
      throw new AgentPlatformProtocolError("expectedRunVersion must be >= 1");
    }
    const key = options.idempotencyKey ?? createIdempotencyKey("recover-effect");
    const path = `/v1/runs/${encode(runId)}/effects/${encode(effectId)}/recover`;
    const snapshot = await this.request<RunViewSnapshotType>("POST", path, {
      signal: options.signal,
      headers: {
        "If-Match": `"${options.expectedRunVersion}"`,
        "Idempotency-Key": key,
      },
      body: {},
      parse: (value) => this.parseRunViewSnapshot(value, "recoverFailedEffect"),
    });
    if (snapshot.run_id !== runId) {
      throw new AgentPlatformProtocolError(
        "recoverFailedEffect response run_id does not match the request",
      );
    }
    return snapshot;
  }

  /** GET /v1/runs/{run_id}/surfaces/{surface_id} — immutable surface revision. */
  async getSurfaceRevision(
    runId: string,
    surfaceId: string,
    options: SurfaceRevisionOptions = {},
  ): Promise<SurfaceRevisionType> {
    const query =
      options.revision === undefined
        ? ""
        : `?revision=${encode(String(options.revision))}`;
    const path = `/v1/runs/${encode(runId)}/surfaces/${encode(surfaceId)}${query}`;
    const revision = await this.request<SurfaceRevisionType>("GET", path, {
      signal: options.signal,
      parse: (value) => {
        const result = SurfaceRevision.safeParse(value);
        if (!result.success) {
          throw new AgentPlatformProtocolError(
            "surface response was not a2ui-surface-revision/v0.9.1",
          );
        }
        return result.data;
      },
    });
    if (revision.run_id !== runId) {
      throw new AgentPlatformProtocolError(
        "surface revision run_id does not match the request",
      );
    }
    return revision;
  }

  /** GET .../download-authorization — short-lived artifact download token. */
  async getArtifactDownloadAuthorization(
    runId: string,
    artifactId: string,
    version: number,
    options: RequestOptions = {},
  ): Promise<ArtifactDownloadAuthorization> {
    if (version < 1) {
      throw new AgentPlatformProtocolError("artifact version must be >= 1");
    }
    return this.request<ArtifactDownloadAuthorization>(
      "GET",
      `/v1/runs/${encode(runId)}/artifacts/${encode(artifactId)}/versions/${version}/download-authorization`,
      {
        signal: options.signal,
        parse: (value) => {
          const result = ArtifactDownloadAuthorization.safeParse(value);
          if (!result.success) {
            throw new AgentPlatformProtocolError(
              "download authorization was not artifact-download-authorization/v1",
            );
          }
          return result.data;
        },
      },
    );
  }

  /** GET /v1/runs/{run_id}/events/stream — incremental SSE from a cursor. */
  async *streamRunEvents(
    runId: string,
    options: RequestOptions & { afterEventSeq: number },
  ): AsyncIterable<EnterpriseEventEnvelope> {
    if (options.afterEventSeq < 0) {
      throw new AgentPlatformProtocolError("afterEventSeq must be non-negative");
    }
    const path = `/v1/runs/${encode(runId)}/events/stream`;
    this.record({ kind: "request", method: "GET", path });
    let response: Response;
    try {
      const init: RequestInit = {
        method: "GET",
        headers: {
          Accept: "text/event-stream",
          Authorization: await this.authHeader(),
          "Last-Event-ID": String(options.afterEventSeq),
        },
      };
      if (options.signal !== undefined) {
        init.signal = options.signal;
      }
      response = await this.fetchImpl(this.url(path), init);
    } catch (error) {
      if (options.signal?.aborted === true) {
        throw error;
      }
      throw new AgentPlatformNetworkError(`SSE request to ${path} failed`, error);
    }
    this.record({ kind: "response", method: "GET", path, status: response.status });
    if (!response.ok) {
      const error = await this.parseErrorResponse(response, "GET", path);
      this.record({ kind: "error", method: "GET", path, code: error.code });
      throw error;
    }
    const body = requireEventStreamResponse(response);
    for await (const event of parseAgentPlatformSse(body, {
      onEvent: (value) => {
        this.record({ kind: "sse", runId, eventSeq: value.event_seq });
      },
    })) {
      yield event;
    }
  }

  private parseRunViewSnapshot(
    value: unknown,
    operation: string,
  ): RunViewSnapshotType {
    const result = RunViewSnapshot.safeParse(value);
    if (!result.success) {
      throw new AgentPlatformProtocolError(
        `${operation} response was not run-view-snapshot/v1`,
      );
    }
    return result.data;
  }
}

/* ------------------------------------------------------------------ */
/* Projection synchronizer                                             */
/* ------------------------------------------------------------------ */

export type RunSyncStatus =
  | "idle"
  | "connecting"
  | "streaming"
  | "replaying"
  | "resyncing"
  | "error"
  | "stopped";

export interface RunProjectionSynchronizerOptions {
  client: AgentPlatformClient;
  runId: string;
  store: RunProjectionStore;
  /** Fetch surface documents for newly committed revisions (default true). */
  refreshSurfaces?: boolean;
  maxReplayLimit?: number;
  retryBaseDelayMs?: number;
  /** Optional external abort; the synchronizer also exposes stop(). */
  signal?: AbortSignal;
}

/** Fetch documents for surface commits newer than the cached revision. */
export async function refreshSurfaceDocuments(
  client: AgentPlatformClient,
  runId: string,
  store: RunProjectionStore,
  options: RequestOptions = {},
): Promise<void> {
  const snapshot = store.getSnapshot();
  const pendingFetches: Array<{ surfaceId: string; revision: number }> = [];
  for (const [surfaceId, revision] of snapshot.surfaceCommits) {
    const cached = snapshot.surfaces.get(surfaceId);
    if (cached === undefined || cached.revision < revision) {
      pendingFetches.push({ surfaceId, revision });
    }
  }
  for (const { surfaceId, revision } of pendingFetches) {
    try {
      const request: SurfaceRevisionOptions = { revision };
      if (options.signal !== undefined) {
        request.signal = options.signal;
      }
      const document = await client.getSurfaceRevision(runId, surfaceId, request);
      store.setSurfaceRevision(document);
    } catch (error) {
      if (options.signal?.aborted === true) {
        throw error;
      }
      // Leave the commit pending; the caller may retry later.
    }
  }
}

/**
 * Drive a projection store through the embedding-guide §4.3 read flow:
 * snapshot -> SSE from the applied watermark -> REST replay for gaps ->
 * snapshot resync when the retention floor is crossed. Reconnects with the
 * same cursor after transient failures.
 */
export class RunProjectionSynchronizer {
  private readonly client: AgentPlatformClient;
  private readonly runId: string;
  private readonly store: RunProjectionStore;
  private readonly refreshSurfaces: boolean;
  private readonly maxReplayLimit: number;
  private readonly retryBaseDelayMs: number;
  private readonly controller = new AbortController();
  private started = false;
  private status: RunSyncStatus = "idle";
  private readonly listeners = new Set<() => void>();

  constructor(options: RunProjectionSynchronizerOptions) {
    this.client = options.client;
    this.runId = options.runId;
    this.store = options.store;
    this.refreshSurfaces = options.refreshSurfaces ?? true;
    this.maxReplayLimit = options.maxReplayLimit ?? 100;
    this.retryBaseDelayMs = options.retryBaseDelayMs ?? 250;
    if (options.signal?.aborted === true) {
      this.controller.abort();
    }
    options.signal?.addEventListener("abort", () => this.stop(), { once: true });
  }

  getStatus(): RunSyncStatus {
    return this.status;
  }

  onStatusChange(listener: () => void): () => void {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  }

  start(): void {
    if (this.started) {
      return;
    }
    this.started = true;
    void this.runLoop();
  }

  stop(): void {
    this.controller.abort();
  }

  private setStatus(status: RunSyncStatus): void {
    if (this.status === status) {
      return;
    }
    this.status = status;
    for (const listener of [...this.listeners]) {
      try {
        listener();
      } catch {
        // ignore faulty subscribers
      }
    }
  }

  private async runLoop(): Promise<void> {
    let attempt = 0;
    while (!this.controller.signal.aborted) {
      try {
        const snapshot = this.store.getSnapshot();
        if (snapshot.resyncRequired || snapshot.run === null) {
          await this.resync();
          attempt = 0;
          continue;
        }
        this.setStatus("connecting");
        for await (const event of this.client.streamRunEvents(this.runId, {
          afterEventSeq: this.store.getSnapshot().appliedWatermark,
          signal: this.controller.signal,
        })) {
          if (this.controller.signal.aborted) {
            return;
          }
          this.store.ingestEvent(event);
          this.setStatus("streaming");
        }
        // Stream ended cleanly (server lifetime deadline) — reconnect.
        this.setStatus("connecting");
        attempt = 0;
        await delay(this.retryBaseDelayMs, this.controller.signal);
      } catch (error) {
        if (this.controller.signal.aborted) {
          return;
        }
        if (isResyncRequired(error)) {
          this.store.markResyncRequired();
          continue;
        }
        attempt += 1;
        this.setStatus("error");
        await delay(
          Math.min(this.retryBaseDelayMs * attempt, 5_000),
          this.controller.signal,
        );
        continue;
      }
      await this.replayHoles();
      if (this.refreshSurfaces) {
        await this.refreshSurfaceDocuments();
      }
    }
    this.setStatus("stopped");
  }

  private async resync(): Promise<void> {
    this.setStatus("resyncing");
    const snapshot = await this.client.getRun(this.runId, {
      signal: this.controller.signal,
    });
    this.store.ingestSnapshot(snapshot);
    await this.replayHoles();
    if (this.refreshSurfaces) {
      await this.refreshSurfaceDocuments();
    }
  }

  private async replayHoles(): Promise<void> {
    while (
      !this.controller.signal.aborted &&
      this.store.needsReplay()
    ) {
      this.setStatus("replaying");
      const before = this.store.getSnapshot().appliedWatermark;
      const page = await this.client.getRunEvents(this.runId, {
        afterEventSeq: before,
        limit: this.maxReplayLimit,
        signal: this.controller.signal,
      });
      this.store.ingestPage(page);
      if (this.store.getSnapshot().appliedWatermark === before) {
        break; // no progress; avoid paging the same cursor forever
      }
    }
  }

  private async refreshSurfaceDocuments(): Promise<void> {
    await refreshSurfaceDocuments(
      this.client,
      this.runId,
      this.store,
      { signal: this.controller.signal },
    );
  }
}
