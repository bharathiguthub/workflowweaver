import { describe, expect, it } from "vitest";

import { CONNECTIVITY_FAIL, CONNECTIVITY_PASS, STATUS_MOCK, STATUS_REAL, STATUS_REAL_WRITES_OFF } from "../test/doubles";
import { REAL_CONFIRMATION_PHRASE, toExecutionRequest, toPlanRequest, validateRealConfirmation } from "./forms";
import { modeOptions } from "./modes";
import { PLANNED } from "../test/doubles";

describe("modeOptions", () => {
  it("offers only mock when the status is unknown", () => {
    const options = modeOptions(null, null);
    expect(options.mock.available).toBe(true);
    expect(options.dry_run.available).toBe(false);
    expect(options.real.available).toBe(false);
    expect(options.real.reason).toMatch(/status is unavailable/);
  });

  it("never offers a mode above the backend ceiling", () => {
    const options = modeOptions(STATUS_MOCK, CONNECTIVITY_PASS);
    expect(options.dry_run.available).toBe(false);
    expect(options.real.available).toBe(false);
    expect(options.real.reason).toMatch(/INTEGRATION_MODE is 'mock'/);
  });

  it("requires a passing read-only connectivity check for real mode", () => {
    expect(modeOptions(STATUS_REAL, null).real).toMatchObject({ available: false, reason: "Run the Notion connectivity check first." });
    expect(modeOptions(STATUS_REAL, CONNECTIVITY_FAIL).real.reason).toMatch(/Notion is unavailable/);
    expect(modeOptions(STATUS_REAL, CONNECTIVITY_PASS).real).toEqual({ mode: "real", available: true, reason: null });
    expect(modeOptions(STATUS_REAL, CONNECTIVITY_PASS).dry_run.available).toBe(true);
  });

  it("reports the write kill switch as the real-mode blocker", () => {
    const options = modeOptions(STATUS_REAL_WRITES_OFF, CONNECTIVITY_PASS);
    expect(options.real.available).toBe(false);
    expect(options.real.reason).toMatch(/REAL_WRITES_ENABLED/);
    expect(options.dry_run.available).toBe(true);
  });

  it("offers only mock when the configuration is invalid", () => {
    const invalid = { ...STATUS_REAL, available_modes: ["mock" as const], configuration_valid: false, configuration_error: "NOTION_DESTINATIONS is not valid JSON." };
    const options = modeOptions(invalid, CONNECTIVITY_PASS);
    expect(options.dry_run.reason).toMatch(/NOTION_DESTINATIONS/);
    expect(options.real.available).toBe(false);
  });
});

describe("mode-aware request helpers", () => {
  it("puts the selected mode into plan and execution requests", () => {
    expect(toPlanRequest(" Create a Notion page please ", "", "dry_run")).toEqual({ instruction: "Create a Notion page please", mode: "dry_run" });
    expect(toExecutionRequest(PLANNED, " reviewer ", "real").approval).toEqual({ approved: true, mode: "real", approved_by: "reviewer" });
    expect(toExecutionRequest(PLANNED, "reviewer").approval.mode).toBe("mock");
  });

  it("requires the exact confirmation phrase", () => {
    expect(validateRealConfirmation("create notion page")).not.toBeNull();
    expect(validateRealConfirmation(` ${REAL_CONFIRMATION_PHRASE} `)).toBeNull();
  });
});
