/**
 * agent-ui-catalog — static allowlisted A2UI surface renderer.
 *
 * DESIGN.md constraints:
 *   - no dynamic import of arbitrary component names (fixed allowlist);
 *   - no dangerouslySetInnerHTML (React escapes all server-provided text);
 *   - light theme, radius 8px, primary #2563eb, host-overridable via
 *     `--eap-*` CSS variables;
 *   - recursion-ready: renderSurfaceDocument accepts a renderChild callback so
 *     a future server-side container component can compose nested documents
 *     without relaxing the allowlist.
 *
 * The allowlist mirrors the backend catalog (ui/validator.py):
 *   ProgressCard, EvidenceSummary, ArtifactCard, ApprovalCard, StaleCard
 * plus SafeFallback for anything the server must never send.
 */
import type { ReactElement, ReactNode } from "react";
import { createElement } from "react";
import type { JsonValue, SurfaceDocument } from "@platform/agent-ui-protocol";

export type { SurfaceDocument } from "@platform/agent-ui-protocol";

/** Component names the server catalog may emit (ui/validator.py parity). */
export const CATALOG_COMPONENTS = [
  "ProgressCard",
  "EvidenceSummary",
  "ArtifactCard",
  "ApprovalCard",
  "StaleCard",
] as const;

export type CatalogComponent = (typeof CATALOG_COMPONENTS)[number];

export const SURFACE_DOCUMENT_KIND = "a2ui-surface/v0.9.1" as const;

/* ------------------------------------------------------------------ */
/* Theme tokens (frontend/DESIGN.md)                                   */
/* ------------------------------------------------------------------ */

export const EAP_THEME = {
  primary: "var(--eap-primary, #2563eb)",
  background: "var(--eap-background, #ffffff)",
  surface: "var(--eap-surface, #f8fafc)",
  text: "var(--eap-text, #0f172a)",
  secondaryText: "var(--eap-secondary-text, #475569)",
  border: "var(--eap-border, #cbd5e1)",
  radius: "var(--eap-radius, 8px)",
  success: "var(--eap-success, #16a34a)",
  danger: "var(--eap-danger, #dc2626)",
  warning: "var(--eap-warning, #d97706)",
  focus: "var(--eap-focus, #2563eb)",
} as const;

/* ------------------------------------------------------------------ */
/* Render context                                                      */
/* ------------------------------------------------------------------ */

export interface SurfaceActionRequest {
  action_ref: string;
  surface_id: string;
  surface_revision: number;
  displayed_digest?: string | null;
}

export interface ArtifactDownloadRequest {
  artifact_id: string;
  version: number;
}

export interface SurfaceRenderContext {
  runId: string;
  /** Surface identity of the document currently being rendered. */
  surface_id: string;
  surface_revision: number;
  /** True while an action is in flight (buttons disabled). */
  submitting?: boolean;
  onAction?: (request: SurfaceActionRequest) => void;
  onDownloadAuthorizationRequest?: (request: ArtifactDownloadRequest) => void;
  /** Recursion hook for future container components. */
  renderChild?: (document: SurfaceDocument) => ReactNode;
}

export type ComponentProps = Readonly<Record<string, JsonValue>>;

/* ------------------------------------------------------------------ */
/* Small typed accessors                                               */
/* ------------------------------------------------------------------ */

function asString(value: JsonValue | undefined): string | undefined {
  return typeof value === "string" ? value : undefined;
}

function asNumber(value: JsonValue | undefined): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function asStringArray(value: JsonValue | undefined): string[] | undefined {
  if (!Array.isArray(value)) {
    return undefined;
  }
  return value.every((item): item is string => typeof item === "string")
    ? value
    : undefined;
}

/* ------------------------------------------------------------------ */
/* Shared primitives                                                   */
/* ------------------------------------------------------------------ */

const cardStyle: React.CSSProperties = {
  border: `1px solid ${EAP_THEME.border}`,
  borderRadius: EAP_THEME.radius,
  background: EAP_THEME.background,
  padding: "12px",
  margin: "8px 0",
  color: EAP_THEME.text,
  fontSize: "14px",
  fontFamily: "inherit",
  lineHeight: "1.5",
};

const titleStyle: React.CSSProperties = {
  margin: "0 0 8px",
  fontSize: "14px",
  fontWeight: 600,
  color: EAP_THEME.text,
};

const metaStyle: React.CSSProperties = {
  color: EAP_THEME.secondaryText,
  fontSize: "12px",
  wordBreak: "break-all",
};

const buttonBase: React.CSSProperties = {
  borderRadius: EAP_THEME.radius,
  border: `1px solid ${EAP_THEME.border}`,
  background: EAP_THEME.background,
  color: EAP_THEME.text,
  padding: "6px 12px",
  fontSize: "13px",
  fontFamily: "inherit",
  cursor: "pointer",
};

const primaryButton: React.CSSProperties = {
  ...buttonBase,
  background: EAP_THEME.primary,
  borderColor: EAP_THEME.primary,
  color: "#ffffff",
};

const dangerButton: React.CSSProperties = {
  ...buttonBase,
  borderColor: EAP_THEME.danger,
  color: EAP_THEME.danger,
};

function statusBadge(status: string): ReactElement {
  return createElement(
    "span",
    {
      style: {
        display: "inline-block",
        padding: "2px 8px",
        borderRadius: EAP_THEME.radius,
        background: EAP_THEME.surface,
        border: `1px solid ${EAP_THEME.border}`,
        color: EAP_THEME.text,
        fontSize: "12px",
        fontWeight: 600,
      },
    },
    status,
  );
}

function disabledButton(label: string): ReactElement {
  return createElement(
    "button",
    {
      type: "button",
      disabled: true,
      style: { ...buttonBase, opacity: 0.55, cursor: "not-allowed" },
    },
    label,
  );
}

/* ------------------------------------------------------------------ */
/* Components                                                          */
/* ------------------------------------------------------------------ */

interface CatalogComponentProps {
  props: ComponentProps;
  ctx?: SurfaceRenderContext;
}

function ProgressCard({ props }: CatalogComponentProps): ReactElement {
  const title = asString(props.title);
  const status = asString(props.status);
  const description = asString(props.description);
  const steps = asStringArray(props.steps);
  return createElement(
    "section",
    { style: cardStyle, "data-component": "ProgressCard" },
    createElement(
      "div",
      {
        style: {
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: "8px",
        },
      },
      title === undefined
        ? null
        : createElement("h3", { style: titleStyle }, title),
      status === undefined ? null : statusBadge(status),
    ),
    description === undefined
      ? null
      : createElement("p", { style: { margin: "4px 0" } }, description),
    steps === undefined || steps.length === 0
      ? null
      : createElement(
          "ol",
          { style: { margin: "8px 0 0", paddingLeft: "18px" } },
          steps.map((step, index) =>
            createElement("li", { key: `${index}-${step}` }, step),
          ),
        ),
  );
}

function EvidenceSummary({ props }: CatalogComponentProps): ReactElement {
  const title = asString(props.title);
  const dataRef = asString(props.data_ref);
  const items = asStringArray(props.items);
  return createElement(
    "section",
    { style: cardStyle, "data-component": "EvidenceSummary" },
    title === undefined ? null : createElement("h3", { style: titleStyle }, title),
    dataRef === undefined
      ? null
      : createElement(
          "div",
          { style: metaStyle },
          "data: ",
          dataRef,
        ),
    items === undefined || items.length === 0
      ? null
      : createElement(
          "ul",
          { style: { margin: "8px 0 0", paddingLeft: "18px" } },
          items.map((item, index) =>
            createElement("li", { key: `${index}-${item}` }, item),
          ),
        ),
  );
}

function ArtifactCard({ props, ctx }: CatalogComponentProps): ReactElement {
  const title = asString(props.title);
  const artifactId = asString(props.artifact_id);
  const version = asNumber(props.version);
  const downloadActionRef = asString(props.download_action_ref);
  const canDownload =
    artifactId !== undefined &&
    version !== undefined &&
    ctx?.onDownloadAuthorizationRequest !== undefined &&
    ctx?.submitting !== true;
  return createElement(
    "section",
    { style: cardStyle, "data-component": "ArtifactCard" },
    title === undefined ? null : createElement("h3", { style: titleStyle }, title),
    createElement(
      "div",
      { style: metaStyle },
      artifactId === undefined ? null : `artifact: ${artifactId}`,
      artifactId !== undefined && version !== undefined
        ? ` v${version}`
        : null,
      downloadActionRef === undefined
        ? null
        : ` · ${downloadActionRef}`,
    ),
    createElement(
      "div",
      { style: { marginTop: "8px" } },
      canDownload
        ? createElement(
            "button",
            {
              type: "button",
              onClick: () =>
                ctx?.onDownloadAuthorizationRequest?.({
                  artifact_id: artifactId as string,
                  version: version as number,
                }),
              style: primaryButton,
            },
            "Download",
          )
        : artifactId === undefined
          ? disabledButton("Artifact unavailable")
          : null,
    ),
  );
}

function ApprovalCard({ props, ctx }: CatalogComponentProps): ReactElement {
  const title = asString(props.title);
  const approveKey = asString(props.approve_key);
  const rejectKey = asString(props.reject_key);
  const displayedDigest = asString(props.displayed_digest);
  const canonicalRequestRef = asString(props.canonical_request_ref);
  const approvalId = asString(props.approval_id);
  const submitting = ctx?.submitting === true;
  const canDecide =
    ctx?.onAction !== undefined && approvalId !== undefined;
  const digest =
    displayedDigest === undefined
      ? canonicalRequestRef
      : `${canonicalRequestRef ?? ""} ${displayedDigest}`.trim();
  return createElement(
    "section",
    { style: cardStyle, "data-component": "ApprovalCard" },
    title === undefined ? null : createElement("h3", { style: titleStyle }, title),
    digest === ""
      ? null
      : createElement("div", { style: metaStyle }, digest),
    createElement(
      "div",
      {
        style: {
          display: "flex",
          gap: "8px",
          marginTop: "10px",
          flexWrap: "wrap",
        },
      },
      submitting
        ? createElement(
            "button",
            {
              type: "button",
              disabled: true,
              style: primaryButton,
            },
            "Submitting…",
          )
        : [
            approveKey !== undefined && canDecide
              ? createElement(
                  "button",
                  {
                    type: "button",
                    onClick: () =>
                      ctx?.onAction?.({
                        action_ref: approveKey,
                        surface_id: ctx?.surface_id ?? "",
                        surface_revision: ctx?.surface_revision ?? 0,
                        displayed_digest: displayedDigest ?? null,
                      }),
                    style: primaryButton,
                  },
                  "Approve",
                )
              : disabledButton("Approve"),
            rejectKey !== undefined && canDecide
              ? createElement(
                  "button",
                  {
                    type: "button",
                    onClick: () =>
                      ctx?.onAction?.({
                        action_ref: rejectKey,
                        surface_id: ctx?.surface_id ?? "",
                        surface_revision: ctx?.surface_revision ?? 0,
                        displayed_digest: displayedDigest ?? null,
                      }),
                    style: dangerButton,
                  },
                  "Reject",
                )
              : disabledButton("Reject"),
          ],
    ),
  );
}

function StaleCard({ props }: CatalogComponentProps): ReactElement {
  const status = asString(props.status);
  return createElement(
    "section",
    { style: cardStyle, "data-component": "StaleCard" },
    createElement(
      "div",
      {
        style: {
          color: EAP_THEME.warning,
          fontWeight: 600,
          marginBottom: "4px",
        },
      },
      "This surface is stale.",
    ),
    createElement(
      "div",
      { style: metaStyle },
      status === undefined ? "Refresh the run view to continue." : status,
    ),
  );
}

function SafeFallback(props: { component: string }): ReactElement {
  return createElement(
    "section",
    { style: cardStyle, "data-component": "SafeFallback" },
    createElement(
      "div",
      { style: { color: EAP_THEME.secondaryText } },
      `Unsupported component "${props.component}": this action cannot be executed.`,
    ),
  );
}

/* ------------------------------------------------------------------ */
/* Renderer dispatch                                                   */
/* ------------------------------------------------------------------ */

export function renderSurfaceDocument(
  document: SurfaceDocument,
  ctx: SurfaceRenderContext,
): ReactElement | null {
  if (
    typeof document.component !== "string" ||
    typeof document.props !== "object" ||
    document.props === null ||
    Array.isArray(document.props)
  ) {
    return createElement(SafeFallback, { component: "<invalid>" });
  }
  const props: ComponentProps = document.props as ComponentProps;
  switch (document.component) {
    case "ProgressCard":
      return createElement(ProgressCard, { props });
    case "EvidenceSummary":
      return createElement(EvidenceSummary, { props });
    case "ArtifactCard":
    case "ArtifactLink": // DESIGN.md name; server emits ArtifactCard.
      return createElement(ArtifactCard, { props, ctx });
    case "ApprovalCard":
      return createElement(ApprovalCard, { props, ctx });
    case "StaleCard":
      return createElement(StaleCard, { props });
    default:
      return createElement(SafeFallback, { component: document.component });
  }
}

/** Allowlist check used by tests and the provider (mirrors the server). */
export function isCatalogComponent(component: string): boolean {
  return (CATALOG_COMPONENTS as readonly string[]).includes(component);
}
