/**
 * @vitest-environment jsdom
 *
 * Catalog renderer tests: allowlisted components render server-shaped
 * documents, unknown components fall back safely, and actions/downloads are
 * only issued through the explicit render context callbacks.
 */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import {
  renderSurfaceDocument,
  type SurfaceRenderContext,
} from "../src/index.js";

function context(overrides: Partial<SurfaceRenderContext> = {}): SurfaceRenderContext {
  return {
    runId: "run_demo",
    surface_id: "surface_summary",
    surface_revision: 1,
    ...overrides,
  };
}

describe("renderSurfaceDocument allowlist", () => {
  it("renders EvidenceSummary with title, data ref and items", () => {
    render(
      renderSurfaceDocument(
        {
          component: "EvidenceSummary",
          props: {
            title: "Synthetic failure evidence",
            data_ref: "artifact:artifact_001:1",
            items: ["submission:demo:A1:sha256:evidence"],
          },
        },
        context(),
      ),
    );
    expect(screen.getByText("Synthetic failure evidence")).toBeTruthy();
    expect(screen.getByText(/artifact:artifact_001:1/)).toBeTruthy();
    expect(screen.getByText("submission:demo:A1:sha256:evidence")).toBeTruthy();
  });

  it("renders ProgressCard with a status badge and steps", () => {
    render(
      renderSurfaceDocument(
        {
          component: "ProgressCard",
          props: {
            title: "Analysis",
            status: "RUNNING",
            description: "Reading synthetic results",
            steps: ["read", "analyze"],
          },
        },
        context(),
      ),
    );
    expect(screen.getByText("Analysis")).toBeTruthy();
    expect(screen.getByText("RUNNING")).toBeTruthy();
    expect(screen.getByText("Reading synthetic results")).toBeTruthy();
    expect(screen.getByText("read")).toBeTruthy();
    expect(screen.getByText("analyze")).toBeTruthy();
  });

  it("renders StaleCard as a non-executable notice", () => {
    render(
      renderSurfaceDocument(
        { component: "StaleCard", props: { status: "refresh required" } },
        context(),
      ),
    );
    expect(screen.getByText(/This surface is stale/)).toBeTruthy();
    expect(screen.getByText("refresh required")).toBeTruthy();
  });

  it("renders SafeFallback for unknown components without executing anything", () => {
    render(
      renderSurfaceDocument(
        { component: "MysteryWidget", props: {} },
        context(),
      ),
    );
    expect(screen.getByText(/Unsupported component "MysteryWidget"/)).toBeTruthy();
  });

  it("renders SafeFallback for malformed documents", () => {
    render(
      renderSurfaceDocument(
        { component: "EvidenceSummary", props: null as unknown as Record<string, unknown> },
        context(),
      ),
    );
    expect(screen.getByText(/Unsupported component "<invalid>"/)).toBeTruthy();
  });

  it("escapes server-provided text (no dangerouslySetInnerHTML)", () => {
    render(
      renderSurfaceDocument(
        {
          component: "EvidenceSummary",
          props: { items: ["<img src=x onerror=alert(1)>"] },
        },
        context(),
      ),
    );
    const item = screen.getByText("<img src=x onerror=alert(1)>");
    expect(item.querySelector("img")).toBeNull();
    expect(item.innerHTML).toContain("&lt;img");
  });
});

describe("ApprovalCard actions", () => {
  it("issues approve through the context with the server action_ref", async () => {
    const onAction = vi.fn();
    const user = userEvent.setup();
    render(
      renderSurfaceDocument(
        {
          component: "ApprovalCard",
          props: {
            approval_id: "approval_001",
            approve_key: "approval:approval_001:approve",
            reject_key: "approval:approval_001:reject",
            title: "Create a synthetic reference defect?",
            displayed_digest: "sha256:server-request",
            canonical_request_ref: "proposal:001",
          },
        },
        context({ onAction }),
      ),
    );
    expect(screen.getByText(/proposal:001/)).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "Approve" }));
    expect(onAction).toHaveBeenCalledWith({
      action_ref: "approval:approval_001:approve",
      surface_id: "surface_summary",
      surface_revision: 1,
      displayed_digest: "sha256:server-request",
    });
    // The browser Action command never carries approval_id.
    expect(onAction.mock.calls[0]?.[0]).not.toHaveProperty("approval_id");
  });

  it("issues reject through the context", async () => {
    const onAction = vi.fn();
    const user = userEvent.setup();
    render(
      renderSurfaceDocument(
        {
          component: "ApprovalCard",
          props: {
            approval_id: "approval_001",
            approve_key: "approval:approval_001:approve",
            reject_key: "approval:approval_001:reject",
          },
        },
        context({ onAction }),
      ),
    );
    await user.click(screen.getByRole("button", { name: "Reject" }));
    expect(onAction).toHaveBeenCalledWith(
      expect.objectContaining({ action_ref: "approval:approval_001:reject" }),
    );
  });

  it("disables both buttons while submitting", () => {
    render(
      renderSurfaceDocument(
        {
          component: "ApprovalCard",
          props: {
            approval_id: "approval_001",
            approve_key: "approval:approval_001:approve",
            reject_key: "approval:approval_001:reject",
          },
        },
        context({ submitting: true }),
      ),
    );
    expect(screen.getByRole("button", { name: /Submitting/ })).toBeTruthy();
    const buttons = screen.getAllByRole("button");
    expect(buttons.every((button) => (button as HTMLButtonElement).disabled)).toBe(true);
  });

  it("renders non-executable buttons when approval context is missing", () => {
    render(
      renderSurfaceDocument(
        {
          component: "ApprovalCard",
          props: { approve_key: "approval:x:approve", reject_key: "approval:x:reject" },
        },
        context(), // no onAction, no approval_id
      ),
    );
    const approve = screen.getByRole("button", { name: "Approve" }) as HTMLButtonElement;
    expect(approve.disabled).toBe(true);
  });
});

describe("ArtifactCard download", () => {
  it("requests an authorization before any download", async () => {
    const onDownload = vi.fn();
    const user = userEvent.setup();
    render(
      renderSurfaceDocument(
        {
          component: "ArtifactCard",
          props: {
            title: "Synthetic analysis report",
            artifact_id: "artifact_001",
            version: 1,
            download_action_ref: "artifact:artifact_001:download",
          },
        },
        context({ onDownloadAuthorizationRequest: onDownload }),
      ),
    );
    await user.click(screen.getByRole("button", { name: "Download" }));
    expect(onDownload).toHaveBeenCalledWith({ artifact_id: "artifact_001", version: 1 });
  });

  it("shows a disabled button when the artifact id is missing", () => {
    render(
      renderSurfaceDocument(
        { component: "ArtifactCard", props: { title: "Broken artifact" } },
        context(),
      ),
    );
    const button = screen.getByRole("button", { name: "Artifact unavailable" }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
  });

  it("supports the DESIGN.md ArtifactLink alias", () => {
    render(
      renderSurfaceDocument(
        {
          component: "ArtifactLink",
          props: { artifact_id: "artifact_001", version: 1 },
        },
        context(),
      ),
    );
    expect(screen.getByText(/artifact: artifact_001/)).toBeTruthy();
    expect(screen.getByText(/v1/)).toBeTruthy();
  });
});
