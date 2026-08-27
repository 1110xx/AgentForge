/**
 * Golden-corpus consistency tests: every fixture in contracts/fixtures/
 * must parse against the corresponding Zod schema, and the schemas must
 * reject unknown keys (mirror of Pydantic extra="forbid").
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import {
  ApiErrorEnvelope,
  Approval,
  EffectCapabilityClaims,
  EnterpriseEventEnvelope,
  FollowupHistoryPage,
  RunEventPage,
  RunViewSnapshot,
  SurfaceRevision,
  ToolInvocation,
  UiSurface,
  type JsonValue,
} from "../src/index.js";

const FIXTURES_DIR = new URL("../../../../contracts/fixtures/", import.meta.url);

function loadFixture(name: string): JsonValue {
  const raw = readFileSync(fileURLToPath(new URL(name, FIXTURES_DIR)), "utf8");
  return JSON.parse(raw) as JsonValue;
}

const FIXTURE_CASES: ReadonlyArray<readonly [string, { safeParse: (input: unknown) => { success: boolean } }]> = [
  ["a2ui-surface.json", UiSurface],
  ["a2ui-surface-revision.json", SurfaceRevision],
  ["approval.json", Approval],
  ["api-error.json", ApiErrorEnvelope],
  ["effect-capability.json", EffectCapabilityClaims],
  ["enterprise-event.json", EnterpriseEventEnvelope],
  ["followup-history-page.json", FollowupHistoryPage],
  ["run-event-page.json", RunEventPage],
  ["run-view-snapshot.json", RunViewSnapshot],
  ["tool-invocation.json", ToolInvocation],
];

describe("golden fixtures parse against protocol schemas", () => {
  for (const [fileName, schema] of FIXTURE_CASES) {
    it(`${fileName} matches ${schema.constructor.name}`, () => {
      const fixture = loadFixture(fileName);
      const result = schema.safeParse(fixture);
      expect(result.success, `${fileName}: ${JSON.stringify(result)}`).toBe(true);
    });
  }

  it("enterprise-event.json is a ui.surface.committed envelope bound to attempt_001", () => {
    const parsed = EnterpriseEventEnvelope.parse(loadFixture("enterprise-event.json"));
    expect(parsed.schema_version).toBe("enterprise-event/v1");
    expect(parsed.event_type).toBe("ui.surface.committed");
    expect(parsed.payload_schema).toBe("a2ui-surface/v0.9.1");
    expect(parsed.payload).toEqual({
      kind: "ui.surface.committed",
      surface_id: "surface_summary",
      revision: 1,
    });
    expect(parsed.attempt_id).toBe("attempt_001");
    expect(parsed.event_seq).toBe(1);
  });

  it("run-event-page.json forms a coherent replay window", () => {
    const parsed = RunEventPage.parse(loadFixture("run-event-page.json"));
    expect(parsed.watermark).toBe(4);
    expect(parsed.retention_floor).toBe(0);
    expect(parsed.events.map((event) => event.event_seq)).toEqual([2, 3, 4]);
    for (const event of parsed.events) {
      expect(event.run_id).toBe("run_demo");
    }
  });

  it("run-view-snapshot.json is a RUNNING run with a committed surface", () => {
    const parsed = RunViewSnapshot.parse(loadFixture("run-view-snapshot.json"));
    expect(parsed.run_id).toBe("run_demo");
    expect(parsed.status).toBe("RUNNING");
    expect(parsed.watermark).toBe(4);
    expect(parsed.view.surfaces).toEqual([
      { surface_id: "surface_summary", catalog_id: "public-catalog", revision: 1 },
    ]);
    expect(parsed.view.attempts[0]?.status).toBe("RUNNING");
  });

  it("a2ui-surface documents adopt the { component, props } A2UI shape", () => {
    const parsed = UiSurface.parse(loadFixture("a2ui-surface.json"));
    const document = parsed.document as { component: string; props: object };
    expect(document.component).toBe("EvidenceSummary");
    expect(document.props).toBeTypeOf("object");
  });
});

describe("strict parsing mirrors Pydantic extra=forbid", () => {
  it("rejects unknown keys on the envelope", () => {
    const fixture = loadFixture("enterprise-event.json") as Record<string, JsonValue>;
    const withExtra = { ...fixture, smuggled: "x" };
    expect(EnterpriseEventEnvelope.safeParse(withExtra).success).toBe(false);
  });

  it("rejects unknown keys on nested payloads", () => {
    const fixture = loadFixture("run-view-snapshot.json") as {
      view: Record<string, JsonValue>;
    };
    const withExtra = {
      ...fixture,
      view: { ...fixture.view, smuggled: "x" },
    };
    expect(RunViewSnapshot.safeParse(withExtra).success).toBe(false);
  });
});
