import { NextRequest, NextResponse } from "next/server";
import {
  exchangeCode,
  ID_TOKEN_COOKIE,
  requestOrigin,
  SESSION_MAX_AGE_SECONDS,
  STATE_COOKIE,
  verifyIdToken,
} from "@/lib/auth";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * OAuth redirect target: validates the CSRF state, exchanges the code for tokens,
 * verifies the ID token, stores it as the session cookie, and returns to the app.
 */
export async function GET(req: NextRequest) {
  const origin = requestOrigin(req);
  const code = req.nextUrl.searchParams.get("code");
  const state = req.nextUrl.searchParams.get("state") ?? "";
  const expected = req.cookies.get(STATE_COOKIE)?.value ?? "";

  const fail = (reason: string) => {
    const res = NextResponse.redirect(`${origin}/api/auth/login`);
    res.cookies.delete(STATE_COOKIE);
    res.headers.set("x-auth-error", reason);
    return res;
  };

  if (!code || !state || !expected || state !== expected) return fail("state_mismatch");

  const tokens = await exchangeCode(origin, code);
  if (!tokens.id_token) return fail(tokens.error ?? "no_id_token");

  const user = await verifyIdToken(tokens.id_token);
  if (!user) return fail("invalid_id_token");

  // The post-login path travels inside the state (second segment, base64url).
  let next = "/";
  try {
    const encoded = state.split(".")[1] ?? "";
    const decoded = Buffer.from(encoded, "base64url").toString();
    if (decoded.startsWith("/") && !decoded.startsWith("//") && !decoded.startsWith("/\\")) {
      next = decoded;
    }
  } catch {
    /* fall back to "/" */
  }

  const res = NextResponse.redirect(`${origin}${next}`);
  res.cookies.delete(STATE_COOKIE);
  res.cookies.set(ID_TOKEN_COOKIE, tokens.id_token, {
    httpOnly: true,
    secure: origin.startsWith("https"),
    sameSite: "lax",
    path: "/",
    maxAge: SESSION_MAX_AGE_SECONDS,
  });
  return res;
}
