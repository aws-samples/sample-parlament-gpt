import { NextRequest, NextResponse } from "next/server";
import { requireUser } from "@/lib/authGuard";
import {
  deleteSession,
  getSession,
  getSettings,
  putSession,
  sessionStoreConfigured,
} from "@/lib/sessionStore";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

// Next 15: route params are async and must be awaited.
type Ctx = { params: Promise<{ id: string }> };

const ID_RE = /^[a-zA-Z0-9-]{1,64}$/;

export async function GET(req: NextRequest, { params }: Ctx) {
  const user = await requireUser(req);
  if (!user) return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  const { id } = await params;
  if (!sessionStoreConfigured() || !ID_RE.test(id)) {
    return NextResponse.json({ error: "not_found" }, { status: 404 });
  }
  const session = await getSession(user.sub, id);
  if (!session) return NextResponse.json({ error: "not_found" }, { status: 404 });
  return NextResponse.json(session);
}

/** Update an existing session. Refused while the account is in confidential mode. */
export async function PUT(req: NextRequest, { params }: Ctx) {
  const user = await requireUser(req);
  if (!user) return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  const { id } = await params;
  if (!sessionStoreConfigured() || !ID_RE.test(id)) {
    return NextResponse.json({ error: "not_found" }, { status: 404 });
  }

  let body: unknown;
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ error: "bad_request" }, { status: 400 });
  }
  const messages = (body as { messages?: unknown })?.messages;
  if (!Array.isArray(messages)) {
    return NextResponse.json({ error: "bad_request" }, { status: 400 });
  }

  const settings = await getSettings(user.sub);
  if (settings.confidential) {
    return NextResponse.json({ error: "confidential_mode" }, { status: 409 });
  }

  const existing = await getSession(user.sub, id);
  if (!existing) return NextResponse.json({ error: "not_found" }, { status: 404 });

  await putSession(user.sub, {
    id,
    title: existing.title,
    createdAt: existing.createdAt,
    messages,
  });
  return NextResponse.json({ ok: true });
}

export async function DELETE(req: NextRequest, { params }: Ctx) {
  const user = await requireUser(req);
  if (!user) return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  const { id } = await params;
  if (!sessionStoreConfigured() || !ID_RE.test(id)) {
    return NextResponse.json({ error: "not_found" }, { status: 404 });
  }
  await deleteSession(user.sub, id);
  return NextResponse.json({ ok: true });
}
