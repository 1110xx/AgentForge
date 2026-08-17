/**
 * SSE parser tests: framing (CRLF/LF), heartbeat/comments, resync frames,
 * id/event_seq consistency, and fail-closed on malformed input.
 */
import { describe, expect, it } from "vitest";
import { AgentPlatformSseError } from "../src/errors.js";
import { parseAgentPlatformSse } from "../src/sse.js";

function stream(text: string): ReadableStream<Uint8Array> {
  return new Response(text).body as ReadableStream<Uint8Array>;
}

async function collect(text: string): Promise<unknown[]> {
  const events: unknown[] = [];
  for await (const event of parseAgentPlatformSse(stream(text))) {
    events.push(event);
  }
  return events;
}

function eventFrame(seq: number): string {
  return [
    `id: ${seq}`,
    "event: run.status.changed",
    `data: {"schema_version":"enterprise-event/v1","event_id":"evt_${seq}","tenant_id":"t","run_id":"run_demo","event_seq":${seq},"event_type":"run.status.changed","occurred_at":"2026-08-07T00:00:00Z","producer_service":"control-plane","payload_schema":"run-status/v1","payload":{"kind":"run.status.changed","previous":"QUEUED","current":"RUNNING"}}`,
    "",
    "",
  ].join("\n");
}

describe("parseAgentPlatformSse", () => {
  it("parses a CRLF-framed stream with id/event/data", async () => {
    const frames = [
      `id: 2\r\nevent: run.status.changed\r\ndata: {"schema_version":"enterprise-event/v1","event_id":"e2","tenant_id":"t","run_id":"run_demo","event_seq":2,"event_type":"run.status.changed","occurred_at":"2026-08-07T00:00:00Z","producer_service":"control-plane","payload_schema":"run-status/v1","payload":{"kind":"run.status.changed","previous":"QUEUED","current":"RUNNING"}}\r\n\r\n`,
    ];
    const events = await collect(frames.join(""));
    expect(events).toHaveLength(1);
    expect((events[0] as { event_seq: number }).event_seq).toBe(2);
  });

  it("parses LF-framed streams and ignores heartbeat comments", async () => {
    const text = `:heartbeat\n\n${eventFrame(3)}:heartbeat\n\n`;
    const events = await collect(text);
    expect(events.map((event) => (event as { event_seq: number }).event_seq)).toEqual([3]);
  });

  it("parses multiple events from one chunk", async () => {
    const events = await collect(`${eventFrame(2)}${eventFrame(3)}`);
    expect(events.map((event) => (event as { event_seq: number }).event_seq)).toEqual([2, 3]);
  });

  it("handles events split across chunk boundaries", async () => {
    const text = `${eventFrame(2)}${eventFrame(3)}`;
    const bytes = new TextEncoder().encode(text);
    const stream_ = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(bytes.slice(0, 40));
        controller.enqueue(bytes.slice(40, 80));
        controller.enqueue(bytes.slice(80));
        controller.close();
      },
    });
    const events: unknown[] = [];
    for await (const event of parseAgentPlatformSse(stream_)) {
      events.push(event);
    }
    expect(events).toHaveLength(2);
  });

  it("throws AgentPlatformApiError for platform.resync-required frames", async () => {
    const text = [
      "event: platform.resync-required",
      `data: {"schema_version":"api-error/v1","code":"RESYNC_REQUIRED","message":"event cursor precedes retention floor"}`,
      "",
      "",
    ].join("\n");
    await expect(collect(text)).rejects.toMatchObject({
      name: "AgentPlatformApiError",
      code: "RESYNC_REQUIRED",
    });
  });

  it("rejects invalid JSON payloads", async () => {
    const text = "id: 2\nevent: run.status.changed\ndata: {not-json}\n\n";
    await expect(collect(text)).rejects.toBeInstanceOf(AgentPlatformSseError);
  });

  it("rejects envelope whose id mismatches event_seq", async () => {
    const text = `id: 99\n${eventFrame(2).split("\n").slice(1).join("\n")}`;
    await expect(collect(text)).rejects.toMatchObject({ code: "invalid-event" });
  });

  it("rejects payloads over the byte limit", async () => {
    const big = `data: ${"x".repeat(70_000)}\n\n`;
    await expect(collect(big)).rejects.toMatchObject({ code: "event-too-large" });
  });

  it("rejects invalid UTF-8 bytes", async () => {
    const bytes = new Uint8Array([0x65, 0x76, 0x65, 0x6e, 0x74, 0x3a, 0x20, 0x78, 0xff, 0xfe]);
    const stream_ = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(bytes);
        controller.close();
      },
    });
    const iterator = parseAgentPlatformSse(stream_);
    await expect(iterator.next()).rejects.toMatchObject({ code: "invalid-utf8" });
  });
});
