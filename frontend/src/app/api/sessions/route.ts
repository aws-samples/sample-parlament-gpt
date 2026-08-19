import { NextRequest, NextResponse } from "next/server";
import { requireUser } from "@/lib/authGuard";
import { getSettings, listSessions, putSession, sessionStoreConfigured } from "@/lib/sessionStore";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET(req: NextRequest) {
  const user = await requireUser(req);
  if (!user) return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  if (!sessionStoreConfigured()) return NextResponse.json({ sessions: [] });
  return NextResponse.json({ sessions: await listSessions(user.sub) });
}

/** Create a new persisted session. Refused while the account is in confidential mode. */
export async function POST(req: NextRequest) {
  const user = await requireUser(req);
  if (!user) return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  if (!sessionStoreConfigured()) {
    return NextResponse.json({ error: "not_configured" }, { status: 501 });
  }

  let body: unknown;
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ error: "bad_request" }, { status: 400 });
  }
  const title = (body as { title?: unknown })?.title;
  const messages = (body as { messages?: unknown })?.messages;
  if (typeof title !== "string" || !Array.isArray(messages)) {
    return NextResponse.json({ error: "bad_request" }, { status: 400 });
  }

  // Server-side backstop: the client skips persistence calls in confidential mode, but
  // the mode must hold even for a stale/misbehaving client.
  const settings = await getSettings(user.sub);
  if (settings.confidential) {
    return NextResponse.json({ error: "confidential_mode" }, { status: 409 });
  }

  const { id } = await putSession(user.sub, { title, messages });
  return NextResponse.json({ id });
}
