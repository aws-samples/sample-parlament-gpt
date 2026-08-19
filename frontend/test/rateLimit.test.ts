import { rateLimit } from "@/app/api/ask/rateLimit";

describe("rateLimit", () => {
  test("allows the first request from a key", () => {
    expect(rateLimit("k1").ok).toBe(true);
  });

  test("blocks after exceeding the window limit", () => {
    const key = "burst";
    let lastOk = true;
    for (let i = 0; i < 25; i++) {
      lastOk = rateLimit(key).ok;
    }
    expect(lastOk).toBe(false);
    const r = rateLimit(key);
    expect(r.ok).toBe(false);
    expect(r.retryAfterMs).toBeGreaterThan(0);
  });

  test("separate keys have independent budgets", () => {
    expect(rateLimit("indep-a").ok).toBe(true);
    expect(rateLimit("indep-b").ok).toBe(true);
  });
});
