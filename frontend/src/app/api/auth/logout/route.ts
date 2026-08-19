import { NextRequest, NextResponse } from "next/server";
import {
  authConfigured,
  getAuthedUser,
  ID_TOKEN_COOKIE,
  logoutUrl,
  requestOrigin,
} from "@/lib/auth";
import { markSignedOut, sessionStoreConfigured } from "@/lib/sessionStore";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * Clears the session cookie and ends the Cognito hosted-UI session, so the next visit
 * requires a fresh sign-in rather than silently re-issuing a code.
 */
export async function POST(req: NextRequest) {
  const origin = requestOrigin(req);

  // Record the sign-out so tokens issued earlier stop being accepted by the data plane:
  // clearing the cookie alone leaves a stolen copy of this stateless token usable until
  // it expires (threat model S7). Best-effort — a store hiccup must not block sign-out.
  if (sessionStoreConfigured()) {
    const user = await getAuthedUser(req);
    if (user) {
      try {
        await markSignedOut(user.sub);
      } catch (err) {
        console.error("failed to record sign-out revocation:", err);
      }
    }
  }

  const res = NextResponse.json({
    ok: true,
    redirect: authConfigured() ? logoutUrl(origin) : `${origin}/`,
  });
  res.cookies.delete(ID_TOKEN_COOKIE);
  return res;
}
