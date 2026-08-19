import { NextRequest, NextResponse } from "next/server";
import { requireUser } from "@/lib/authGuard";
import { debugDefault, getSettings, putSettings, sessionStoreConfigured } from "@/lib/sessionStore";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET(req: NextRequest) {
  const user = await requireUser(req);
  if (!user) return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  if (!sessionStoreConfigured()) {
    // No store = nothing to persist, but the debug DEFAULT still comes from the
    // deployment (DEFAULT_DEBUG_MODE), never a hardcoded true (threat model I4).
    return NextResponse.json({ confidential: false, debug: debugDefault(), persistence: false });
  }
  const settings = await getSettings(user.sub);
  return NextResponse.json({ ...settings, persistence: true });
}

export async function PUT(req: NextRequest) {
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
  const confidential = (body as { confidential?: unknown })?.confidential;
  const debug = (body as { debug?: unknown })?.debug;
  if (typeof confidential !== "boolean" || typeof debug !== "boolean") {
    return NextResponse.json({ error: "bad_request" }, { status: 400 });
  }

  await putSettings(user.sub, { confidential, debug });
  return NextResponse.json({ confidential, debug, persistence: true });
}
