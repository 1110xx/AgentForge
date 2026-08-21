/**
 * useFollowupHistory — React hook for followup history management.
 *
 * Manages followup entries (question/answer pairs) for a given run.
 * - Loads history from server on mount (if loadOnMount=true)
 * - Optimistic UI: appends question immediately, updates with answer on response
 * - Errors are captured per-entry for retry
 * - Clears on unmount or runId change
 */
import { useCallback, useEffect, useRef, useState } from "react";
import type { AgentPlatformClient } from "@platform/agent-ui-client";
import { createIdempotencyKey } from "@platform/agent-ui-client";
import type {
  FollowupHistoryPage,
  FollowupRecord,
} from "@platform/agent-ui-protocol";

/* ------------------------------------------------------------------ */
/* Types                                                              */
/* ------------------------------------------------------------------ */

export type FollowupEntryStatus = "sending" | "done" | "error";

export interface FollowupEntry {
  followupSeq: number;
  question: string;
  answer: string | null; // null = pending
  answeredAt: string | null;
  clientFollowupId: string;
  status: FollowupEntryStatus;
}

export interface UseFollowupHistoryOptions {
  /** The run id to bind history to. */
  runId: string;
  /** The API client. */
  client: AgentPlatformClient;
  /** Load history from server on mount (default: true). */
  loadOnMount?: boolean;
}

export interface UseFollowupHistoryResult {
  /** Followup entries, sorted by seq ascending. */
  entries: FollowupEntry[];
  /** True while a send is in-flight. */
  sending: boolean;
  /** Last error message (null if no error). */
  error: string | null;
  /** Send a followup question (optimistic UI). */
  send: (question: string) => Promise<void>;
  /** Reload history from server. */
  reload: () => Promise<void>;
  /** Clear all local state. */
  clear: () => void;
}

/* ------------------------------------------------------------------ */
/* Helpers                                                             */
/* ------------------------------------------------------------------ */

function recordToEntry(
  record: FollowupRecord,
  status: FollowupEntryStatus = "done",
): FollowupEntry {
  return {
    followupSeq: record.followup_seq,
    question: record.question,
    answer: record.answer,
    answeredAt: record.answered_at,
    clientFollowupId: record.client_followup_id,
    status,
  };
}

function parseHistoryPage(
  page: FollowupHistoryPage,
): FollowupEntry[] {
  return page.records.map((r) => recordToEntry(r, "done"));
}

/* ------------------------------------------------------------------ */
/* Hook                                                                */
/* ------------------------------------------------------------------ */

export function useFollowupHistory(
  options: UseFollowupHistoryOptions,
): UseFollowupHistoryResult {
  const { runId, client, loadOnMount = true } = options;
  const [entries, setEntries] = useState<FollowupEntry[]>([]);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const mountedRef = useRef(true);
  const seqCounter = useRef(0);

  // Track mount state
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  // Load history on mount
  useEffect(() => {
    if (!loadOnMount) return;
    let cancelled = false;
    (async () => {
      try {
        const page = await client.listFollowups(runId);
        if (!cancelled && mountedRef.current) {
          setEntries(parseHistoryPage(page));
          setError(null);
        }
      } catch (cause) {
        if (!cancelled && mountedRef.current) {
          setError(
            cause instanceof Error ? cause.message : "Failed to load history",
          );
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [client, runId, loadOnMount]);

  const send = useCallback(
    async (question: string): Promise<void> => {
      if (!question.trim()) return;
      const clientFollowupId = createIdempotencyKey("followup");
      const seq = seqCounter.current++;
      const entry: FollowupEntry = {
        followupSeq: seq,
        question: question.trim(),
        answer: null,
        answeredAt: null,
        clientFollowupId,
        status: "sending",
      };
      setEntries((prev) => [...prev, entry]);
      setSending(true);
      setError(null);
      try {
        const answer = await client.submitFollowup(runId, question.trim(), {
          idempotencyKey: clientFollowupId,
        });
        if (!mountedRef.current) return;
        // Backend FollowupAnswer does not return client_followup_id or
        // answered_at; we use the locally-generated idempotency key and
        // current timestamp instead.
        setEntries((prev) =>
          prev.map((e) =>
            e.clientFollowupId === clientFollowupId
              ? {
                  ...e,
                  answer: answer.answer,
                  answeredAt: new Date().toISOString(),
                  status: "done" as const,
                }
              : e,
          ),
        );
      } catch (cause) {
        if (!mountedRef.current) return;
        const message =
          cause instanceof Error ? cause.message : "Followup failed";
        setError(message);
        setEntries((prev) =>
          prev.map((e) =>
            e.clientFollowupId === clientFollowupId
              ? { ...e, status: "error" as const }
              : e,
          ),
        );
      } finally {
        if (mountedRef.current) {
          setSending(false);
        }
      }
    },
    [client, runId],
  );

  const reload = useCallback(async () => {
    try {
      const page = await client.listFollowups(runId);
      if (mountedRef.current) {
        setEntries(parseHistoryPage(page));
        setError(null);
      }
    } catch (cause) {
      if (mountedRef.current) {
        setError(
          cause instanceof Error ? cause.message : "Failed to reload history",
        );
      }
    }
  }, [client, runId]);

  const clear = useCallback(() => {
    setEntries([]);
    setSending(false);
    setError(null);
    seqCounter.current = 0;
  }, []);

  return { entries, sending, error, send, reload, clear };
}