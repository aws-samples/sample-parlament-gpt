// Authorisation guard for Node route handlers: token verification PLUS sign-out
// revocation. Node-only on purpose — it reaches DynamoDB, so it must never be pulled into
// the Edge middleware bundle (which uses lib/auth.ts directly).
//
// Why revocation exists: the session is a stateless Cognito ID token, so signing out
// cannot invalidate a copy an attacker already holds. Sign-out records a timestamp; any
// token issued before it is refused here — at the data plane, where the sensitive
// operations live (threat model S7).

import { getAuthedUser, type AuthedUser } from "@/lib/auth";
import { sessionStoreConfigured, signedOutAt } from "@/lib/sessionStore";

type CookieReader = { cookies: { get(name: string): { value: string } | undefined } };

/** Verified, non-revoked user for this request, or null. */
export async function requireUser(req: CookieReader): Promise<AuthedUser | null> {
  const user = await getAuthedUser(req);
  if (!user) return null;
  // Without a store there is nowhere to record sign-outs; the signature check stands alone.
  if (!sessionStoreConfigured()) return user;
  try {
    const revokedAt = await signedOutAt(user.sub);
    // Tokens minted in the same second as the sign-out are treated as revoked (iat has
    // one-second resolution, so ">=" would let a racing token through).
    if (revokedAt > 0 && user.issuedAt <= revokedAt) return null;
  } catch {
    // Fail open on a store outage rather than locking every user out: the token is still
    // cryptographically valid and short-lived. Logged for operators.
    console.error("revocation check unavailable; accepting valid token");
  }
  return user;
}
