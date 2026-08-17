/**
 * Typed errors for the AgentForge public API.
 *
 * The platform returns a stable `api-error/v1` envelope for every non-2xx
 * response; the SDK surfaces it as AgentPlatformApiError and never parses the
 * English message as program state (see docs/embedding-guide.md §4.4).
 */
import {
  ApiErrorEnvelope,
  type ApiErrorEnvelope as ApiErrorEnvelopeType,
} from "@platform/agent-ui-protocol";

/** Stable error codes the SDK switches on (subset; codes are forward-open). */
export const API_ERROR_CODES = {
  RESYNC_REQUIRED: "RESYNC_REQUIRED",
  EVENT_CURSOR_AHEAD: "EVENT_CURSOR_AHEAD",
  VERSION_CONFLICT: "VERSION_CONFLICT",
  IDEMPOTENCY_KEY_REUSED: "IDEMPOTENCY_KEY_REUSED",
  IDEMPOTENCY_IN_PROGRESS: "IDEMPOTENCY_IN_PROGRESS",
  NOT_FOUND: "NOT_FOUND",
  FORBIDDEN: "FORBIDDEN",
  UNAUTHENTICATED: "UNAUTHENTICATED",
  STALE_UI_ACTION: "STALE_UI_ACTION",
  UI_ACTION_MISMATCH: "UI_ACTION_MISMATCH",
  UI_ACTION_DIGEST_MISMATCH: "UI_ACTION_DIGEST_MISMATCH",
  REQUEST_VALIDATION_FAILED: "REQUEST_VALIDATION_FAILED",
  HOST_PORT_UNAVAILABLE: "HOST_PORT_UNAVAILABLE",
} as const;

/** Server-side contract error (parsed from an `api-error/v1` envelope). */
export class AgentPlatformApiError extends Error {
  readonly schemaVersion = "api-error/v1" as const;

  constructor(
    readonly code: string,
    message: string,
    readonly traceId: string | null,
    readonly retryable: boolean,
    readonly details: Readonly<Record<string, string>>,
  ) {
    super(message);
    this.name = "AgentPlatformApiError";
  }

  static fromEnvelope(envelope: ApiErrorEnvelopeType): AgentPlatformApiError {
    return new AgentPlatformApiError(
      envelope.code,
      envelope.message,
      envelope.trace_id ?? null,
      envelope.retryable,
      envelope.details,
    );
  }
}

/** Transport-level failure (DNS, TCP, TLS, aborted fetch). */
export class AgentPlatformNetworkError extends Error {
  constructor(message = "network request failed", cause?: unknown) {
    super(message, cause === undefined ? undefined : { cause });
    this.name = "AgentPlatformNetworkError";
  }
}

/** Response body did not match the public contract; fail closed. */
export class AgentPlatformProtocolError extends Error {
  constructor(message = "response did not match the public contract") {
    super(message);
    this.name = "AgentPlatformProtocolError";
  }
}

export type SseErrorCode =
  | "http-error"
  | "invalid-content-type"
  | "missing-body"
  | "invalid-utf8"
  | "invalid-json"
  | "invalid-event"
  | "unsupported-event"
  | "event-too-large";

/** Malformed or unsafe SSE frame/stream. */
export class AgentPlatformSseError extends Error {
  constructor(readonly code: SseErrorCode) {
    super(code);
    this.name = "AgentPlatformSseError";
  }
}

/** True when the error signals the projection must be rebuilt from a snapshot. */
export function isResyncRequired(error: unknown): boolean {
  return (
    error instanceof AgentPlatformApiError &&
    error.code === API_ERROR_CODES.RESYNC_REQUIRED
  );
}

/** True when the error is an abort of the given controller. */
export function isAbortError(error: unknown, signal: AbortSignal): boolean {
  return signal.aborted;
}

/** Parse a JSON body strictly as an `api-error/v1` envelope, or throw a protocol error. */
export function parseApiError(value: unknown): AgentPlatformApiError {
  const result = ApiErrorEnvelope.safeParse(value);
  if (!result.success) {
    throw new AgentPlatformProtocolError("error body was not an api-error/v1 envelope");
  }
  return AgentPlatformApiError.fromEnvelope(result.data);
}
