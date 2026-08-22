/**
 * AgentLauncher — floating free-form chat entry (Phase 3.6 frontend launcher).
 *
 * A fixed-position, collapsible launcher in the host corner: collapsed shows
 * a single pill button; expanded shows the message list (each entry carries
 * its Run id / status badge), an input box and a send button. Every submitted
 * message creates a Run via POST /v1/chat (through useAgentChat); the host
 * binds AgentPanel to the reported run id via onRunCreated.
 *
 * Styling follows the EAP_THEME tokens (same variables as AgentPanel /
 * FollowupPanel); no CSS framework is introduced.
 */
import {
  useRef,
  useState,
  type CSSProperties,
  type FormEvent,
  type ReactElement,
} from "react";
import { EAP_THEME } from "@platform/agent-ui-catalog";
import { useAgentChat, type ChatEntry } from "./use-chat.js";

/* ------------------------------------------------------------------ */
/* Styles                                                              */
/* ------------------------------------------------------------------ */

const fabStyle: CSSProperties = {
  position: "fixed",
  right: "16px",
  bottom: "16px",
  zIndex: 1000,
  display: "flex",
  flexDirection: "column",
  gap: "8px",
  alignItems: "flex-end",
  fontFamily: "inherit",
  fontSize: "14px",
  color: EAP_THEME.text,
};

const pillStyle: CSSProperties = {
  padding: "10px 16px",
  border: `1px solid ${EAP_THEME.border}`,
  borderRadius: EAP_THEME.radius,
  background: EAP_THEME.primary,
  color: "var(--eap-primary-foreground, #ffffff)",
  cursor: "pointer",
  fontSize: "14px",
  fontWeight: 600,
  boxShadow: "0 2px 8px rgba(0, 0, 0, 0.18)",
};

const panelStyle: CSSProperties = {
  width: "320px",
  maxWidth: "calc(100vw - 32px)",
  background: EAP_THEME.background,
  border: `1px solid ${EAP_THEME.border}`,
  borderRadius: EAP_THEME.radius,
  boxShadow: "0 4px 16px rgba(0, 0, 0, 0.22)",
  overflow: "hidden",
  display: "flex",
  flexDirection: "column",
  boxSizing: "border-box",
};

const headerStyle: CSSProperties = {
  display: "flex",
  alignItems: "center",
  justifyContent: "space-between",
  padding: "10px 12px",
  borderBottom: `1px solid ${EAP_THEME.border}`,
};

const titleStyle: CSSProperties = {
  margin: 0,
  fontSize: "14px",
  fontWeight: 600,
};

const collapseStyle: CSSProperties = {
  border: "none",
  background: "transparent",
  color: EAP_THEME.secondaryText,
  cursor: "pointer",
  fontSize: "16px",
  lineHeight: 1,
};

const listStyle: CSSProperties = {
  margin: 0,
  padding: "8px 12px",
  listStyle: "none",
  maxHeight: "240px",
  overflowY: "auto",
  borderBottom: `1px solid ${EAP_THEME.border}`,
};

const entryStyle: CSSProperties = {
  padding: "6px 0",
  display: "flex",
  flexDirection: "column",
  gap: "4px",
};

const userTextStyle: CSSProperties = {
  whiteSpace: "pre-wrap",
  overflowWrap: "anywhere",
};

const metaStyle: CSSProperties = {
  fontSize: "12px",
  color: EAP_THEME.secondaryText,
  display: "flex",
  alignItems: "center",
  justifyContent: "space-between",
  gap: "8px",
};

const badgeStyle: CSSProperties = {
  padding: "1px 8px",
  borderRadius: EAP_THEME.radius,
  background: EAP_THEME.surface,
  border: `1px solid ${EAP_THEME.border}`,
  fontSize: "11px",
  fontWeight: 600,
  whiteSpace: "nowrap",
};

const formStyle: CSSProperties = {
  display: "flex",
  gap: "8px",
  padding: "10px 12px",
};

const inputStyle: CSSProperties = {
  flex: 1,
  padding: "8px 10px",
  border: `1px solid ${EAP_THEME.border}`,
  borderRadius: EAP_THEME.radius,
  background: EAP_THEME.surface,
  color: EAP_THEME.text,
  fontFamily: "inherit",
  fontSize: "14px",
};

const sendStyle: CSSProperties = {
  padding: "8px 14px",
  border: "none",
  borderRadius: EAP_THEME.radius,
  background: EAP_THEME.primary,
  color: "var(--eap-primary-foreground, #ffffff)",
  cursor: "pointer",
  fontSize: "14px",
  fontWeight: 600,
};

const emptyStyle: CSSProperties = {
  padding: "16px 0",
  color: EAP_THEME.secondaryText,
  textAlign: "center",
};

/* ------------------------------------------------------------------ */
/* Component                                                           */
/* ------------------------------------------------------------------ */

export interface AgentLauncherProps {
  /** Called for every Run created via the chat launcher. */
  onRunCreated?: (runId: string) => void;
  /** Input placeholder (default: free-form prompt hint). */
  placeholder?: string;
  /** Accessible label for the collapsed pill (default: "Open agent chat"). */
  launchLabel?: string;
}

function statusBadge(entry: ChatEntry): string {
  if (entry.status === "created" && entry.runId !== null) {
    return `run ${entry.runId.slice(0, 8)}…`;
  }
  if (entry.status === "error") {
    return "failed";
  }
  return "sending…";
}

export function AgentLauncher({
  onRunCreated,
  placeholder = "分析或提问，例如：分析日志中的故障模式",
  launchLabel = "Open agent chat",
}: AgentLauncherProps): ReactElement {
  const { entries, sending, send } = useAgentChat({ onRunCreated });
  const [open, setOpen] = useState(false);
  const [text, setText] = useState("");
  const inputRef = useRef<HTMLInputElement | null>(null);

  const toggle = (): void => {
    const next = !open;
    setOpen(next);
    if (next) {
      requestAnimationFrame(() => inputRef.current?.focus());
    }
  };

  const handleSubmit = (event: FormEvent): void => {
    event.preventDefault();
    const trimmed = text.trim();
    if (!trimmed || sending) {
      return;
    }
    void send(trimmed);
    setText("");
  };

  return (
    <div style={fabStyle} data-agent-launcher={open ? "open" : "collapsed"}>
      {open ? (
        <section style={panelStyle} role="dialog" aria-label="Agent chat">
          <div style={headerStyle}>
            <h2 style={titleStyle}>Agent Chat</h2>
            <button
              type="button"
              style={collapseStyle}
              onClick={() => setOpen(false)}
              aria-label="Collapse agent chat"
            >
              ×
            </button>
          </div>
          <ul style={listStyle}>
            {entries.length === 0 ? (
              <li style={emptyStyle}>还没有消息——试着提一个问题。</li>
            ) : (
              entries.map((entry) => (
                <li key={entry.id} style={entryStyle}>
                  <div style={userTextStyle}>{entry.text}</div>
                  <div style={metaStyle}>
                    <span>{entry.error ?? "AgentForge"}</span>
                    <span style={badgeStyle}>{statusBadge(entry)}</span>
                  </div>
                </li>
              ))
            )}
          </ul>
          <form style={formStyle} onSubmit={handleSubmit}>
            <input
              ref={inputRef}
              style={inputStyle}
              value={text}
              onChange={(event) => setText(event.target.value)}
              placeholder={placeholder}
              aria-label="Chat message"
            />
            <button
              type="submit"
              style={sendStyle}
              disabled={!text.trim() || sending}
            >
              Send
            </button>
          </form>
        </section>
      ) : null}
      {!open ? (
        <button type="button" style={pillStyle} onClick={toggle} aria-label={launchLabel}>
          💬 Agent
        </button>
      ) : null}
    </div>
  );
}