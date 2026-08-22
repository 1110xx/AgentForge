/**
 * useAgentChat — React hook for free-form conversation entry (frontend
 * launcher, Phase 3.6 F-B).
 *
 * Sends natural-language messages to POST /v1/chat, records the created Run
 * id per message, and reports fresh runs to the host via onRunCreated so the
 * panel can bind its projection synchronizer to the new run. The message is
 * kept verbatim as the Run intent (backend classify_intent keeps it), entries
 * are optimistic (sending -> created/error), blank messages are never sent,
 * and each message gets its own idempotency key (retrying the same text
 * reuses the same Run via the backend digest).
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { createIdempotencyKey } from "@platform/agent-ui-client";
import { useAgentPlatform } from "./index.js";
import type { RunViewSnapshot } from "@platform/agent-ui-protocol";

/* ------------------------------------------------------------------ */
/* Types                                                              */
/* ------------------------------------------------------------------ */

export type ChatEntryStatus = "sending" | "created" | "error";

export interface ChatEntry {
  /** Client-side entry id; doubles as the Idempotency-Key for POST /v1/chat. */
  id: string;
  /** Trimmed user message; preserved verbatim as the Run intent. */
  text: string;
  /** Created Run id (null while sending or on failure). */
  runId: string | null;
  status: ChatEntryStatus;
  /** Last error message (null unless status === "error"). */
  error: string | null;
}

export interface SendChatOptions {
  /** Override the default resource refs for this message. */
  resourceRefs?: string[];
  /** Explicit workflow escape hatch (backend uses it verbatim). */
  workflowHint?: string;
}

export interface UseAgentChatOptions {
  /** Called once for every newly created Run. */
  onRunCreated?: ((runId: string) => void) | undefined;
}

export interface UseAgentChatResult {
  /** Chat entries, oldest first. */
  entries: ChatEntry[];
  /** True while a send is in-flight. */
  sending: boolean;
  /** Send a message; blank messages are ignored. */
  send: (text: string, options?: SendChatOptions) => Promise<void>;
  /** Clear all entries. */
  clear: () => void;
}

/* ------------------------------------------------------------------ */
/* Hook                                                                */
/* ------------------------------------------------------------------ */

export function useAgentChat(
  options: UseAgentChatOptions = {},
): UseAgentChatResult {
  const { client } = useAgentPlatform();
  const [entries, setEntries] = useState<ChatEntry[]>([]);
  const [sending, setSending] = useState(false);
  const mountedRef = useRef(true);
  const sendingRef = useRef(false);
  const onRunCreatedRef = useRef(options.onRunCreated);
  onRunCreatedRef.current = options.onRunCreated;

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const clear = useCallback(() => {
    setEntries([]);
    setSending(false);
  }, []);

  const send = useCallback(
    async (text: string, sendOptions: SendChatOptions = {}): Promise<void> => {
      const trimmed = text.trim();
      if (!trimmed || sendingRef.current) {
        return;
      }
      const id = createIdempotencyKey("chat");
      const entry: ChatEntry = {
        id,
        text: trimmed,
        runId: null,
        status: "sending",
        error: null,
      };
      setEntries((previous) => [...previous, entry]);
      setSending(true);
      sendingRef.current = true;
      try {
        const snapshot: RunViewSnapshot = await client.chat({
          message: trimmed,
          ...(sendOptions.resourceRefs !== undefined
            ? { resource_refs: sendOptions.resourceRefs }
            : {}),
          ...(sendOptions.workflowHint !== undefined
            ? { workflow_hint: sendOptions.workflowHint }
            : {}),
        });
        if (!mountedRef.current) {
          return;
        }
        setEntries((previous) =>
          previous.map((e) =>
            e.id === id
              ? { ...e, runId: snapshot.run_id, status: "created" as const }
              : e,
          ),
        );
        onRunCreatedRef.current?.(snapshot.run_id);
      } catch (cause) {
        if (!mountedRef.current) {
          return;
        }
        const message =
          cause instanceof Error ? cause.message : "Chat message failed";
        setEntries((previous) =>
          previous.map((e) =>
            e.id === id ? { ...e, status: "error" as const, error: message } : e,
          ),
        );
      } finally {
        sendingRef.current = false;
        if (mountedRef.current) {
          setSending(false);
        }
      }
    },
    [client],
  );

  return { entries, sending, send, clear };
}