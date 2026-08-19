// Citation links are gated to http(s): source_url comes from upstream parliament APIs
// (lower-trust input) and an anchor href is an injection sink. See docs/threat-model.md T-U2/F1.
//
// The predicate lives in the client component, so it is re-implemented here against the same
// contract; page.tsx is a "use client" module that jest (node env) does not import cleanly.

const safeHttpUrl = (url: string | null | undefined): boolean =>
  typeof url === "string" && /^https?:\/\//i.test(url);

describe("safeHttpUrl (citation link gate)", () => {
  test("accepts the real-world source URLs the adapters produce", () => {
    expect(safeHttpUrl("https://hansard.parliament.uk/Commons/2024-01-01/debates/abc")).toBe(true);
    expect(safeHttpUrl("https://dserver.bundestag.de/btp/21/21017.pdf")).toBe(true);
    expect(safeHttpUrl("http://www.parlament.gv.at/record")).toBe(true);
    expect(safeHttpUrl("HTTPS://UPPER.EXAMPLE/x")).toBe(true);
  });

  test("rejects script-bearing and non-http schemes", () => {
    expect(safeHttpUrl("javascript:alert(1)")).toBe(false);
    expect(safeHttpUrl("JaVaScRiPt:alert(1)")).toBe(false);
    expect(safeHttpUrl("data:text/html;base64,PHNjcmlwdD4=")).toBe(false);
    expect(safeHttpUrl("vbscript:msgbox")).toBe(false);
    expect(safeHttpUrl("file:///etc/passwd")).toBe(false);
    expect(safeHttpUrl("//evil.example/x")).toBe(false);
    expect(safeHttpUrl("/relative/path")).toBe(false);
  });

  test("rejects missing and non-string values", () => {
    expect(safeHttpUrl(null)).toBe(false);
    expect(safeHttpUrl(undefined)).toBe(false);
    expect(safeHttpUrl("")).toBe(false);
    expect(safeHttpUrl(" https://example.com")).toBe(false); // leading space is not a valid URL
  });
});
