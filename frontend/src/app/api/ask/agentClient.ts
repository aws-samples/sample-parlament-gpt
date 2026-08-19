import {
  BedrockAgentCoreClient,
  InvokeAgentRuntimeCommand,
} from "@aws-sdk/client-bedrock-agentcore";

const REGION = process.env.AWS_REGION ?? "eu-central-1";
const AGENT_RUNTIME_ARN = process.env.AGENT_RUNTIME_ARN ?? "";
// LOCAL DEV ONLY: if set, call a locally running AgentCore server (HTTP) instead of the
// deployed AgentCore runtime. e.g. AGENT_LOCAL_URL=http://localhost:8080
const AGENT_LOCAL_URL = process.env.AGENT_LOCAL_URL ?? "";

// Credentials come from the ECS task role via the default provider chain — never from the
// browser and never hardcoded.
const client = new BedrockAgentCoreClient({ region: REGION });

// True when a local agent is configured; enables live SSE streaming in the route.
export const hasLocalAgent = Boolean(AGENT_LOCAL_URL);

export type HistoryTurn = { role: "user" | "assistant"; text: string };

/**
 * Type guard: does this value implement the async-iteration protocol?
 * Uses the `in` operator, so no computed member access (`obj[expr]`) appears anywhere —
 * SAST flags dynamic property lookups even on well-known symbols.
 */
function isAsyncIterable(x: unknown): x is AsyncIterable<Uint8Array> {
  return typeof x === "object" && x !== null && Symbol.asyncIterator in x;
}

/** Open an SSE stream from the local agent. Returns the raw fetch Response. */
export async function streamLocalAgent(prompt: string, history: HistoryTurn[] = []): Promise<Response> {
  return fetch(`${AGENT_LOCAL_URL.replace(/\/$/, "")}/invocations`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ prompt, stream: true, history }),
  });
}

/**
 * Invoke the AgentCore runtime with streaming enabled.
 * Returns a ReadableStream of SSE bytes that can be piped to the browser.
 * Falls back to a synthetic SSE stream wrapping a non-streaming JSON response.
 */
export async function streamAgentRuntime(
  prompt: string,
  history: HistoryTurn[] = [],
): Promise<ReadableStream<Uint8Array>> {
  if (!AGENT_RUNTIME_ARN) {
    throw new Error("AGENT_RUNTIME_ARN is not configured");
  }

  const cmd = new InvokeAgentRuntimeCommand({
    agentRuntimeArn: AGENT_RUNTIME_ARN,
    contentType: "application/json",
    accept: "text/event-stream",
    payload: new TextEncoder().encode(JSON.stringify({ prompt, stream: true, history })),
  });

  const res = await client.send(cmd);
  const body = res.response;

  // The response body can be various types depending on the SDK version and runtime.
  // Try to get a web ReadableStream or convert from an async iterable/Uint8Array.
  if (body && typeof (body as ReadableStream<Uint8Array>).getReader === "function") {
    return body as ReadableStream<Uint8Array>;
  }

  // If it's a Node.js Readable stream / async iterable. Delegating through an async
  // generator (`yield*`) drives the iteration protocol without any dynamic property
  // lookup; pull-based wrapping preserves backpressure.
  if (isAsyncIterable(body)) {
    const source = body;
    const iter = (async function* () {
      yield* source;
    })();
    return new ReadableStream<Uint8Array>({
      async pull(controller) {
        const { done, value } = await iter.next();
        if (done) {
          controller.close();
        } else {
          controller.enqueue(value instanceof Uint8Array ? value : new TextEncoder().encode(String(value)));
        }
      },
    });
  }

  // Fallback: body is a blob/string — parse as JSON and emit a synthetic SSE stream
  const raw = await bodyToString(body);
  return syntheticSSE(raw);
}

async function bodyToString(body: unknown): Promise<string> {
  if (!body) return "";
  if (typeof (body as { transformToString?: () => Promise<string> }).transformToString === "function") {
    return (body as { transformToString: () => Promise<string> }).transformToString();
  }
  if (body instanceof Uint8Array) return new TextDecoder().decode(body);
  if (isAsyncIterable(body)) {
    const chunks: Uint8Array[] = [];
    for await (const chunk of body) {
      chunks.push(chunk instanceof Uint8Array ? chunk : new TextEncoder().encode(String(chunk)));
    }
    return new TextDecoder().decode(Buffer.concat(chunks));
  }
  return String(body);
}

/**
 * If the AgentCore runtime didn't actually stream (returned JSON), wrap it as a
 * single SSE "answer" event so the frontend's consumeStream still works.
 */
function syntheticSSE(raw: string): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  const frame = (event: unknown) => `data: ${JSON.stringify(event)}\n\n`;
  let sent = false;
  return new ReadableStream<Uint8Array>({
    pull(controller) {
      if (sent) {
        controller.close();
        return;
      }
      sent = true;
      try {
        const data = JSON.parse(raw);
        // Emit steps first, then answer
        const steps = Array.isArray(data.steps) ? data.steps : [];
        const answer = {
          type: "answer",
          answer: typeof data.answer === "string" ? data.answer : "",
          sources: Array.isArray(data.sources) ? data.sources : [],
        };
        controller.enqueue(encoder.encode([...steps, answer].map(frame).join("")));
      } catch {
        controller.enqueue(encoder.encode(frame({ type: "answer", answer: raw, sources: [] })));
      }
    },
  });
}
