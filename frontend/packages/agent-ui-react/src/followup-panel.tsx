/**
 * FollowupPanel — Embedded followup question/answer panel for AgentPanel.
 *
 * DESIGN.md v2 design system:
 * - Collapsible panel, hidden until run ends (SUCCEEDED/FAILED/CANCELLED)
 * - Shows effect summary, history of Q&A pairs, and input box
 * - Uses CSS variables (--agent-*) for theming, falls back to DESIGN.md v2 defaults
 * - Optimistic UI: question appears immediately, answer fills in on response
 * - Error state with per-entry retry
 *
 * Integration:
 *   <FollowupPanel runId={runId} runEnded={true} effectSummary="..." />
 */
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type CSSProperties,
  type FormEvent,
  type ReactElement,
} from "react";
import { useAgentPlatform } from "./index.js";
import {
  useFollowupHistory,
  type FollowupEntry,
  type UseFollowupHistoryResult,
} from "./use-followup-history.js";

/* ------------------------------------------------------------------ */
/* v2 Design System CSS Variable Tokens (fallback defaults)            */
/* ------------------------------------------------------------------ */

const V2 = {
  primary: "var(--agent-primary, #4f46e5)",
  primaryHover: "var(--agent-primary-hover, #4338ca)",
  primarySubtle: "var(--agent-primary-subtle, #eef2ff)",
  primaryGlow: "var(--agent-primary-glow, rgba(79,70,229,0.12))",
  bgBase: "var(--agent-bg-base, #ffffff)",
  bgElevated: "var(--agent-bg-elevated, #fafaf9)",
  bgSurface: "var(--agent-bg-surface, #f5f5f4)",
  textPrimary: "var(--agent-text-primary, #1c1917)",
  textSecondary: "var(--agent-text-secondary, #57534e)",
  textTertiary: "var(--agent-text-tertiary, #a8a29e)",
  textQuaternary: "var(--agent-text-quaternary, #d6d3d1)",
  textInverse: "var(--agent-text-inverse, #ffffff)",
  borderDefault: "var(--agent-border-default, rgba(120,113,108,0.15))",
  borderSubtle: "var(--agent-border-subtle, rgba(120,113,108,0.08))",
  borderFocus: "var(--agent-border-focus, #4f46e5)",
  borderHover: "var(--agent-border-hover, rgba(79,70,229,0.3))",
  followupBg: "var(--agent-followup-bg, #fafaf9)",
  followupBorder: "var(--agent-followup-border, rgba(120,113,108,0.1))",
  userBubbleBg: "var(--agent-followup-user-bubble, #eef2ff)",
  userBubbleBorder: "var(--agent-followup-user-border, rgba(79,70,229,0.12))",
  agentBubbleBg: "var(--agent-followup-agent-bubble, #f5f5f4)",
  agentBubbleBorder:
    "var(--agent-followup-agent-border, rgba(120,113,108,0.1))",
  shadowXs: "var(--agent-shadow-xs, 0 1px 2px rgba(28,25,23,0.04))",
  shadowSm: "var(--agent-shadow-sm, 0 1px 3px rgba(28,25,23,0.06))",
  shadowFocus: "var(--agent-shadow-focus, 0 0 0 3px rgba(79,70,229,0.12))",
  shadowInset:
    "var(--agent-shadow-inset, inset 0 1px 2px rgba(28,25,23,0.04))",
  radiusSm: "var(--agent-radius-sm, 6px)",
  radiusMd: "var(--agent-radius-md, 10px)",
  radiusLg: "var(--agent-radius-lg, 14px)",
  radiusPill: "var(--agent-radius-pill, 9999px)",
  transFast: "var(--agent-transition-fast, 150ms cubic-bezier(0.4,0,0.2,1))",
  transNormal:
    "var(--agent-transition-normal, 250ms cubic-bezier(0.4,0,0.2,1))",
  transSpring:
    "var(--agent-transition-spring, 400ms cubic-bezier(0.34,1.56,0.64,1))",
} as const;

/* ------------------------------------------------------------------ */
/* Styles                                                              */
/* ------------------------------------------------------------------ */

const panelStyle: CSSProperties = {
  background: V2.followupBg,
  borderTop: `1px solid ${V2.followupBorder}`,
};

const headerStyle: CSSProperties = {
  display: "flex",
  alignItems: "center",
  justifyContent: "space-between",
  padding: "14px 20px",
  cursor: "pointer",
  userSelect: "none",
  transition: `background ${V2.transFast}`,
  border: "none",
  background: "transparent",
  width: "100%",
  fontFamily: "inherit",
  fontSize: "14px",
  color: V2.textPrimary,
  fontWeight: 600,
};

const headerLeftStyle: CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: "8px",
};

const countBadgeStyle: CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  minWidth: "22px",
  height: "22px",
  padding: "0 7px",
  background: V2.primary,
  color: V2.textInverse,
  borderRadius: V2.radiusPill,
  fontSize: "12px",
  fontWeight: 600,
  boxShadow: "0 1px 4px rgba(79,70,229,0.25)",
  transition: `transform ${V2.transSpring}`,
};

const toggleIconStyle: CSSProperties = {
  fontSize: "14px",
  color: V2.textTertiary,
  transition: `transform ${V2.transNormal}`,
};

const bodyWrapperStyle: CSSProperties = {
  overflow: "hidden",
  transition: `max-height ${V2.transNormal}, opacity ${V2.transNormal}, padding ${V2.transNormal}`,
};

const bodyExpandedStyle: CSSProperties = {
  ...bodyWrapperStyle,
  maxHeight: "420px",
  opacity: 1,
  padding: "0 20px 20px",
  overflowY: "auto",
};

const bodyCollapsedStyle: CSSProperties = {
  ...bodyWrapperStyle,
  maxHeight: "0",
  opacity: 0,
  padding: "0 20px",
  overflow: "hidden",
};

const summaryStyle: CSSProperties = {
  background: V2.bgBase,
  border: `1px solid ${V2.borderSubtle}`,
  borderRadius: V2.radiusMd,
  padding: "12px 16px",
  marginBottom: "18px",
  fontSize: "13px",
  color: V2.textSecondary,
  lineHeight: "1.5",
  boxShadow: V2.shadowXs,
};

const summaryLabelStyle: CSSProperties = {
  fontWeight: 600,
  color: V2.textPrimary,
  marginBottom: "4px",
  fontSize: "12px",
  textTransform: "uppercase",
  letterSpacing: "0.04em",
};

const entryStyle: CSSProperties = {
  marginBottom: "22px",
};

const questionRowStyle: CSSProperties = {
  display: "flex",
  alignItems: "flex-start",
  gap: "10px",
  marginBottom: "10px",
};

const userAvatarStyle: CSSProperties = {
  width: "30px",
  height: "30px",
  borderRadius: "50%",
  background: `linear-gradient(135deg, ${V2.primary} 0%, #818cf8 100%)`,
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  fontSize: "12px",
  color: V2.textInverse,
  fontWeight: 600,
  flexShrink: 0,
  marginTop: "2px",
  boxShadow: "0 2px 6px rgba(79,70,229,0.2)",
};

const userBubbleStyle: CSSProperties = {
  flex: 1,
  background: V2.userBubbleBg,
  border: `1px solid ${V2.userBubbleBorder}`,
  borderRadius: V2.radiusLg,
  borderBottomLeftRadius: "4px",
  padding: "12px 16px",
  fontSize: "14px",
  color: V2.textPrimary,
  lineHeight: "1.5",
  fontWeight: 500,
  boxShadow: V2.shadowXs,
};

const answerRowStyle: CSSProperties = {
  display: "flex",
  alignItems: "flex-start",
  gap: "10px",
  marginLeft: "40px",
};

const agentAvatarStyle: CSSProperties = {
  width: "30px",
  height: "30px",
  borderRadius: "50%",
  background: "linear-gradient(135deg, #a8a29e 0%, #78716c 100%)",
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  fontSize: "12px",
  color: V2.textInverse,
  fontWeight: 600,
  flexShrink: 0,
  marginTop: "2px",
  boxShadow: "0 2px 6px rgba(120,113,108,0.2)",
};

const answerBubbleStyle: CSSProperties = {
  flex: 1,
  background: V2.agentBubbleBg,
  border: `1px solid ${V2.agentBubbleBorder}`,
  borderRadius: V2.radiusLg,
  borderBottomRightRadius: "4px",
  padding: "12px 16px",
  fontSize: "14px",
  color: V2.textSecondary,
  lineHeight: "1.7",
  boxShadow: V2.shadowXs,
};

const answerBubbleSendingStyle: CSSProperties = {
  ...answerBubbleStyle,
  color: V2.textTertiary,
  fontStyle: "italic",
};

const answerBubbleErrorStyle: CSSProperties = {
  ...answerBubbleStyle,
  borderColor: "rgba(239,68,68,0.3)",
  background: "var(--agent-error-bg, #fef2f2)",
  color: "#b91c1c",
};

const timestampStyle: CSSProperties = {
  fontSize: "11px",
  color: V2.textQuaternary,
  marginTop: "6px",
  marginLeft: "76px",
  fontWeight: 500,
  letterSpacing: "0.02em",
};

const inputWrapperStyle: CSSProperties = {
  display: "flex",
  alignItems: "flex-end",
  gap: "10px",
  paddingTop: "18px",
  borderTop: `1px solid ${V2.borderSubtle}`,
  marginTop: "18px",
};

const inputStyle: CSSProperties = {
  flex: 1,
  minHeight: "46px",
  maxHeight: "120px",
  padding: "13px 16px",
  border: `1px solid ${V2.borderDefault}`,
  borderRadius: V2.radiusLg,
  background: V2.bgBase,
  fontFamily: "inherit",
  fontSize: "14px",
  color: V2.textPrimary,
  resize: "vertical",
  outline: "none",
  transition: `all ${V2.transFast}`,
  boxShadow: V2.shadowInset,
  lineHeight: "1.5",
};

const sendBtnStyle: CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  height: "46px",
  padding: "0 22px",
  background: V2.primary,
  color: V2.textInverse,
  border: "none",
  borderRadius: V2.radiusLg,
  fontSize: "14px",
  fontWeight: 500,
  cursor: "pointer",
  transition: `all ${V2.transFast}`,
  whiteSpace: "nowrap",
  fontFamily: "inherit",
  boxShadow: "0 1px 4px rgba(79,70,229,0.2)",
  letterSpacing: "0.01em",
};

const sendBtnDisabledStyle: CSSProperties = {
  ...sendBtnStyle,
  background: V2.bgSurface,
  color: V2.textTertiary,
  cursor: "not-allowed",
  boxShadow: "none",
};

const footerStyle: CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: "6px",
  paddingTop: "12px",
  fontSize: "11px",
  color: V2.textTertiary,
  fontWeight: 500,
  letterSpacing: "0.02em",
};

const retryBtnStyle: CSSProperties = {
  background: "none",
  border: "none",
  color: V2.primary,
  fontWeight: 600,
  cursor: "pointer",
  padding: "2px 6px",
  borderRadius: V2.radiusSm,
  fontSize: "12px",
  fontFamily: "inherit",
  transition: `all ${V2.transFast}`,
};

const emptyStyle: CSSProperties = {
  display: "flex",
  flexDirection: "column",
  alignItems: "center",
  justifyContent: "center",
  padding: "40px 24px",
  textAlign: "center",
};

const emptyIconStyle: CSSProperties = {
  fontSize: "36px",
  marginBottom: "16px",
  opacity: 0.2,
  filter: "grayscale(100%)",
};

const emptyTitleStyle: CSSProperties = {
  fontSize: "15px",
  fontWeight: 600,
  color: V2.textSecondary,
  marginBottom: "6px",
  letterSpacing: "-0.01em",
};

const emptyDescStyle: CSSProperties = {
  fontSize: "13px",
  color: V2.textTertiary,
};

/* ------------------------------------------------------------------ */
/* Animated entry wrapper                                              */
/* ------------------------------------------------------------------ */

const entryAnimStyle: CSSProperties = {
  animation: "agent-message-in 300ms cubic-bezier(0.34,1.56,0.64,1) forwards",
};

/* ------------------------------------------------------------------ */
/* Sub-components                                                      */
/* ------------------------------------------------------------------ */

function FollowupEntryRow({
  entry,
  onRetry,
}: {
  entry: FollowupEntry;
  onRetry?: ((entry: FollowupEntry) => void) | undefined;
}): ReactElement {
  return (
    <div style={entryAnimStyle}>
      <div style={entryStyle}>
        {/* Question */}
        <div style={questionRowStyle}>
          <div style={userAvatarStyle}>U</div>
          <div style={userBubbleStyle}>{entry.question}</div>
        </div>
        {/* Answer */}
        <div style={answerRowStyle}>
          <div style={agentAvatarStyle}>A</div>
          {entry.status === "sending" ? (
            <div style={answerBubbleSendingStyle}>正在回答...</div>
          ) : entry.status === "error" ? (
            <div style={answerBubbleErrorStyle}>
              回答失败
              {onRetry && (
                <button
                  type="button"
                  style={retryBtnStyle}
                  onClick={() => onRetry(entry)}
                >
                  重试
                </button>
              )}
            </div>
          ) : (
            <div style={answerBubbleStyle}>{entry.answer}</div>
          )}
        </div>
        {/* Timestamp */}
        {entry.answeredAt && (
          <div style={timestampStyle}>
            {new Date(entry.answeredAt).toLocaleString("zh-CN", {
              year: "numeric",
              month: "2-digit",
              day: "2-digit",
              hour: "2-digit",
              minute: "2-digit",
              second: "2-digit",
            })}
          </div>
        )}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Main Panel                                                         */
/* ------------------------------------------------------------------ */

export interface FollowupPanelProps {
  runId: string;
  /** Run has ended (SUCCEEDED/FAILED/CANCELLED) — panel can expand */
  runEnded: boolean;
  /** Optional effect summary text shown at the top */
  effectSummary?: string | undefined;
}

/* Inject @keyframes for message-in animation */
const keyframeStyle = `
@keyframes agent-message-in {
  from {
    opacity: 0;
    transform: translateY(12px) scale(0.96);
  }
  to {
    opacity: 1;
    transform: translateY(0) scale(1);
  }
}
`;

let injected = false;
function ensureKeyframes(): void {
  if (injected || typeof document === "undefined") return;
  injected = true;
  const style = document.createElement("style");
  style.textContent = keyframeStyle;
  document.head.appendChild(style);
}

export function FollowupPanel({
  runId,
  runEnded,
  effectSummary,
}: FollowupPanelProps): ReactElement {
  // Inject keyframe animation once
  useEffect(() => {
    ensureKeyframes();
  }, []);

  const { client } = useAgentPlatform();
  const history: UseFollowupHistoryResult = useFollowupHistory({
    runId,
    client,
    loadOnMount: runEnded,
  });

  const [expanded, setExpanded] = useState(false);
  const [inputValue, setInputValue] = useState("");
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const bodyRef = useRef<HTMLDivElement>(null);

  // Auto-expand when run ends (so user sees the input box immediately)
  useEffect(() => {
    if (runEnded) {
      setExpanded(true);
    }
  }, [runEnded]);

  // Auto-scroll to bottom when expanded or new entries arrive
  useEffect(() => {
    if (expanded && bodyRef.current) {
      bodyRef.current.scrollTop = bodyRef.current.scrollHeight;
    }
  }, [expanded, history.entries.length]);

  // Auto-resize textarea
  const handleInputChange = useCallback(
    (event: React.ChangeEvent<HTMLTextAreaElement>) => {
      setInputValue(event.target.value);
      const el = event.target;
      el.style.height = "auto";
      el.style.height = `${Math.min(el.scrollHeight, 120)}px`;
    },
    [],
  );

  const handleToggle = useCallback(() => {
    if (!runEnded) return;
    setExpanded((prev) => !prev);
  }, [runEnded]);

  const handleSubmit = useCallback(
    (event?: FormEvent) => {
      event?.preventDefault();
      if (!inputValue.trim() || history.sending) return;
      history.send(inputValue.trim());
      setInputValue("");
      // Reset textarea height
      if (inputRef.current) {
        inputRef.current.style.height = "auto";
      }
    },
    [inputValue, history],
  );

  const handleKeyDown = useCallback(
    (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        handleSubmit();
      }
    },
    [handleSubmit],
  );

  const handleRetry = useCallback(
    (entry: FollowupEntry) => {
      history.send(entry.question);
    },
    [history],
  );

  // Compute keyboard shortcut hint
  const canSend = inputValue.trim().length > 0 && !history.sending && runEnded;

  return (
    <div style={panelStyle} data-followup-panel={runId}>
      {/* Header */}
      <button
        type="button"
        style={headerStyle}
        onClick={handleToggle}
        disabled={!runEnded}
        aria-expanded={expanded}
      >
        <div style={headerLeftStyle}>
          <span>💬 追问</span>
          {history.entries.length > 0 && (
            <span style={countBadgeStyle}>{history.entries.length}</span>
          )}
        </div>
        <span
          style={{
            ...toggleIconStyle,
            transform: expanded ? "rotate(180deg)" : "rotate(0deg)",
          }}
        >
          ▼
        </span>
      </button>

      {/* Body */}
      <div
        ref={bodyRef}
        style={expanded ? bodyExpandedStyle : bodyCollapsedStyle}
      >
        {!runEnded ? (
          <div style={emptyStyle}>
            <div style={emptyIconStyle}>⏳</div>
            <div style={emptyTitleStyle}>任务执行中</div>
            <div style={emptyDescStyle}>任务完成后可在此追问</div>
          </div>
        ) : (
          <>
            {/* Empty state hint (first time) */}
            {history.entries.length === 0 && !effectSummary && (
              <div style={emptyStyle}>
                <div style={emptyIconStyle}>💬</div>
                <div style={emptyTitleStyle}>暂无追问记录</div>
                <div style={emptyDescStyle}>对结果有疑问？输入问题开始追问</div>
              </div>
            )}

            {/* Effect summary */}
            {effectSummary && (
              <div style={summaryStyle}>
                <div style={summaryLabelStyle}>效果摘要</div>
                {effectSummary}
              </div>
            )}

            {/* History entries */}
            {history.entries.map((entry) => (
              <FollowupEntryRow
                key={entry.clientFollowupId}
                entry={entry}
                onRetry={entry.status === "error" ? handleRetry : undefined}
              />
            ))}

            {/* Error banner */}
            {history.error && (
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: "10px",
                  padding: "10px 14px",
                  background: "var(--agent-error-bg, #fef2f2)",
                  border: "1px solid rgba(239,68,68,0.15)",
                  borderRadius: V2.radiusLg,
                  color: "#b91c1c",
                  fontSize: "13px",
                  marginBottom: "16px",
                  fontWeight: 500,
                }}
              >
                <span>❌</span>
                <span>{history.error}</span>
                <button
                  type="button"
                  style={{
                    ...retryBtnStyle,
                    marginLeft: "auto",
                  }}
                  onClick={() => history.reload()}
                >
                  重试
                </button>
              </div>
            )}

            {/* Input area */}
            <div style={inputWrapperStyle}>
              <textarea
                ref={inputRef}
                style={{
                  ...inputStyle,
                  ...(canSend ? {} : { cursor: "text" }),
                }}
                rows={1}
                placeholder="输入追问..."
                value={inputValue}
                onChange={handleInputChange}
                onKeyDown={handleKeyDown}
                disabled={!runEnded}
              />
              <button
                type="button"
                style={canSend ? sendBtnStyle : sendBtnDisabledStyle}
                disabled={!canSend}
                onClick={() => handleSubmit()}
              >
                {history.sending ? "发送中..." : "发送"}
              </button>
            </div>

            {/* Footer hint */}
            <div style={footerStyle}>
              <svg
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
                style={{ width: "14px", height: "14px", opacity: 0.5 }}
              >
                <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
              </svg>
              只读模式 · 模型基于本次任务上下文回答
            </div>
          </>
        )}
      </div>
    </div>
  );
}