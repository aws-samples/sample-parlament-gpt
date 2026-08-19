// The PreSignUp allowlist matcher (plain CommonJS so the Lambda asset needs no bundling).
// eslint-disable-next-line @typescript-eslint/no-var-requires
const { emailDomainAllowed } = require("../lambda/pre-signup/matcher");

describe("sign-up e-mail domain allowlist", () => {
  test("unset, empty, or '*' means open sign-up", () => {
    expect(emailDomainAllowed("a@anything.org", undefined)).toBe(true);
    expect(emailDomainAllowed("a@anything.org", "")).toBe(true);
    expect(emailDomainAllowed("a@anything.org", "  ")).toBe(true);
    expect(emailDomainAllowed("a@anything.org", "*")).toBe(true);
    expect(emailDomainAllowed("a@anything.org", "example.com,*")).toBe(true);
  });

  test("exact domain matches only that domain", () => {
    expect(emailDomainAllowed("a@example.com", "example.com")).toBe(true);
    expect(emailDomainAllowed("a@mail.example.com", "example.com")).toBe(false);
    expect(emailDomainAllowed("a@examplexcom", "example.com")).toBe(false);
    expect(emailDomainAllowed("a@evil-example.com", "example.com")).toBe(false);
  });

  test("*.domain matches the domain and its subdomains", () => {
    expect(emailDomainAllowed("a@example.com", "*.example.com")).toBe(true);
    expect(emailDomainAllowed("a@mail.example.com", "*.example.com")).toBe(true);
    expect(emailDomainAllowed("a@a.b.example.com", "*.example.com")).toBe(true);
    expect(emailDomainAllowed("a@notexample.com", "*.example.com")).toBe(false);
    expect(emailDomainAllowed("a@example.com.evil.org", "*.example.com")).toBe(false);
  });

  test("domain.* matches any TLD including multi-label", () => {
    expect(emailDomainAllowed("a@amazon.com", "amazon.*")).toBe(true);
    expect(emailDomainAllowed("a@amazon.de", "amazon.*")).toBe(true);
    expect(emailDomainAllowed("a@amazon.co.uk", "amazon.*")).toBe(true);
    expect(emailDomainAllowed("a@amazonaws.com", "amazon.*")).toBe(false);
    expect(emailDomainAllowed("a@evilamazon.com", "amazon.*")).toBe(false);
    // Subdomains are NOT implied by a TLD wildcard; use "*.amazon.com" for that.
    expect(emailDomainAllowed("a@sub.amazon.com", "amazon.*")).toBe(false);
  });

  test("lists combine with OR", () => {
    const list = "amazon.*, *.example.org ,partner.com";
    expect(emailDomainAllowed("a@amazon.co.jp", list)).toBe(true);
    expect(emailDomainAllowed("a@mail.example.org", list)).toBe(true);
    expect(emailDomainAllowed("a@partner.com", list)).toBe(true);
    expect(emailDomainAllowed("a@other.com", list)).toBe(false);
  });

  test("matching is case-insensitive", () => {
    expect(emailDomainAllowed("A@AMAZON.DE", "amazon.*")).toBe(true);
    expect(emailDomainAllowed("a@Example.COM", "EXAMPLE.com")).toBe(true);
  });

  test("addresses without a domain part are rejected when restricted", () => {
    expect(emailDomainAllowed("not-an-email", "example.com")).toBe(false);
    expect(emailDomainAllowed("@example.com", "example.com")).toBe(false);
    expect(emailDomainAllowed("", "example.com")).toBe(false);
  });
});
