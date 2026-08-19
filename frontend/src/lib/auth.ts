// Cognito authentication: OAuth 2.0 authorization-code flow against the hosted UI, with
// the ID token kept in an httpOnly cookie and verified via the pool's JWKS. Uses `jose`
// so the exact same verification runs in the middleware and in Node route handlers.

import { createRemoteJWKSet, jwtVerify, type JWTPayload } from "jose";

export const ID_TOKEN_COOKIE = "pg_id";
export const STATE_COOKIE = "pg_oauth_state";
// Matches the pool client's idTokenValidity (12 h).
export const SESSION_MAX_AGE_SECONDS = 12 * 60 * 60;

const ISSUER = process.env.COGNITO_ISSUER ?? "";
const CLIENT_ID = process.env.COGNITO_CLIENT_ID ?? "";
const CLIENT_SECRET = process.env.COGNITO_CLIENT_SECRET ?? "";
const COGNITO_DOMAIN = (process.env.COGNITO_DOMAIN ?? "").replace(/\/$/, "");

export function authConfigured(): boolean {
  // CLIENT_SECRET included deliberately: without it the code exchange can only fail
  // with an opaque Cognito 400 — better to report "auth not configured" up front.
  return Boolean(ISSUER && CLIENT_ID && CLIENT_SECRET && COGNITO_DOMAIN);
}

// One JWKS per runtime instance; jose caches and refreshes keys internally.
let jwks: ReturnType<typeof createRemoteJWKSet> | undefined;
function getJwks() {
  if (!jwks) jwks = createRemoteJWKSet(new URL(`${ISSUER}/.well-known/jwks.json`));
  return jwks;
}

// `iat` is carried so the data plane can reject tokens issued before a sign-out
// (see lib/authGuard.ts — stateless JWTs cannot be invalidated on their own).
export type AuthedUser = { sub: string; email: string; issuedAt: number };

/** Verify a Cognito ID token; returns the user or null. */
export async function verifyIdToken(token: string | undefined | null): Promise<AuthedUser | null> {
  if (!token || !authConfigured()) return null;
  try {
    const { payload } = await jwtVerify(token, getJwks(), {
      issuer: ISSUER,
      audience: CLIENT_ID,
    });
    if (payload.token_use !== "id") return null;
    const sub = typeof payload.sub === "string" ? payload.sub : "";
    if (!sub) return null;
    return {
      sub,
      email: typeof payload.email === "string" ? payload.email : "",
      issuedAt: typeof payload.iat === "number" ? payload.iat : 0,
    };
  } catch {
    return null;
  }
}

/**
 * Resolve the authenticated user from a request's cookies.
 *
 * Signature check only. Node route handlers should use `requireUser` from
 * lib/authGuard.ts instead, which additionally honours sign-out revocation; that check
 * needs DynamoDB and therefore cannot live in this module (it is bundled into the Edge
 * middleware).
 */
export async function getAuthedUser(req: {
  cookies: { get(name: string): { value: string } | undefined };
}): Promise<AuthedUser | null> {
  return verifyIdToken(req.cookies.get(ID_TOKEN_COOKIE)?.value);
}

/**
 * Public origin of the running app, derived from the (CloudFront-forwarded) request so
 * redirect URIs work on every registered domain without configuration.
 */
export function requestOrigin(req: { headers: Headers; nextUrl: { protocol: string; host: string } }): string {
  const host = req.headers.get("x-forwarded-host") ?? req.headers.get("host") ?? req.nextUrl.host;
  const proto = host.startsWith("localhost") ? "http" : "https";
  return `${proto}://${host}`;
}

export function authorizeUrl(origin: string, state: string): string {
  const q = new URLSearchParams({
    response_type: "code",
    client_id: CLIENT_ID,
    redirect_uri: `${origin}/api/auth/callback`,
    scope: "openid email profile",
    state,
  });
  return `${COGNITO_DOMAIN}/oauth2/authorize?${q}`;
}

export function logoutUrl(origin: string): string {
  const q = new URLSearchParams({ client_id: CLIENT_ID, logout_uri: `${origin}/` });
  return `${COGNITO_DOMAIN}/logout?${q}`;
}

export type TokenResponse = { id_token?: string; error?: string };

/** Exchange an authorization code for tokens (server-side, confidential client). */
export async function exchangeCode(origin: string, code: string): Promise<TokenResponse> {
  const body = new URLSearchParams({
    grant_type: "authorization_code",
    client_id: CLIENT_ID,
    code,
    redirect_uri: `${origin}/api/auth/callback`,
  });
  // btoa instead of Buffer: this module is also bundled into the Edge middleware.
  const basic = btoa(`${CLIENT_ID}:${CLIENT_SECRET}`);
  const res = await fetch(`${COGNITO_DOMAIN}/oauth2/token`, {
    method: "POST",
    headers: {
      "Content-Type": "application/x-www-form-urlencoded",
      Authorization: `Basic ${basic}`,
    },
    body,
  });
  if (!res.ok) return { error: `token_endpoint_${res.status}` };
  return (await res.json()) as TokenResponse;
}

/** Extra claims access for tests/diagnostics. */
export type IdTokenClaims = JWTPayload & { email?: string; token_use?: string };
