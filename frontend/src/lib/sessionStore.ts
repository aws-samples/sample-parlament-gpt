// Chat sessions and per-user settings in DynamoDB (single table, TTL'd).
//
// Layout:
//   pk = USER#<sub>   sk = SESSION#<id>   title, messages(JSON), createdAt, updatedAt, expiresAt
//   pk = USER#<sub>   sk = SETTINGS       confidential, debug
//
// Confidential mode is enforced by the CALLER (routes/UI simply never write while it is
// on); this module is a plain data mapper and never deletes anything on a settings flip.

import { DynamoDBClient } from "@aws-sdk/client-dynamodb";
import {
  DeleteCommand,
  DynamoDBDocumentClient,
  GetCommand,
  PutCommand,
  QueryCommand,
} from "@aws-sdk/lib-dynamodb";
import { randomUUID } from "node:crypto";

const TABLE = process.env.SESSIONS_TABLE ?? "";
const REGION = process.env.AWS_REGION ?? "eu-central-1";

// Sessions expire 90 days after their last update (TTL attribute, refreshed on write).
const SESSION_TTL_SECONDS = 90 * 24 * 60 * 60;
// Bound what one session may store: newest messages win.
const MAX_MESSAGES = 50;
const MAX_MESSAGES_BYTES = 200_000;
const MAX_TITLE_LEN = 120;

const doc = DynamoDBDocumentClient.from(new DynamoDBClient({ region: REGION }), {
  marshallOptions: { removeUndefinedValues: true },
});

export function sessionStoreConfigured(): boolean {
  return Boolean(TABLE);
}

export type StoredMessage = Record<string, unknown>;

export type SessionSummary = {
  id: string;
  title: string;
  updatedAt: string;
  messageCount: number;
};

export type SessionDetail = SessionSummary & { messages: StoredMessage[]; createdAt: string };

export type UserSettings = { confidential: boolean; debug: boolean };

const pkOf = (sub: string) => `USER#${sub}`;
const skOf = (id: string) => `SESSION#${id}`;

/** Trim to the newest messages within count/size bounds and drop transient flags. */
export function boundMessages(messages: StoredMessage[]): StoredMessage[] {
  const cleaned = messages
    .filter((m) => m && typeof m === "object")
    .map((m) => {
      const { loading, ...rest } = m as { loading?: unknown } & StoredMessage;
      void loading;
      return rest;
    })
    .slice(-MAX_MESSAGES);
  while (cleaned.length > 1 && JSON.stringify(cleaned).length > MAX_MESSAGES_BYTES) {
    cleaned.shift();
  }
  return cleaned;
}

export async function listSessions(sub: string): Promise<SessionSummary[]> {
  const out = await doc.send(
    new QueryCommand({
      TableName: TABLE,
      KeyConditionExpression: "pk = :pk AND begins_with(sk, :sk)",
      ExpressionAttributeValues: { ":pk": pkOf(sub), ":sk": "SESSION#" },
      ProjectionExpression: "sk, title, updatedAt, messageCount",
    }),
  );
  return (out.Items ?? [])
    .map((it) => ({
      id: String(it.sk).slice("SESSION#".length),
      title: String(it.title ?? "Untitled"),
      updatedAt: String(it.updatedAt ?? ""),
      messageCount: Number(it.messageCount ?? 0),
    }))
    .sort((a, b) => (a.updatedAt < b.updatedAt ? 1 : -1));
}

export async function getSession(sub: string, id: string): Promise<SessionDetail | null> {
  const out = await doc.send(
    new GetCommand({ TableName: TABLE, Key: { pk: pkOf(sub), sk: skOf(id) } }),
  );
  if (!out.Item) return null;
  let messages: StoredMessage[] = [];
  try {
    const parsed: unknown = JSON.parse(String(out.Item.messages ?? "[]"));
    if (Array.isArray(parsed)) messages = parsed as StoredMessage[];
  } catch {
    /* corrupt payload -> empty conversation, still openable */
  }
  return {
    id,
    title: String(out.Item.title ?? "Untitled"),
    createdAt: String(out.Item.createdAt ?? ""),
    updatedAt: String(out.Item.updatedAt ?? ""),
    messageCount: Number(out.Item.messageCount ?? messages.length),
    messages,
  };
}

export async function putSession(
  sub: string,
  args: { id?: string; title: string; messages: StoredMessage[]; createdAt?: string },
): Promise<{ id: string }> {
  const id = args.id ?? randomUUID();
  const now = new Date().toISOString();
  const bounded = boundMessages(args.messages);
  await doc.send(
    new PutCommand({
      TableName: TABLE,
      Item: {
        pk: pkOf(sub),
        sk: skOf(id),
        title: args.title.slice(0, MAX_TITLE_LEN) || "Untitled",
        messages: JSON.stringify(bounded),
        messageCount: bounded.length,
        createdAt: args.createdAt ?? now,
        updatedAt: now,
        expiresAt: Math.floor(Date.now() / 1000) + SESSION_TTL_SECONDS,
      },
    }),
  );
  return { id };
}

export async function deleteSession(sub: string, id: string): Promise<void> {
  await doc.send(new DeleteCommand({ TableName: TABLE, Key: { pk: pkOf(sub), sk: skOf(id) } }));
}

/**
 * Sign-out revocation marker: the newest sign-out time per user. Stateless ID tokens
 * cannot be invalidated, so the data plane rejects any token issued before this instant
 * (see lib/authGuard.ts). Kept slightly beyond the maximum token lifetime.
 */
const REVOCATION_TTL_SECONDS = 24 * 60 * 60;

export async function markSignedOut(sub: string): Promise<void> {
  const now = Math.floor(Date.now() / 1000);
  await doc.send(
    new PutCommand({
      TableName: TABLE,
      Item: {
        pk: pkOf(sub),
        sk: "REVOCATION",
        revokedAt: now,
        expiresAt: now + REVOCATION_TTL_SECONDS,
      },
    }),
  );
}

/** Unix seconds of the user's last sign-out, or 0 when there is none on record. */
export async function signedOutAt(sub: string): Promise<number> {
  const out = await doc.send(
    new GetCommand({ TableName: TABLE, Key: { pk: pkOf(sub), sk: "REVOCATION" } }),
  );
  const value = out.Item?.revokedAt;
  return typeof value === "number" ? value : 0;
}

// Debug mode ships the full technical trace. Default is off (least surprise for a
// consumer-facing deployment); the demo turns it on via DEFAULT_DEBUG_MODE=true.
const DEBUG_DEFAULT = (process.env.DEFAULT_DEBUG_MODE ?? "").toLowerCase() === "true";

const DEFAULT_SETTINGS: UserSettings = { confidential: false, debug: DEBUG_DEFAULT };

/** The deployment's debug default — also the answer when no session store exists. */
export function debugDefault(): boolean {
  return DEBUG_DEFAULT;
}

export async function getSettings(sub: string): Promise<UserSettings> {
  const out = await doc.send(
    new GetCommand({ TableName: TABLE, Key: { pk: pkOf(sub), sk: "SETTINGS" } }),
  );
  if (!out.Item) return { ...DEFAULT_SETTINGS };
  return {
    confidential: Boolean(out.Item.confidential),
    debug: out.Item.debug === undefined ? DEFAULT_SETTINGS.debug : Boolean(out.Item.debug),
  };
}

export async function putSettings(sub: string, settings: UserSettings): Promise<void> {
  await doc.send(
    new PutCommand({
      TableName: TABLE,
      Item: {
        pk: pkOf(sub),
        sk: "SETTINGS",
        confidential: Boolean(settings.confidential),
        debug: Boolean(settings.debug),
      },
    }),
  );
}
