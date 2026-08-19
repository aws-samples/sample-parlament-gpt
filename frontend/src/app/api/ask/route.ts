import { NextRequest, NextResponse } from "next/server";
import { requireUser } from "@/lib/authGuard";
import { streamAgentRuntime, hasLocalAgent, streamLocalAgent, type HistoryTurn } from "./agentClient";
import { rateLimit } from "./rateLimit";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const MAX_LEN = 500;

export async function POST(req: NextRequest) {
  // Defence in depth: the middleware already gates this route, but model invocation is the
  // one costly action in the system, so it re-verifies identity here (and honours sign-out
  // revocation) instead of trusting a single upstream check.
  const user = await requireUser(req);
  if (!user) {
    return NextResponse.json({ answer: "", sources: [] }, { status: 401 });
  }

  // Rate limit on the VERIFIED identity, not on X-Forwarded-For: the header is
  // client-supplied and only trustworthy because direct origin access is blocked, whereas
  // the subject comes from a signature-checked token (threat model D1).
  const { ok, retryAfterMs } = rateLimit(`user:${user.sub}`);
  if (!ok) {
    return NextResponse.json(
      { answer: "", sources: [] },
      { status: 429, headers: { "Retry-After": String(Math.ceil(retryAfterMs / 1000)) } },
    );
  }

  let body: unknown;
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ answer: "", sources: [] }, { status: 400 });
  }

  const question = (body as { question?: unknown })?.question;
  if (typeof question !== "string" || question.trim().length === 0) {
    return NextResponse.json({ answer: "", sources: [] }, { status: 400 });
  }

  const prompt = question.trim().slice(0, MAX_LEN);

  // Prior conversation turns supplied by the client, so follow-up questions have context.
  const rawHistory = (body as { history?: unknown })?.history;
  const history: HistoryTurn[] = Array.isArray(rawHistory)
    ? rawHistory
        .filter(
          (t): t is HistoryTurn =>
            !!t &&
            typeof t === "object" &&
            ((t as HistoryTurn).role === "user" || (t as HistoryTurn).role === "assistant") &&
            typeof (t as HistoryTurn).text === "string",
        )
        .slice(-12)
    : [];

  const sseHeaders = {
    "Content-Type": "text/event-stream; charset=utf-8",
    "Cache-Control": "no-cache, no-transform",
    Connection: "keep-alive",
  };

  // Local dev: stream the agent's SSE straight through to the browser.
  if (hasLocalAgent) {
    try {
      const upstream = await streamLocalAgent(prompt, history);
      if (!upstream.ok || !upstream.body) {
        return NextResponse.json({ answer: "", sources: [] }, { status: 502 });
      }
      return new Response(upstream.body, { status: 200, headers: sseHeaders });
    } catch (err) {
      console.error("agent stream failed:", err);
      return NextResponse.json({ answer: "", sources: [] }, { status: 502 });
    }
  }

  // Production: stream from AgentCore runtime.
  try {
    const stream = await streamAgentRuntime(prompt, history);
    return new Response(stream, { status: 200, headers: sseHeaders });
  } catch (err) {
    console.error("agent invocation failed:", err);
    return NextResponse.json({ answer: "", sources: [] }, { status: 502 });
  }
}
