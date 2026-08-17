/**
 * Host Bridge capabilities for embedded AgentForge panels.
 *
 * The Bridge exposes exactly the three host-owned capabilities described in
 * docs/embedding-guide.md §5.3:
 *   1. getAccessToken      -> short-lived token (audience=enterprise-agent-platform);
 *                             provided to the client as a transport option.
 *   2. navigate            -> host navigation by stable destination_ref.
 *   3. downloadAuthorizedArtifact -> handle a server-issued ArtifactDownloadAuthorization.
 *
 * The Bridge deliberately does NOT expose arbitrary fetch, eval, DOM, router
 * objects, credential stores or business API clients to surfaces.
 */
import type { ArtifactDownloadAuthorization } from "./index.js";

export const HOST_BRIDGE_SCHEMA_VERSION = "host-bridge-capabilities/v1" as const;

export interface NavigateInput {
  /** Stable destination reference; the host resolves it, surfaces never see URLs. */
  destination_ref: string;
}

export interface DownloadAuthorizedArtifactInput {
  /** Server-issued, short-lived download authorization. */
  authorization: ArtifactDownloadAuthorization;
}

export interface HostBridgeCapabilities {
  schema_version: typeof HOST_BRIDGE_SCHEMA_VERSION;
  navigate(input: NavigateInput): Promise<void> | void;
  downloadAuthorizedArtifact(
    input: DownloadAuthorizedArtifactInput,
  ): Promise<void> | void;
}

/** Identity function helper for hosts that want a typed, readonly bridge. */
export function defineHostBridge(
  bridge: HostBridgeCapabilities,
): HostBridgeCapabilities {
  return bridge;
}
