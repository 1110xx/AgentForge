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
 *
 * Visual language borrowed from the pi-web-access search curator (MIT,
 * https://github.com/nicobailon/pi-web-access) and remapped onto the
 * EAP_THEME light palette: elevated card + top ambient glow, uppercase
 * kicker, pill status badges, dashed input row that sharpens on focus.
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

// Collapsed pill: curator-style rounded capsule on the primary token.
const pillStyle: CSSProperties = {
  padding: "9px 16px",
  border: `1px solid ${EAP_THEME.primary}`,
  borderRadius: "999px",
  background: EAP_THEME.primary,
  color: "var(--eap-primary-foreground, #ffffff)",
  cursor: "pointer",
  fontSize: "14px",
  fontWeight: 600,
  boxShadow: "0 2px 10px rgba(37, 99, 235, 0.28)",
};

// Panel: elevated card on the EAP surface tone with a top ambient glow
// (mirror of the curator's radial-gradient hero light, in the EAP blue).
const panelStyle: CSSProperties = {
  width: "320px",
  maxWidth: "calc(100vw - 32px)",
  background: EAP_THEME.surface,
  backgroundImage:
    "radial-gradient(ellipse at 50% 0%, rgba(37, 99, 235, 0.07) 0%, transparent 60%)",
  border: `1px solid ${EAP_THEME.border}`,
  borderRadius: "10px",
  boxShadow: "0 6px 24px rgba(15, 23, 42, 0.18)",
  overflow: "hidden",
  display: "flex",
  flexDirection: "column",
  boxSizing: "border-box",
};

const headerStyle: CSSProperties = {
  display: "flex",
  alignItems: "center",
  justifyContent: "space-between",
  padding: "10px 14px",
  borderBottom: `1px solid ${EAP_THEME.border}`,
};

const headerTextStyle: CSSProperties = {
  minWidth: 0,
};

// Curator-style uppercase kicker above the panel title.
const kickerStyle: CSSProperties = {
  margin: 0,
  fontSize: "10px",
  fontWeight: 700,
  letterSpacing: "0.1em",
  textTransform: "uppercase",
  color: EAP_THEME.primary,
};

const titleStyle: CSSProperties = {
  margin: 0,
  fontSize: "15px",
  fontWeight: 600,
  lineHeight: 1.3,
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
  padding: "10px 12px",
  listStyle: "none",
  maxHeight: "240px",
  overflowY: "auto",
  borderBottom: `1px solid ${EAP_THEME.border}`,
};

// Each chat entry is a curator-style result card: query row + status pill
// + two-line preview.
const entryStyle: CSSProperties = {
  padding: "10px 12px",
  margin: "0 0 8px",
  display: "flex",
  flexDirection: "column",
  gap: "6px",
  background: EAP_THEME.background,
  border: `1px solid ${EAP_THEME.border}`,
  borderRadius: "10px",
  boxShadow: "0 1px 2px rgba(15, 23, 42, 0.05)",
};

const entryQueryRowStyle: CSSProperties = {
  display: "flex",
  alignItems: "center",
  justifyContent: "space-between",
  gap: "8px",
};

const userTextStyle: CSSProperties = {
  fontSize: "14px",
  fontWeight: 600,
  color: EAP_THEME.text,
  whiteSpace: "pre-wrap",
  overflowWrap: "anywhere",
};

// Preview line mirrors the curator's two-line clamped result preview.
const entryPreviewStyle: CSSProperties = {
  fontSize: "12px",
  color: EAP_THEME.secondaryText,
  lineHeight: 1.45,
  whiteSpace: "nowrap",
  overflow: "hidden",
  textOverflow: "ellipsis",
};

// Pill status badge (curator 999px capsule) per state.
function badgeFor(entry: ChatEntry): CSSProperties {
  if (entry.status === "error") {
    return {
      padding: "2px 10px",
      borderRadius: "999px",
      background: "rgba(220, 38, 38, 0.10)",
      border: "1px solid rgba(220, 38, 38, 0.30)",
      color: "#b91c1c",
      fontSize: "10px",
      fontWeight: 700,
      letterSpacing: "0.03em",
      textTransform: "uppercase",
      whiteSpace: "nowrap",
    };
  }
  if (entry.status === "sending") {
    return {
      padding: "2px 10px",
      borderRadius: "999px",
      background: EAP_THEME.surface,
      border: `1px solid ${EAP_THEME.border}`,
      color: EAP_THEME.secondaryText,
      fontSize: "10px",
      fontWeight: 700,
      letterSpacing: "0.03em",
      textTransform: "uppercase",
      whiteSpace: "nowrap",
    };
  }
  // created with a run id: primary-tinted pill.
  return {
    padding: "2px 10px",
    borderRadius: "999px",
    background: "rgba(37, 99, 235, 0.10)",
    border: "1px solid rgba(37, 99, 235, 0.30)",
    color: EAP_THEME.primary,
    fontSize: "10px",
    fontWeight: 700,
    letterSpacing: "0.03em",
    textTransform: "uppercase",
    whiteSpace: "nowrap",
  };
}

const formStyle: CSSProperties = {
  display: "flex",
  gap: "8px",
  padding: "10px 14px 12px",
};

// Dashed input row that sharpens to a solid accent border on focus
// (curator "add-search" pattern).
const inputStyle: CSSProperties = {
  flex: 1,
  padding: "8px 10px",
  border: "1px dashed " + EAP_THEME.border,
  borderRadius: "8px",
  background: EAP_THEME.background,
  color: EAP_THEME.text,
  fontFamily: "inherit",
  fontSize: "14px",
};

const inputFocusStyle: CSSProperties = {
  border: `1px solid ${EAP_THEME.primary}`,
  boxShadow: "0 0 0 3px rgba(37, 99, 235, 0.12)",
};

const sendStyle: CSSProperties = {
  padding: "8px 14px",
  border: "none",
  borderRadius: "8px",
  background: EAP_THEME.primary,
  color: "var(--eap-primary-foreground, #ffffff)",
  cursor: "pointer",
  fontSize: "14px",
  fontWeight: 600,
  boxShadow: "0 2px 6px rgba(37, 99, 235, 0.25)",
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
  const [focused, setFocused] = useState(false);
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
            <div style={headerTextStyle}>
              <p style={kickerStyle}>Agent Platform</p>
              <h2 style={titleStyle}>Agent Chat</h2>
            </div>
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
                  <div style={entryQueryRowStyle}>
                    <span style={userTextStyle}>{entry.text}</span>
                    <span style={badgeFor(entry)}>{statusBadge(entry)}</span>
                  </div>
                  <div style={entryPreviewStyle}>
                    {entry.error ??
                      "Run 已创建 — AgentPanel 将实时跟随进度。"}
                  </div>
                </li>
              ))
            )}
          </ul>
          <form style={formStyle} onSubmit={handleSubmit}>
            <input
              ref={inputRef}
              style={focused ? { ...inputStyle, ...inputFocusStyle } : inputStyle}
              value={text}
              onChange={(event) => setText(event.target.value)}
              onFocus={() => setFocused(true)}
              onBlur={() => setFocused(false)}
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