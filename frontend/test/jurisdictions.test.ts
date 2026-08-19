import {
  JURISDICTIONS,
  displayFlag,
  displayLabel,
  enabledJurisdictions,
  jurisdictionMeta,
} from "@/lib/jurisdictions";

describe("jurisdiction metadata", () => {
  test("keys are unique and 2-letter, matching SpeechResult.jurisdiction", () => {
    const keys = JURISDICTIONS.map((j) => j.key);
    expect(new Set(keys).size).toBe(keys.length);
    for (const k of keys) expect(k).toMatch(/^[a-z]{2}$/);
  });

  test("every jurisdiction carries an attribution (a compliance requirement)", () => {
    for (const j of JURISDICTIONS) {
      expect(j.attribution.trim().length).toBeGreaterThan(0);
      expect(j.sourceUrl).toMatch(/^https:\/\//);
    }
  });

  test("licence-restricted sources are not enabled without a decision", () => {
    // Canada (Speaker's permission excludes commercial use) and Australia (CC BY-NC-ND) are
    // held pending a licensing decision — they must not be advertised as available.
    expect(jurisdictionMeta("ca")?.enabled).toBe(false);
    expect(jurisdictionMeta("au")?.enabled).toBe(false);
  });

  test("Canada and Australia attributions carry their required notices", () => {
    expect(jurisdictionMeta("ca")?.attribution).toMatch(/not an official version/i);
    expect(jurisdictionMeta("au")?.attribution).toMatch(/CC BY-NC-ND/i);
  });

  test("Switzerland attribution names the Parliamentary Services, as mandated", () => {
    expect(jurisdictionMeta("ch")?.attribution).toMatch(/Parlamentsdienste/);
  });

  test("enabledJurisdictions reflects only enabled entries", () => {
    const enabled = enabledJurisdictions();
    expect(enabled.length).toBeGreaterThan(0);
    for (const j of enabled) expect(j.enabled).toBe(true);
  });

  test("display helpers fall back safely for unknown keys", () => {
    expect(displayLabel("de")).toBe("German Bundestag");
    expect(displayLabel("zz", "Backend Label")).toBe("Backend Label");
    expect(displayLabel(null)).toBe("Unknown parliament");
    expect(displayFlag("de")).toBe("🇩🇪");
    expect(displayFlag("zz")).toBe("🏛");
  });
});
