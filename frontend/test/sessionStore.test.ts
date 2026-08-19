// Session store against a mocked DynamoDB DocumentClient.

export {}; // module scope, so top-level consts don't clash with other test files

const sendMock = jest.fn();

jest.mock("@aws-sdk/lib-dynamodb", () => {
  const actual = jest.requireActual("@aws-sdk/lib-dynamodb");
  return {
    ...actual,
    DynamoDBDocumentClient: { from: () => ({ send: sendMock }) },
  };
});

const OLD = process.env;

async function loadStore() {
  jest.resetModules();
  sendMock.mockReset();
  process.env = { ...OLD, SESSIONS_TABLE: "TestTable", AWS_REGION: "eu-central-1" };
  return import("@/lib/sessionStore");
}

afterAll(() => {
  process.env = OLD;
});

describe("boundMessages", () => {
  test("drops transient loading flags and caps the count", async () => {
    const { boundMessages } = await loadStore();
    const messages = Array.from({ length: 60 }, (_, i) => ({
      id: i,
      role: "user",
      text: `m${i}`,
      loading: true,
    }));
    const bounded = boundMessages(messages);
    expect(bounded).toHaveLength(50);
    expect(bounded[0]).not.toHaveProperty("loading");
    expect(bounded[0].id).toBe(10); // newest 50 win
  });

  test("drops oldest messages until the payload fits the size cap", async () => {
    const { boundMessages } = await loadStore();
    const big = "x".repeat(60_000);
    const messages = [1, 2, 3, 4, 5].map((i) => ({ id: i, role: "assistant", text: big }));
    const bounded = boundMessages(messages);
    expect(bounded.length).toBeLessThan(5);
    expect(bounded[bounded.length - 1].id).toBe(5); // newest kept
  });
});

describe("sessions", () => {
  test("putSession writes bounded messages with TTL and returns an id", async () => {
    const { putSession } = await loadStore();
    sendMock.mockResolvedValue({});
    const { id } = await putSession("sub-1", {
      title: "My first question",
      messages: [{ id: 1, role: "user", text: "hi" }],
    });
    expect(id).toMatch(/[0-9a-f-]{36}/);
    const item = sendMock.mock.calls[0][0].input.Item;
    expect(item.pk).toBe("USER#sub-1");
    expect(item.sk).toBe(`SESSION#${id}`);
    expect(item.messageCount).toBe(1);
    expect(item.expiresAt).toBeGreaterThan(Date.now() / 1000);
    expect(JSON.parse(item.messages)).toEqual([{ id: 1, role: "user", text: "hi" }]);
  });

  test("listSessions maps items and sorts newest first", async () => {
    const { listSessions } = await loadStore();
    sendMock.mockResolvedValue({
      Items: [
        { sk: "SESSION#a", title: "Old", updatedAt: "2026-08-01T00:00:00Z", messageCount: 2 },
        { sk: "SESSION#b", title: "New", updatedAt: "2026-08-12T00:00:00Z", messageCount: 4 },
      ],
    });
    const sessions = await listSessions("sub-1");
    expect(sessions.map((s) => s.id)).toEqual(["b", "a"]);
    expect(sessions[0]).toEqual({
      id: "b",
      title: "New",
      updatedAt: "2026-08-12T00:00:00Z",
      messageCount: 4,
    });
  });

  test("getSession parses stored messages and survives corrupt payloads", async () => {
    const { getSession } = await loadStore();
    sendMock.mockResolvedValueOnce({
      Item: {
        title: "T",
        createdAt: "c",
        updatedAt: "u",
        messageCount: 1,
        messages: JSON.stringify([{ id: 1, role: "user", text: "hi" }]),
      },
    });
    const ok = await getSession("sub-1", "abc");
    expect(ok?.messages).toEqual([{ id: 1, role: "user", text: "hi" }]);

    sendMock.mockResolvedValueOnce({ Item: { title: "T", messages: "{corrupt" } });
    const corrupt = await getSession("sub-1", "abc");
    expect(corrupt?.messages).toEqual([]);
  });
});

describe("settings", () => {
  test("defaults: not confidential, debug OFF unless the deployment opts in", async () => {
    const { getSettings } = await loadStore();
    sendMock.mockResolvedValue({});
    expect(await getSettings("sub-1")).toEqual({ confidential: false, debug: false });
  });

  test("DEFAULT_DEBUG_MODE=true makes debug the default for new users (demo deployments)", async () => {
    jest.resetModules();
    sendMock.mockReset();
    process.env = { ...OLD, SESSIONS_TABLE: "TestTable", DEFAULT_DEBUG_MODE: "true" };
    const { getSettings } = await import("@/lib/sessionStore");
    sendMock.mockResolvedValue({});
    expect(await getSettings("sub-1")).toEqual({ confidential: false, debug: true });
  });

  test("stored values win", async () => {
    const { getSettings } = await loadStore();
    sendMock.mockResolvedValue({ Item: { confidential: true, debug: false } });
    expect(await getSettings("sub-1")).toEqual({ confidential: true, debug: false });
  });

  test("putSettings writes both flags", async () => {
    const { putSettings } = await loadStore();
    sendMock.mockResolvedValue({});
    await putSettings("sub-1", { confidential: true, debug: false });
    const item = sendMock.mock.calls[0][0].input.Item;
    expect(item).toMatchObject({
      pk: "USER#sub-1",
      sk: "SETTINGS",
      confidential: true,
      debug: false,
    });
  });
});
