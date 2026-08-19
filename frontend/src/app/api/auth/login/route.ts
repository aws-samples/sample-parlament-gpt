import { NextRequest, NextResponse } from "next/server";
import { authConfigured, authorizeUrl, requestOrigin, STATE_COOKIE } from "@/lib/auth";
import { randomBytes } from "node:crypto";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/** Keep the post-login target on our own origin (reject absolute/protocol-relative URLs). */
function safeNext(raw: string | null): string {
  if (!raw || !raw.startsWith("/")) return "/";
  if (raw.startsWith("//") || raw.startsWith("/\\")) return "/";
  return raw;
}

/**
 * Starts the sign-in flow: sets a CSRF state cookie (which also carries the post-login
 * path) and redirects to the Cognito hosted UI.
 */
export async function GET(req: NextRequest) {
  if (!authConfigured()) {
    return NextResponse.json({ error: "auth_not_configured" }, { status: 500 });
  }
  const nonce = randomBytes(16).toString("base64url");
  const state = `${nonce}.${Buffer.from(safeNext(req.nextUrl.searchParams.get("next"))).toString("base64url")}`;
  const origin = requestOrigin(req);
  const res = NextResponse.redirect(authorizeUrl(origin, state));
  res.cookies.set(STATE_COOKIE, state, {
    httpOnly: true,
    secure: origin.startsWith("https"),
    sameSite: "lax",
    path: "/api/auth",
    maxAge: 600,
  });
  return res;
}
