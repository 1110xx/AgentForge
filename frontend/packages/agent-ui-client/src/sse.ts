/**
 * Strict SSE parser for the AgentForge run event stream.
 *
 * The backend frames every event as:
 *   id: {event_seq}\nevent: {event_type}\ndata: {envelope json}\n\n
 * plus `event: platform.resync-required` frames and `:heartbeat` comments
 * (see backend fastapi/sse.py). Every data payload is strict-parsed against
 * the protocol schemas; anything unexpected fails closed.
 */
import {
  ApiErrorEnvelope,
  EnterpriseEventEnvelope,
  StreamChunk,
} from "@platform/agent-ui-protocol";

/** One parsed SSE frame: a durable enterprise event or an ephemeral chunk. */
export type SseEvent = EnterpriseEventEnvelope | StreamChunk;
import {
  AgentPlatformApiError,
  AgentPlatformSseError,
  type SseErrorCode,
} from "./errors.js";

export const MAX_SSE_EVENT_BYTES = 65_536;

export interface SseFrame {
  readonly id: string | null;
  readonly event: string | null;
  readonly data: string | null;
}

/** Split one SSE frame into its id/event/data fields (comments ignored). */
export function parseSseFrame(frame: string): SseFrame {
  let id: string | null = null;
  let event: string | null = null;
  const dataLines: string[] = [];
  for (const line of frame.split(/\r?\n/)) {
    if (line.startsWith(":")) {
      continue; // comment / heartbeat
    }
    if (line.startsWith("id:")) {
      id = line.slice(3).trim();
    } else if (line.startsWith("event:")) {
      event = line.slice(6).trim();
    } else if (line.startsWith("data:")) {
      const value = line.slice(5);
      dataLines.push(value.startsWith(" ") ? value.slice(1) : value);
    }
  }
  return {
    id,
    event,
    data: dataLines.length === 0 ? null : dataLines.join("\n"),
  };
}

interface FrameBoundary {
  readonly index: number;
  readonly length: number;
}

function nextFrameBoundary(buffer: string): FrameBoundary | null {
  const lf = buffer.indexOf("\n\n");
  const crlf = buffer.indexOf("\r\n\r\n");
  if (lf === -1 && crlf === -1) return null;
  if (lf === -1) return { index: crlf, length: 4 };
  if (crlf === -1 || lf < crlf) return { index: lf, length: 2 };
  return { index: crlf, length: 4 };
}

function requireWithinLimit(value: string, limit: number): void {
  if (new TextEncoder().encode(value).byteLength > limit) {
    throw new AgentPlatformSseError("event-too-large");
  }
}

function decodePayload(frame: SseFrame, limit: number): SseEvent {
  requireWithinLimit(frame.data ?? "", limit);
  const data = frame.data;
  if (data === null) {
    throw new AgentPlatformSseError("invalid-event");
  }
  let value: unknown;
  try {
    value = JSON.parse(data);
  } catch {
    throw new AgentPlatformSseError("invalid-json");
  }
  if (frame.event === "stream-chunk") {
    // Ephemeral live delta (SDD §11.5): no event_seq, no id, never replayed.
    const chunk = StreamChunk.safeParse(value);
    if (!chunk.success) {
      throw new AgentPlatformSseError("invalid-event");
    }
    return chunk.data;
  }
  if (frame.event === "platform.resync-required") {
    const envelope = ApiErrorEnvelope.safeParse(value);
    if (!envelope.success) {
      throw new AgentPlatformSseError("invalid-event");
    }
    throw AgentPlatformApiError.fromEnvelope(envelope.data);
  }
  if (frame.event !== null && !frame.event.startsWith("platform.")) {
    // The stream only carries registered enterprise event types plus
    // platform.* / stream-chunk control frames; anything else is unsupported.
    const envelope = EnterpriseEventEnvelope.safeParse(value);
    if (!envelope.success) {
      throw new AgentPlatformSseError("invalid-event");
    }
    return envelope.data;
  }
  // No event: field on a data frame — treat the payload as an envelope.
  const envelope = EnterpriseEventEnvelope.safeParse(value);
  if (!envelope.success) {
    throw new AgentPlatformSseError("invalid-event");
  }
  return envelope.data;
}

export interface ParseSseOptions {
  readonly maxEventBytes?: number;
  readonly onEvent?: (event: EnterpriseEventEnvelope) => void;
  readonly onChunk?: (chunk: StreamChunk) => void;
}

/**
 * Consume a response body as a strict AgentForge SSE stream.
 * Yields enterprise events; throws AgentPlatformApiError for
 * `platform.resync-required` frames and AgentPlatformSseError for framing
 * violations.
 */
export async function* parseAgentPlatformSse(
  body: ReadableStream<Uint8Array>,
  options: ParseSseOptions = {},
): AsyncIterable<SseEvent> {
  const reader = body.getReader();
  const decoder = new TextDecoder("utf-8", { fatal: true });
  const limit = options.maxEventBytes ?? MAX_SSE_EVENT_BYTES;
  let buffer = "";
  let exhausted = false;
  try {
    while (true) {
      const { value, done } = await reader.read();
      try {
        buffer += done ? decoder.decode() : decoder.decode(value, { stream: true });
      } catch {
        throw new AgentPlatformSseError("invalid-utf8");
      }
      while (true) {
        const boundary = nextFrameBoundary(buffer);
        if (boundary === null) break;
        const current = buffer.slice(0, boundary.index);
        buffer = buffer.slice(boundary.index + boundary.length);
        const frame = parseSseFrame(current);
        if (frame.data === null) continue; // comment/heartbeat-only frame
        const payload = decodePayload(frame, limit);
        if ("event_seq" in payload) {
          // Durable enterprise event (SDD §11.4) — id must match its seq.
          if (frame.id !== null && frame.id !== String(payload.event_seq)) {
            throw new AgentPlatformSseError("invalid-event");
          }
          options.onEvent?.(payload);
        } else {
          // Ephemeral stream-chunk (SDD §11.5) — live view only.
          options.onChunk?.(payload);
        }
        yield payload;
        requireWithinLimit(buffer, limit);
      }
      if (done) break;
    }
    if (buffer.trim() !== "") {
      const frame = parseSseFrame(buffer);
      if (frame.data !== null) {
        const payload = decodePayload(frame, limit);
        if ("event_seq" in payload) {
          if (frame.id !== null && frame.id !== String(payload.event_seq)) {
            throw new AgentPlatformSseError("invalid-event");
          }
          options.onEvent?.(payload);
        } else {
          options.onChunk?.(payload);
        }
        yield payload;
      }
    }
    exhausted = true;
  } catch (error) {
    if (
      error instanceof AgentPlatformApiError ||
      error instanceof AgentPlatformSseError
    ) {
      throw error;
    }
    throw new AgentPlatformSseError("invalid-event");
  } finally {
    if (!exhausted) {
      await reader.cancel().catch(() => undefined);
    }
    reader.releaseLock();
  }
}

/** Validate the response before streaming; returns the response body. */
export function requireEventStreamResponse(response: Response): ReadableStream<Uint8Array> {
  if (!response.ok) {
    throw new AgentPlatformSseError("http-error");
  }
  const contentType = response.headers
    .get("Content-Type")
    ?.split(";", 1)[0]
    ?.trim()
    ?.toLowerCase();
  if (contentType !== "text/event-stream") {
    throw new AgentPlatformSseError("invalid-content-type");
  }
  if (response.body === null) {
    throw new AgentPlatformSseError("missing-body");
  }
  return response.body;
}

export function sseErrorCode(error: unknown): SseErrorCode | null {
  return error instanceof AgentPlatformSseError ? error.code : null;
}
