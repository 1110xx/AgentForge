/**
 * LiveActivityPanel — agent 中间执行过程实时视图（SDD §11.4/§11.5）。
 *
 * 双数据源合并渲染：
 *   1. 持久事件（recentEvents 中的 tool.execution.started/ended 与
 *      agent.turn.completed）——可回放、重连后完整渲染每一轮思考/回复/工具；
 *   2. 临时 stream-chunk（projection.streamChunks 中的 thinking.delta /
 *      text.delta / tool.execution.updated）——只在在线会话渲染打字机效果，
 *      断连即丢弃，重连不依赖它们。
 *
 * 运行中用户看到：思考增量 + 工具调用（名称/参数/中间输出）+ 回复打字机；
 * 刷新/重连后看到：从持久 agent.turn.completed 恢复的完整轮次。
 */
import { useMemo, type CSSProperties, type ReactElement } from "react";
import { EAP_THEME } from "@platform/agent-ui-catalog";
import type { RunProjectionSnapshot } from "@platform/agent-ui-client";
import type { EnterpriseEventEnvelope } from "@platform/agent-ui-protocol";

const MAX_LIVE_TEXT_CHARS = 4_000;

interface CompletedTurn {
  readonly eventSeq: number;
  readonly turnSeq: number;
  readonly thinking: string;
  readonly messageText: string;
  readonly toolCalls: ReadonlyArray<{
    call_id: string;
    tool_name: string;
    status: string;
    is_error?: boolean;
  }>;
}

function toTurn(event: EnterpriseEventEnvelope): CompletedTurn | null {
  if (event.event_type !== "agent.turn.completed") {
    return null;
  }
  const payload = event.payload;
  if (payload.kind !== "agent.turn.completed") {
    return null;
  }
  return {
    eventSeq: event.event_seq,
    turnSeq: payload.turn_seq,
    thinking: payload.thinking ?? "",
    messageText: payload.message_text ?? "",
    toolCalls: payload.tool_calls ?? [],
  };
}

interface ActiveTool {
  readonly toolName: string;
  readonly argsText: string | null;
  readonly partial: string | null;
  readonly isError: boolean;
}

const activityStyle: CSSProperties = {
  padding: "12px",
  borderTop: `1px solid ${EAP_THEME.border}`,
  fontSize: "13px",
};

const labelStyle: CSSProperties = {
  margin: "0 0 8px",
  fontSize: "12px",
  fontWeight: 600,
  color: EAP_THEME.secondaryText,
};

const turnStyle: CSSProperties = {
  margin: "0 0 10px",
  padding: "8px 10px",
  borderRadius: EAP_THEME.radius,
  background: EAP_THEME.surface,
  border: `1px solid ${EAP_THEME.border}`,
};

const turnTitleStyle: CSSProperties = {
  fontSize: "12px",
  fontWeight: 600,
  margin: "0 0 4px",
};

const thinkingStyle: CSSProperties = {
  color: EAP_THEME.secondaryText,
  fontStyle: "italic",
  whiteSpace: "pre-wrap",
  margin: "4px 0",
};

const textStyle: CSSProperties = {
  whiteSpace: "pre-wrap",
  margin: "4px 0",
};

const toolStyle: CSSProperties = {
  fontFamily: "monospace",
  fontSize: "12px",
  color: EAP_THEME.secondaryText,
  margin: "2px 0",
  whiteSpace: "pre-wrap",
};

/** 最近 chunk 数量上限内的实时拼装（内存有界，UI-only）。 */
function clip(value: string): string {
  if (value.length <= MAX_LIVE_TEXT_CHARS) {
    return value;
  }
  return value.slice(-MAX_LIVE_TEXT_CHARS);
}

export function LiveActivityPanel({
  projection,
}: {
  projection: RunProjectionSnapshot;
}): ReactElement {
  // ── 持久事件 → 已完成的轮次（可回放） ──
  const turns = useMemo<CompletedTurn[]>(() => {
    const out: CompletedTurn[] = [];
    for (const event of projection.recentEvents) {
      const turn = toTurn(event);
      if (turn !== null) {
        out.push(turn);
      }
    }
    return out;
  }, [projection.recentEvents]);

  // ── 实时 chunk → 进行中的内容（打字机，不持久） ──
  const live = useMemo(() => {
    let thinking = "";
    let text = "";
    const activeTools = new Map<string, ActiveTool>();
    for (const chunk of projection.streamChunks) {
      switch (chunk.kind) {
        case "thinking.delta":
          thinking = clip(thinking + (chunk.delta ?? ""));
          break;
        case "text.delta":
          text = clip(text + (chunk.delta ?? ""));
          break;
        case "tool.execution.started":
          if (chunk.call_id !== undefined) {
            activeTools.set(chunk.call_id ?? "", {
              toolName: chunk.tool_name ?? "",
              argsText:
                chunk.args === null || chunk.args === undefined
                  ? null
                  : JSON.stringify(chunk.args),
              partial: null,
              isError: false,
            });
          }
          break;
        case "tool.execution.updated": {
          const current = activeTools.get(chunk.call_id ?? "");
          if (current !== undefined) {
            activeTools.set(chunk.call_id ?? "", {
              ...current,
              partial: chunk.partial ?? current.partial,
            });
          }
          break;
        }
        case "tool.execution.ended":
          if (chunk.call_id !== undefined) {
            const current = activeTools.get(chunk.call_id ?? "");
            if (current !== undefined) {
              activeTools.set(chunk.call_id ?? "", {
                ...current,
                isError: chunk.is_error === true,
              });
            }
          }
          break;
      }
    }
    return { thinking, text, activeTools };
  }, [projection.streamChunks]);

  const hasLive = live.thinking !== "" || live.text !== "" || live.activeTools.size > 0;
  const isEmpty = turns.length === 0 && !hasLive;

  return (
    <section style={activityStyle} data-agent-panel="live-activity">
      <p style={labelStyle}>Agent 运行过程</p>
      {isEmpty ? (
        <div style={{ color: EAP_THEME.secondaryText, fontSize: "12px" }}>
          等待 agent 启动…
        </div>
      ) : null}

      {turns.map((turn) => (
        <div key={turn.eventSeq} style={turnStyle} data-agent-turn={turn.turnSeq}>
          <p style={turnTitleStyle}>第 {turn.turnSeq} 轮</p>
          {turn.thinking ? (
            <details>
              <summary style={{ fontSize: "12px", color: EAP_THEME.secondaryText }}>
                思考过程（{turn.thinking.length} 字）
              </summary>
              <div style={thinkingStyle}>{turn.thinking}</div>
            </details>
          ) : null}
          {turn.toolCalls.map((tool) => (
            <div key={tool.call_id} style={toolStyle}>
              🔧 {tool.tool_name} → {tool.status}
              {tool.is_error ? " (失败)" : ""}
            </div>
          ))}
          {turn.messageText ? <div style={textStyle}>{turn.messageText}</div> : null}
        </div>
      ))}

      {hasLive ? (
        <div style={turnStyle} data-agent-turn="live">
          <p style={turnTitleStyle}>进行中</p>
          {live.activeTools.size > 0
            ? [...live.activeTools.entries()].map(([callId, tool]) => (
                <div key={callId} style={toolStyle}>
                  🔧 {tool.toolName}
                  {tool.argsText !== null ? ` ${tool.argsText}` : ""}
                  {tool.partial !== null ? `\n  ⇢ ${tool.partial}` : ""}
                  {tool.isError ? " ✗" : ""}
                </div>
              ))
            : null}
          {live.thinking ? <div style={thinkingStyle}>💭 {live.thinking}</div> : null}
          {live.text ? <div style={textStyle}>{live.text}</div> : null}
        </div>
      ) : null}
    </section>
  );
}

export type { CompletedTurn };