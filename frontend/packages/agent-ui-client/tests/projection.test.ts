/**
 * RunProjectionStore tests: dedup, out-of-order buffering, replay draining,
 * retention-floor resync and snapshot resync.
 */
import { describe, expect, it } from "vitest";
import type {
  EnterpriseEventEnvelope,
  RunEventPage,
  RunViewSnapshot,
  StreamChunk,
} from "@platform/agent-ui-protocol";
import { MAX_LIVE_CHUNKS, RunProjectionStore } from "../src/projection.js";

function chunk(
  kind: StreamChunk["kind"],
  overrides: Partial<StreamChunk> = {},
): StreamChunk {
  return { run_id: "run_demo", kind, ...overrides };
}


let seqCounter = 0;

function envelope(overrides: Partial<EnterpriseEventEnvelope> = {}): EnterpriseEventEnvelope {
  seqCounter += 1;
  return {
    schema_version: "enterprise-event/v1",
    event_id: `evt_${seqCounter}`,
    tenant_id: "t",
    run_id: "run_demo",
    event_seq: seqCounter,
    event_type: "run.status.changed",
    occurred_at: "2026-08-07T00:00:00Z",
    producer_service: "control-plane",
    payload_schema: "run-status/v1",
    payload: { kind: "run.status.changed", previous: "QUEUED", current: "RUNNING" },
    attempt_id: null,
    causation_event_id: null,
    trace_id: null,
    ...overrides,
  };
}

function surfaceEvent(seq: number, revision: number): EnterpriseEventEnvelope {
  return envelope({
    event_seq: seq,
    event_type: "ui.surface.committed",
    payload_schema: "a2ui-surface/v0.9.1",
    payload: { kind: "ui.surface.committed", surface_id: "surface_summary", revision },
  });
}

function page(
  afterEventSeq: number,
  events: EnterpriseEventEnvelope[],
  extra: Partial<RunEventPage> = {},
): RunEventPage {
  return {
    schema_version: "run-event-page/v1",
    run_id: "run_demo",
    after_event_seq: afterEventSeq,
    watermark: events.length === 0 ? afterEventSeq : events[events.length - 1]?.event_seq ?? afterEventSeq,
    retention_floor: 0,
    resync_required: false,
    events,
    ...extra,
  };
}

describe("RunProjectionStore dedup and ordering", () => {
  it("applies contiguous events and advances the watermark", () => {
    const store = new RunProjectionStore("run_demo");
    store.ingestEvent(envelope({ event_seq: 1 }));
    store.ingestEvent(envelope({ event_seq: 2 }));
    const snap = store.getSnapshot();
    expect(snap.appliedWatermark).toBe(2);
    expect(snap.recentEvents.map((event) => event.event_seq)).toEqual([1, 2]);
  });

  it("drops duplicate and stale seqs", () => {
    const store = new RunProjectionStore("run_demo");
    store.ingestEvent(envelope({ event_seq: 1 }));
    store.ingestEvent(envelope({ event_seq: 1 })); // duplicate
    store.ingestEvent(envelope({ event_seq: 0 })); // stale
    expect(store.getSnapshot().appliedWatermark).toBe(1);
    expect(store.getSnapshot().recentEvents).toHaveLength(1);
  });

  it("buffers out-of-order events and drains them once the hole fills", () => {
    const store = new RunProjectionStore("run_demo");
    store.ingestEvent(envelope({ event_seq: 3 }));
    store.ingestEvent(envelope({ event_seq: 2 }));
    expect(store.getSnapshot().appliedWatermark).toBe(0);
    expect(store.getSnapshot().pendingCount).toBe(2);
    expect(store.needsReplay()).toBe(true);

    store.ingestEvent(envelope({ event_seq: 1 }));
    const snap = store.getSnapshot();
    expect(snap.appliedWatermark).toBe(3);
    expect(snap.pendingCount).toBe(0);
    expect(snap.recentEvents.map((event) => event.event_seq)).toEqual([1, 2, 3]);
  });

  it("marks resync required when the pending buffer overflows", () => {
    const store = new RunProjectionStore("run_demo");
    for (let seq = 2; seq <= 66; seq += 1) {
      store.ingestEvent(envelope({ event_seq: seq }));
    }
    expect(store.getSnapshot().resyncRequired).toBe(true);
  });
});

describe("RunProjectionStore pages and retention floor", () => {
  it("ingests a replay page and updates the retention floor", () => {
    const store = new RunProjectionStore("run_demo");
    store.ingestEvent(envelope({ event_seq: 1 }));
    store.ingestPage(
      page(1, [envelope({ event_seq: 2 }), envelope({ event_seq: 3 })], {
        watermark: 5,
        retention_floor: 1,
      }),
    );
    const snap = store.getSnapshot();
    expect(snap.appliedWatermark).toBe(3);
    expect(snap.knownWatermark).toBe(5);
    expect(snap.retentionFloor).toBe(1);
    expect(snap.resyncRequired).toBe(false);
  });

  it("signals resync when the retention floor passes the applied watermark", () => {
    const store = new RunProjectionStore("run_demo");
    store.ingestPage(
      page(1, [envelope({ event_seq: 2 })], {
        watermark: 9,
        retention_floor: 7,
      }),
    );
    expect(store.getSnapshot().resyncRequired).toBe(true);
  });

  it("marks resync on explicit signal", () => {
    const store = new RunProjectionStore("run_demo");
    store.markResyncRequired();
    expect(store.getSnapshot().resyncRequired).toBe(true);
  });

  it("rejects pages from another run", () => {
    const store = new RunProjectionStore("run_demo");
    expect(() =>
      store.ingestPage({ ...page(0, []), run_id: "other_run" }),
    ).toThrow();
  });
});

describe("RunProjectionStore snapshots and surfaces", () => {
  const snapshot = (watermark: number, surfaces: RunViewSnapshot["view"]["surfaces"] = []): RunViewSnapshot => ({
    schema_version: "run-view-snapshot/v1",
    run_id: "run_demo",
    status: "RUNNING",
    watermark,
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
      execution_units: [],
      attempts: [],
      current_step: null,
      approvals: [],
      artifacts: [],
      surfaces,
      watermark,
    },
  });

  it("rebuilds the projection from a snapshot and seeds surface commits", () => {
    const store = new RunProjectionStore("run_demo");
    store.ingestEvent(envelope({ event_seq: 1 }));
    store.ingestSnapshot(
      snapshot(8, [{ surface_id: "surface_summary", catalog_id: "public-catalog", revision: 1 }]),
    );
    const snap = store.getSnapshot();
    expect(snap.appliedWatermark).toBe(8);
    expect(snap.resyncRequired).toBe(false);
    expect(snap.surfaceCommits.get("surface_summary")).toBe(1);
  });

  it("tracks ui.surface.committed events and caches fetched documents", () => {
    const store = new RunProjectionStore("run_demo");
    store.ingestEvent(envelope({ event_seq: 1 }));
    store.ingestEvent(surfaceEvent(2, 1));
    expect(store.getSnapshot().surfaceCommits.get("surface_summary")).toBe(1);
    store.setSurfaceRevision({
      schema_version: "a2ui-surface-revision/v0.9.1",
      surface_id: "surface_summary",
      run_id: "run_demo",
      revision: 1,
      source_attempt_id: "attempt_001",
      source_event_seq: 2,
      document: { component: "EvidenceSummary", props: {} },
      checksum: "sha256:abc",
    });
    expect(store.getSnapshot().surfaces.get("surface_summary")?.revision).toBe(1);
  });

  it("notifies subscribers on change", () => {
    const store = new RunProjectionStore("run_demo");
    let calls = 0;
    const unsubscribe = store.subscribe(() => {
      calls += 1;
    });
    store.ingestEvent(envelope({ event_seq: 1 }));
    expect(calls).toBe(1);
    unsubscribe();
    store.ingestEvent(envelope({ event_seq: 2 }));
    expect(calls).toBe(1);
  });

  it("tracks runStatus from run.status.changed events", () => {
    const store = new RunProjectionStore("run_demo");
    store.ingestEvent(
      envelope({
        event_seq: 1,
        payload: { kind: "run.status.changed", previous: "QUEUED", current: "RUNNING" },
      }),
    );
    expect(store.getSnapshot().runStatus).toBe("RUNNING");
    store.ingestEvent(
      envelope({
        event_seq: 2,
        payload: { kind: "run.status.changed", previous: "RUNNING", current: "WAITING_APPROVAL" },
      }),
    );
    expect(store.getSnapshot().runStatus).toBe("WAITING_APPROVAL");
  });

  it("freezes snapshots so consumers cannot mutate projection state", () => {
    const store = new RunProjectionStore("run_demo");
    store.ingestEvent(envelope({ event_seq: 1 }));
    const snap = store.getSnapshot();
    expect(Object.isFrozen(snap)).toBe(true);
    expect(Object.isFrozen(snap.recentEvents)).toBe(true);
  });
});

describe("RunProjectionStore live stream-chunks (SDD §11.5)", () => {
  it("buffers ephemeral chunks without touching the event watermark or recentEvents", () => {
    const store = new RunProjectionStore("run_demo");
    store.ingestChunk(chunk("thinking.delta", { delta: "a" }));
    store.ingestChunk(chunk("text.delta", { delta: "b" }));
    const snap = store.getSnapshot();
    expect(snap.streamChunks.map((item) => item.kind)).toEqual([
      "thinking.delta",
      "text.delta",
    ]);
    expect(snap.appliedWatermark).toBe(0);
    expect(snap.recentEvents).toHaveLength(0);
  });

  it("rejects chunks from another run", () => {
    const store = new RunProjectionStore("run_demo");
    expect(() =>
      store.ingestChunk(chunk("text.delta", { run_id: "other_run" })),
    ).toThrow();
  });

  it("bounds the live buffer and evicts the oldest chunks", () => {
    const store = new RunProjectionStore("run_demo");
    for (let index = 0; index < MAX_LIVE_CHUNKS + 25; index += 1) {
      store.ingestChunk(chunk("text.delta", { delta: String(index) }));
    }
    const snap = store.getSnapshot();
    expect(snap.streamChunks).toHaveLength(MAX_LIVE_CHUNKS);
    expect(snap.streamChunks[0]?.delta).toBe("25"); // oldest 25 evicted
  });

  it("clears the live buffer on snapshot resync", () => {
    const store = new RunProjectionStore("run_demo");
    store.ingestChunk(chunk("thinking.delta", { delta: "live" }));
    expect(store.getSnapshot().streamChunks).toHaveLength(1);
    store.ingestSnapshot({
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
        execution_units: [],
        attempts: [],
        current_step: null,
        approvals: [],
        artifacts: [],
        surfaces: [],
        watermark: 4,
      },
    });
    expect(store.getSnapshot().streamChunks).toHaveLength(0);
  });

  it("keeps recentEvents durable-only (chunks never enter the replay log)", () => {
    const store = new RunProjectionStore("run_demo");
    store.ingestEvent(envelope({ event_seq: 1 }));
    store.ingestChunk(chunk("text.delta", { delta: "x" }));
    store.ingestEvent(envelope({ event_seq: 2 }));
    expect(store.getSnapshot().recentEvents.map((event) => event.event_seq)).toEqual([
      1, 2,
    ]);
  });
});
