/**
 * Run projection store (docs/embedding-guide.md §4.3).
 *
 * Consumes the enterprise event stream with:
 *   - duplicate seq dedup (already-applied events are ignored);
 *   - out-of-order buffering (events beyond the contiguous watermark are
 *     held until the hole is filled by REST replay);
 *   - watermark tracking (applied / known / retention floor);
 *   - resync signalling when the retention floor is crossed or the
 *     out-of-order buffer overflows — the caller must rebuild from a
 *     RunViewSnapshot in that case.
 *
 * The store is pure (no I/O). The client synchronizer drives it.
 */
import type {
  EnterpriseEventEnvelope,
  RunEventPage,
  RunState,
  RunViewSnapshot,
  StreamChunk,
  SurfaceRevision,
} from "@platform/agent-ui-protocol";
import { AgentPlatformProtocolError } from "./errors.js";
import { deepFreeze } from "./util.js";

export const MAX_PENDING_EVENTS = 64;
export const MAX_RECENT_EVENTS = 200;
/** Bounded live-view chunk ring (SDD §11.5): never persisted, evicted oldest. */
export const MAX_LIVE_CHUNKS = 500;

export interface RunProjectionSnapshot {
  readonly runId: string;
  /** Last full RunViewSnapshot (null until the first snapshot/resync). */
  readonly run: RunViewSnapshot | null;
  /** Effective run state, driven by run.status.changed events (or the snapshot). */
  readonly runStatus: RunState;
  /** Highest contiguous event_seq applied. */
  readonly appliedWatermark: number;
  /** Highest event_seq the server has told us about. */
  readonly knownWatermark: number;
  /** Lowest event_seq the server can still replay. */
  readonly retentionFloor: number;
  /** True when the projection must be rebuilt from a snapshot. */
  readonly resyncRequired: boolean;
  readonly pendingCount: number;
  /** Bounded, most-recent-first-safe log of applied events (ascending seq). */
  readonly recentEvents: readonly EnterpriseEventEnvelope[];
  /**
   * Ephemeral live stream-chunks (SDD §11.5). UI-only buffer for the
   * typewriter/tool-activity view; NOT part of recentEvents and NEVER
   * replayed after a reconnect — durable turns come from
   * ``agent.turn.completed`` events in ``recentEvents`` instead.
   */
  readonly streamChunks: readonly StreamChunk[];
  /** Fetched surface documents keyed by surface_id. */
  readonly surfaces: ReadonlyMap<string, SurfaceRevision>;
  /** surface_id -> latest committed revision (from events and snapshots). */
  readonly surfaceCommits: ReadonlyMap<string, number>;
}

export class RunProjectionStore {
  private readonly runId: string;
  private run: RunViewSnapshot | null = null;
  private runStatus: RunState = "QUEUED";
  private appliedWatermark = 0;
  private knownWatermark = 0;
  private retentionFloor = 0;
  private resyncRequired = false;
  private readonly pending = new Map<number, EnterpriseEventEnvelope>();
  private recentEvents: EnterpriseEventEnvelope[] = [];
  private streamChunks: StreamChunk[] = [];
  private readonly surfaces = new Map<string, SurfaceRevision>();
  private readonly surfaceCommits = new Map<string, number>();
  private snapshot: RunProjectionSnapshot;
  private readonly listeners = new Set<() => void>();

  constructor(runId: string) {
    if (runId.trim() === "") {
      throw new Error("RunProjectionStore requires a non-empty run id");
    }
    this.runId = runId;
    this.snapshot = this.buildSnapshot();
  }

  getSnapshot = (): RunProjectionSnapshot => this.snapshot;

  /** Arrow property so React can call it as a bare function (useSyncExternalStore). */
  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };

  /** Rebuild the projection from a fresh snapshot (resync path). */
  ingestSnapshot(snapshot: RunViewSnapshot): void {
    if (snapshot.run_id !== this.runId) {
      throw new AgentPlatformProtocolError(
        "snapshot does not belong to this run projection",
      );
    }
    this.run = snapshot;
    this.runStatus = snapshot.status;
    this.appliedWatermark = snapshot.watermark;
    this.knownWatermark = snapshot.watermark;
    this.retentionFloor = 0;
    this.resyncRequired = false;
    this.pending.clear();
    // Resync rebuilds from durable state; the live chunk ring has no durable
    // equivalent and is cleared (the frontend re-renders turns from the
    // persisted agent.turn.completed events in recentEvents).
    this.streamChunks = [];
    for (const surface of snapshot.view.surfaces) {
      this.surfaceCommits.set(surface.surface_id, surface.revision);
    }
    this.commit();
  }

  /** Apply one event (dedup, buffer, or apply). */
  ingestEvent(event: EnterpriseEventEnvelope): void {
    if (event.run_id !== this.runId) {
      throw new AgentPlatformProtocolError(
        "event does not belong to this run projection",
      );
    }
    if (event.event_seq <= this.appliedWatermark) {
      return; // duplicate or already applied
    }
    if (event.event_seq > this.appliedWatermark + 1) {
      this.pending.set(event.event_seq, event);
      if (event.event_seq > this.knownWatermark) {
        this.knownWatermark = event.event_seq;
      }
      if (this.pending.size >= MAX_PENDING_EVENTS) {
        this.resyncRequired = true;
      }
      this.commit();
      return;
    }
    this.applySequenced(event);
  }

  /** Apply a replay page (updates floor/watermark, then ingests its events). */
  ingestPage(page: RunEventPage): void {
    if (page.run_id !== this.runId) {
      throw new AgentPlatformProtocolError(
        "replay page does not belong to this run projection",
      );
    }
    this.retentionFloor = page.retention_floor;
    if (page.watermark > this.knownWatermark) {
      this.knownWatermark = page.watermark;
    }
    if (this.appliedWatermark < this.retentionFloor) {
      this.resyncRequired = true;
    }
    for (const event of page.events) {
      this.ingestEvent(event);
    }
    this.commit();
  }

  /** True when buffered out-of-order events await a hole filled by replay. */
  needsReplay(): boolean {
    return this.pending.size > 0 && !this.resyncRequired;
  }

  /** Mark the projection as requiring a snapshot resync. */
  markResyncRequired(): void {
    if (!this.resyncRequired) {
      this.resyncRequired = true;
      this.commit();
    }
  }

  /**
   * Cache an ephemeral stream-chunk for the live view (SDD §11.5).
   * Bounded ring: oldest chunks are evicted as new ones arrive, and the
   * buffer is cleared on resync. Never persisted, never replayed.
   */
  ingestChunk(chunk: StreamChunk): void {
    if (chunk.run_id !== this.runId) {
      throw new AgentPlatformProtocolError(
        "stream chunk does not belong to this run projection",
      );
    }
    this.streamChunks.push(chunk);
    if (this.streamChunks.length > MAX_LIVE_CHUNKS) {
      this.streamChunks = this.streamChunks.slice(-MAX_LIVE_CHUNKS);
    }
    this.commit();
  }

  /** Cache a fetched surface document; bumps the commit to its revision. */
  setSurfaceRevision(revision: SurfaceRevision): void {
    if (revision.run_id !== this.runId) {
      throw new AgentPlatformProtocolError(
        "surface revision does not belong to this run projection",
      );
    }
    this.surfaces.set(revision.surface_id, revision);
    this.surfaceCommits.set(revision.surface_id, revision.revision);
    this.commit();
  }

  private applySequenced(event: EnterpriseEventEnvelope): void {
    this.appliedWatermark = event.event_seq;
    if (event.event_seq > this.knownWatermark) {
      this.knownWatermark = event.event_seq;
    }
    this.recordApplied(event);
    // Drain contiguous buffered events.
    let next = this.appliedWatermark + 1;
    let candidate = this.pending.get(next);
    while (candidate !== undefined) {
      this.pending.delete(next);
      this.appliedWatermark = next;
      this.recordApplied(candidate);
      next += 1;
      candidate = this.pending.get(next);
    }
    this.commit();
  }

  private recordApplied(event: EnterpriseEventEnvelope): void {
    if (event.payload.kind === "ui.surface.committed") {
      this.surfaceCommits.set(event.payload.surface_id, event.payload.revision);
    }
    if (event.payload.kind === "run.status.changed") {
      this.runStatus = event.payload.current;
    }
    this.recentEvents.push(event);
    if (this.recentEvents.length > MAX_RECENT_EVENTS) {
      this.recentEvents = this.recentEvents.slice(-MAX_RECENT_EVENTS);
    }
  }

  private buildSnapshot(): RunProjectionSnapshot {
    return deepFreeze({
      runId: this.runId,
      run: this.run,
      runStatus: this.runStatus,
      appliedWatermark: this.appliedWatermark,
      knownWatermark: this.knownWatermark,
      retentionFloor: this.retentionFloor,
      resyncRequired: this.resyncRequired,
      pendingCount: this.pending.size,
      recentEvents: [...this.recentEvents],
      streamChunks: [...this.streamChunks],
      surfaces: new Map(this.surfaces),
      surfaceCommits: new Map(this.surfaceCommits),
    });
  }

  private commit(): void {
    this.snapshot = this.buildSnapshot();
    for (const listener of [...this.listeners]) {
      try {
        listener();
      } catch {
        // A faulty subscriber must not corrupt projection ownership.
      }
    }
  }
}
