// Sign-out revocation at the data plane (threat model S7): a valid-but-revoked token must
// be refused, and a store outage must not lock everyone out.

export {}; // module scope, so top-level consts don't clash with other test files

const verifyMock = jest.fn();
const signedOutAtMock = jest.fn();
const configuredMock = jest.fn();

jest.mock("@/lib/auth", () => ({
  getAuthedUser: (...args: unknown[]) => verifyMock(...args),
}));

jest.mock("@/lib/sessionStore", () => ({
  sessionStoreConfigured: () => configuredMock(),
  signedOutAt: (...args: unknown[]) => signedOutAtMock(...args),
}));

const REQ = { cookies: { get: () => ({ value: "token" }) } };
const NOW = Math.floor(Date.now() / 1000);

async function loadGuard() {
  jest.resetModules();
  return import("@/lib/authGuard");
}

beforeEach(() => {
  verifyMock.mockReset();
  signedOutAtMock.mockReset();
  configuredMock.mockReset().mockReturnValue(true);
});

describe("requireUser", () => {
  test("rejects when the token itself is invalid", async () => {
    verifyMock.mockResolvedValue(null);
    const { requireUser } = await loadGuard();
    expect(await requireUser(REQ)).toBeNull();
    expect(signedOutAtMock).not.toHaveBeenCalled();
  });

  test("accepts a valid token with no sign-out on record", async () => {
    verifyMock.mockResolvedValue({ sub: "s1", email: "a@b.c", issuedAt: NOW });
    signedOutAtMock.mockResolvedValue(0);
    const { requireUser } = await loadGuard();
    expect(await requireUser(REQ)).toEqual({ sub: "s1", email: "a@b.c", issuedAt: NOW });
  });

  test("rejects a token issued BEFORE the last sign-out", async () => {
    verifyMock.mockResolvedValue({ sub: "s1", email: "a@b.c", issuedAt: NOW - 600 });
    signedOutAtMock.mockResolvedValue(NOW - 60);
    const { requireUser } = await loadGuard();
    expect(await requireUser(REQ)).toBeNull();
  });

  test("rejects a token issued in the same second as the sign-out (iat resolution)", async () => {
    verifyMock.mockResolvedValue({ sub: "s1", email: "a@b.c", issuedAt: NOW });
    signedOutAtMock.mockResolvedValue(NOW);
    const { requireUser } = await loadGuard();
    expect(await requireUser(REQ)).toBeNull();
  });

  test("accepts a token issued AFTER the sign-out (fresh sign-in)", async () => {
    verifyMock.mockResolvedValue({ sub: "s1", email: "a@b.c", issuedAt: NOW });
    signedOutAtMock.mockResolvedValue(NOW - 30);
    const { requireUser } = await loadGuard();
    expect(await requireUser(REQ)).not.toBeNull();
  });

  test("fails open on a store outage rather than locking users out", async () => {
    verifyMock.mockResolvedValue({ sub: "s1", email: "a@b.c", issuedAt: NOW });
    signedOutAtMock.mockRejectedValue(new Error("ddb down"));
    const errorSpy = jest.spyOn(console, "error").mockImplementation(() => {});
    const { requireUser } = await loadGuard();
    expect(await requireUser(REQ)).not.toBeNull();
    errorSpy.mockRestore();
  });

  test("skips the revocation check when no store is configured", async () => {
    configuredMock.mockReturnValue(false);
    verifyMock.mockResolvedValue({ sub: "s1", email: "a@b.c", issuedAt: NOW });
    const { requireUser } = await loadGuard();
    expect(await requireUser(REQ)).not.toBeNull();
    expect(signedOutAtMock).not.toHaveBeenCalled();
  });
});
