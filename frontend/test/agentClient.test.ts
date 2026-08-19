// Mock the AWS SDK so no network/creds are needed.
export {}; // module scope, so top-level consts don't clash with other test files

const sendMock = jest.fn();

jest.mock("@aws-sdk/client-bedrock-agentcore", () => ({
  BedrockAgentCoreClient: jest.fn().mockImplementation(() => ({ send: sendMock })),
  InvokeAgentRuntimeCommand: jest.fn().mockImplementation((input) => ({ input })),
}));

function fakeBody(payload: string) {
  return { transformToString: async () => payload };
}

async function readAll(stream: ReadableStream<Uint8Array>): Promise<string> {
  const reader = stream.getReader();
  const decoder = new TextDecoder();
  let out = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    out += decoder.decode(value, { stream: true });
  }
  return out + decoder.decode();
}

describe("streamAgentRuntime", () => {
  const OLD = process.env;
  beforeEach(() => {
    jest.resetModules();
    sendMock.mockReset();
    process.env = { ...OLD, AGENT_RUNTIME_ARN: "arn:aws:bedrock-agentcore:eu-central-1:1:runtime/x" };
  });
  afterAll(() => {
    process.env = OLD;
  });

  test("wraps a non-streaming JSON response as a synthetic SSE answer event", async () => {
    sendMock.mockResolvedValue({
      response: fakeBody(JSON.stringify({ answer: "Antwort", sources: [{ party: "SPD" }] })),
    });
    const { streamAgentRuntime } = await import("@/app/api/ask/agentClient");
    const text = await readAll(await streamAgentRuntime("Frage zum Bundestag"));
    const frames = text.split("\n\n").filter(Boolean);
    expect(frames).toHaveLength(1);
    const event = JSON.parse(frames[0].replace(/^data:\s*/, ""));
    expect(event).toEqual({ type: "answer", answer: "Antwort", sources: [{ party: "SPD" }] });
  });

  test("emits steps before the answer when the JSON response includes them", async () => {
    sendMock.mockResolvedValue({
      response: fakeBody(
        JSON.stringify({
          answer: "Done",
          sources: [],
          steps: [{ type: "tool_call", tool: "uk___search_debates", input: {} }],
        }),
      ),
    });
    const { streamAgentRuntime } = await import("@/app/api/ask/agentClient");
    const text = await readAll(await streamAgentRuntime("q"));
    const events = text
      .split("\n\n")
      .filter(Boolean)
      .map((f) => JSON.parse(f.replace(/^data:\s*/, "")));
    expect(events.map((e) => e.type)).toEqual(["tool_call", "answer"]);
  });

  test("falls back to raw text as the answer when the body is not JSON", async () => {
    sendMock.mockResolvedValue({ response: fakeBody("plain text") });
    const { streamAgentRuntime } = await import("@/app/api/ask/agentClient");
    const text = await readAll(await streamAgentRuntime("x"));
    const event = JSON.parse(text.split("\n\n")[0].replace(/^data:\s*/, ""));
    expect(event).toEqual({ type: "answer", answer: "plain text", sources: [] });
  });

  test("forwards prompt and history in the runtime payload", async () => {
    sendMock.mockResolvedValue({ response: fakeBody("{}") });
    const { streamAgentRuntime } = await import("@/app/api/ask/agentClient");
    const history = [{ role: "user" as const, text: "earlier question" }];
    await streamAgentRuntime("follow-up", history);
    const cmd = sendMock.mock.calls[0][0];
    const payload = JSON.parse(new TextDecoder().decode(cmd.input.payload));
    expect(payload).toEqual({ prompt: "follow-up", stream: true, history });
  });

  test("throws when AGENT_RUNTIME_ARN is missing", async () => {
    delete process.env.AGENT_RUNTIME_ARN;
    const { streamAgentRuntime } = await import("@/app/api/ask/agentClient");
    await expect(streamAgentRuntime("x")).rejects.toThrow(/AGENT_RUNTIME_ARN/);
  });
});
