import { NextRequest } from "next/server";

// The route streams: it calls streamAgentRuntime (production) or streamLocalAgent (dev) and
// pipes an SSE body through. The previous version of this suite mocked `invokeAgent`, which the
// route no longer calls, so two tests could never pass.
const streamAgentRuntimeMock = jest.fn();
const streamLocalAgentMock = jest.fn();
jest.mock("@/app/api/ask/agentClient", () => ({
  hasLocalAgent: false,
  streamAgentRuntime: (...args: unknown[]) => streamAgentRuntimeMock(...args),
  streamLocalAgent: (...args: unknown[]) => streamLocalAgentMock(...args),
}));

// The route authenticates itself (defence in depth, and to rate-limit on the verified
// subject); tests supply an authenticated user unless they assert the 401 path.
const requireUserMock = jest.fn();
jest.mock("@/lib/authGuard", () => ({
  requireUser: (...args: unknown[]) => requireUserMock(...args),
}));

/** Build a ReadableStream of SSE bytes, like the Gateway/agent would emit. */
function sseStream(events: Record<string, unknown>[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  return new ReadableStream<Uint8Array>({
    start(controller) {
      for (const ev of events) {
        controller.enqueue(encoder.encode(`data: ${JSON.stringify(ev)}\n\n`));
      }
      controller.close();
    },
  });
}

async function readAll(body: ReadableStream<Uint8Array> | null): Promise<string> {
  if (!body) return "";
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let out = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    out += decoder.decode(value, { stream: true });
  }
  return out;
}

function makeReq(body: unknown, ip = "1.2.3.4") {
  return new NextRequest("http://localhost/api/ask", {
    method: "POST",
    headers: { "content-type": "application/json", "x-forwarded-for": ip },
    body: typeof body === "string" ? body : JSON.stringify(body),
  });
}

describe("/api/ask route", () => {
  beforeEach(() => {
    jest.resetModules();
    streamAgentRuntimeMock.mockReset();
    streamLocalAgentMock.mockReset();
    // A distinct subject per test keeps the per-user rate limiter from bleeding across cases.
    requireUserMock
      .mockReset()
      .mockImplementation(async () => ({
        sub: `sub-${Math.random().toString(36).slice(2)}`,
        email: "a@b.c",
        issuedAt: Math.floor(Date.now() / 1000),
      }));
  });

  test("401 when the caller is unauthenticated or its session was revoked", async () => {
    requireUserMock.mockResolvedValue(null);
    const { POST } = await import("@/app/api/ask/route");
    const res = await POST(makeReq({ question: "Was sagte der Bundestag?" }, "ip-401"));
    expect(res.status).toBe(401);
    expect(streamAgentRuntimeMock).not.toHaveBeenCalled();
  });

  test("400 on empty question", async () => {
    const { POST } = await import("@/app/api/ask/route");
    const res = await POST(makeReq({ question: "   " }, "ip-empty"));
    expect(res.status).toBe(400);
    expect(streamAgentRuntimeMock).not.toHaveBeenCalled();
  });

  test("400 on malformed JSON", async () => {
    const { POST } = await import("@/app/api/ask/route");
    const res = await POST(makeReq("{not json", "ip-bad"));
    expect(res.status).toBe(400);
  });

  test("200 streams the agent's SSE events for a valid question", async () => {
    const source = {
      jurisdiction: "de",
      jurisdiction_label: "German Bundestag",
      doc_id: "aktivitaet:1@protokoll:2",
      title: "Debate",
      date: "2026-06-11",
      group: "SPD",
    };
    streamAgentRuntimeMock.mockResolvedValue(
      sseStream([
        { type: "tool_call", tool: "search_debates", jurisdiction: "germany", input: {} },
        { type: "tool_result", count: 1 },
        { type: "answer", answer: "Antwort", sources: [source] },
      ]),
    );
    const { POST } = await import("@/app/api/ask/route");
    const res = await POST(makeReq({ question: "Was sagte der Bundestag?" }, "ip-ok"));

    expect(res.status).toBe(200);
    expect(res.headers.get("content-type")).toContain("text/event-stream");
    const text = await readAll(res.body);
    expect(text).toContain('"type":"answer"');
    expect(text).toContain("Antwort");
    expect(text).toContain('"jurisdiction":"de"');
  });

  test("truncates over-long input to 500 chars before invoking", async () => {
    streamAgentRuntimeMock.mockResolvedValue(sseStream([{ type: "answer", answer: "ok", sources: [] }]));
    const { POST } = await import("@/app/api/ask/route");
    await POST(makeReq({ question: "x".repeat(2000) }, "ip-long"));
    const passed = streamAgentRuntimeMock.mock.calls[0][0] as string;
    expect(passed.length).toBe(500);
  });

  test("passes prior conversation turns through as history", async () => {
    streamAgentRuntimeMock.mockResolvedValue(sseStream([{ type: "answer", answer: "ok", sources: [] }]));
    const { POST } = await import("@/app/api/ask/route");
    await POST(
      makeReq(
        {
          question: "And in 2025?",
          history: [
            { role: "user", text: "pensions 2024" },
            { role: "assistant", text: "..." },
            { role: "bogus", text: "dropped" },
          ],
        },
        "ip-hist",
      ),
    );
    const history = streamAgentRuntimeMock.mock.calls[0][1] as { role: string }[];
    expect(history.map((h) => h.role)).toEqual(["user", "assistant"]);
  });

  test("502 when the agent invocation fails", async () => {
    streamAgentRuntimeMock.mockRejectedValue(new Error("boom"));
    const { POST } = await import("@/app/api/ask/route");
    const res = await POST(makeReq({ question: "Frage" }, "ip-err"));
    expect(res.status).toBe(502);
  });

  test("429 after one authenticated user floods the endpoint", async () => {
    // The limiter keys on the verified subject, not on the spoofable X-Forwarded-For:
    // rotating the header must not buy extra quota.
    requireUserMock.mockResolvedValue({ sub: "sub-flood", email: "a@b.c", issuedAt: 1 });
    streamAgentRuntimeMock.mockResolvedValue(sseStream([{ type: "answer", answer: "ok", sources: [] }]));
    const { POST } = await import("@/app/api/ask/route");
    let status = 200;
    for (let i = 0; i < 25; i++) {
      const res = await POST(makeReq({ question: "Frage" }, `ip-rotating-${i}`));
      status = res.status;
    }
    expect(status).toBe(429);
  });
});
