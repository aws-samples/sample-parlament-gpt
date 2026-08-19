// ID-token verification against a locally generated key pair: jose's remote JWKS is
// swapped for a local one so no network is involved.

import { exportJWK, generateKeyPair, SignJWT, type JWK } from "jose";

const ISSUER = "https://cognito-idp.eu-central-1.amazonaws.com/eu-central-1_TESTPOOL";
const CLIENT_ID = "test-client-id";

let privateKey: Awaited<ReturnType<typeof generateKeyPair>>["privateKey"];
let publicJwk: JWK;

jest.mock("jose", () => {
  const actual = jest.requireActual("jose");
  return {
    ...actual,
    // The module under test builds a remote JWKS from the issuer URL; serve our local key.
    createRemoteJWKSet: () => actual.createLocalJWKSet({ keys: [publicJwk] }),
  };
});

async function loadAuth() {
  jest.resetModules();
  process.env = {
    ...OLD,
    COGNITO_ISSUER: ISSUER,
    COGNITO_CLIENT_ID: CLIENT_ID,
    // authConfigured() requires the client secret too (confidential client).
    COGNITO_CLIENT_SECRET: "test-client-secret",
    COGNITO_DOMAIN: "https://test.auth.eu-central-1.amazoncognito.com",
  };
  return import("@/lib/auth");
}

const OLD = process.env;

beforeAll(async () => {
  const pair = await generateKeyPair("RS256");
  privateKey = pair.privateKey;
  publicJwk = { ...(await exportJWK(pair.publicKey)), kid: "test-key", alg: "RS256", use: "sig" };
});

afterAll(() => {
  process.env = OLD;
});

function baseToken() {
  return new SignJWT({ token_use: "id", email: "alice@example.com" })
    .setProtectedHeader({ alg: "RS256", kid: "test-key" })
    .setIssuer(ISSUER)
    .setAudience(CLIENT_ID)
    .setSubject("user-sub-1")
    .setIssuedAt()
    .setExpirationTime("1h");
}

describe("verifyIdToken", () => {
  test("accepts a valid ID token and returns sub + email", async () => {
    const { verifyIdToken } = await loadAuth();
    const token = await baseToken().sign(privateKey);
    // issuedAt is carried so the data plane can reject pre-sign-out tokens (see authGuard).
    expect(await verifyIdToken(token)).toEqual({
      sub: "user-sub-1",
      email: "alice@example.com",
      issuedAt: expect.any(Number),
    });
  });

  test("rejects a token for a different audience", async () => {
    const { verifyIdToken } = await loadAuth();
    const token = await baseToken().setAudience("other-client").sign(privateKey);
    expect(await verifyIdToken(token)).toBeNull();
  });

  test("rejects a token from a different issuer", async () => {
    const { verifyIdToken } = await loadAuth();
    const token = await baseToken().setIssuer("https://evil.example.com").sign(privateKey);
    expect(await verifyIdToken(token)).toBeNull();
  });

  test("rejects access tokens (token_use must be id)", async () => {
    const { verifyIdToken } = await loadAuth();
    const token = await new SignJWT({ token_use: "access" })
      .setProtectedHeader({ alg: "RS256", kid: "test-key" })
      .setIssuer(ISSUER)
      .setAudience(CLIENT_ID)
      .setSubject("user-sub-1")
      .setIssuedAt()
      .setExpirationTime("1h")
      .sign(privateKey);
    expect(await verifyIdToken(token)).toBeNull();
  });

  test("rejects expired tokens", async () => {
    const { verifyIdToken } = await loadAuth();
    const token = await baseToken().setExpirationTime("-1h").sign(privateKey);
    expect(await verifyIdToken(token)).toBeNull();
  });

  test("rejects garbage and empty tokens", async () => {
    const { verifyIdToken } = await loadAuth();
    expect(await verifyIdToken("not.a.jwt")).toBeNull();
    expect(await verifyIdToken("")).toBeNull();
    expect(await verifyIdToken(undefined)).toBeNull();
  });
});

describe("authorize / logout URLs", () => {
  test("authorizeUrl carries client id, redirect uri, and state", async () => {
    const { authorizeUrl } = await loadAuth();
    const url = new URL(authorizeUrl("https://app.example.com", "state-123"));
    expect(url.origin).toBe("https://test.auth.eu-central-1.amazoncognito.com");
    expect(url.pathname).toBe("/oauth2/authorize");
    expect(url.searchParams.get("client_id")).toBe(CLIENT_ID);
    expect(url.searchParams.get("redirect_uri")).toBe("https://app.example.com/api/auth/callback");
    expect(url.searchParams.get("state")).toBe("state-123");
    expect(url.searchParams.get("response_type")).toBe("code");
  });

  test("logoutUrl points back at the app root", async () => {
    const { logoutUrl } = await loadAuth();
    const url = new URL(logoutUrl("https://app.example.com"));
    expect(url.pathname).toBe("/logout");
    expect(url.searchParams.get("logout_uri")).toBe("https://app.example.com/");
  });
});
