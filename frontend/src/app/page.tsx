"use client";

import { useCallback, useEffect, useRef, useState } from "react";

// Source is generated from the Python contract (lambdas/shared/.../contracts.py);
// `make gen-types-check` fails the build if it drifts.
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import type { Source } from "@/lib/generated/source";
import {
  displayFlag,
  displayLabel,
  enabledJurisdictions,
  jurisdictionMeta,
} from "@/lib/jurisdictions";
import { MESSAGES } from "@/lib/messages";

type Step =
  | { type: "reasoning"; text: string }
  | { type: "tool_call"; tool?: string; jurisdiction?: string; input?: Record<string, unknown> }
  | { type: "tool_result"; count?: number | null; raw?: string | null; truncated?: boolean }
  | { type: "model_call"; model?: string }
  | { type: "model_stop"; stop_reason?: string | null }
  | {
      type: "model_metrics";
      input_tokens?: number | null;
      output_tokens?: number | null;
      total_tokens?: number | null;
      latency_ms?: number | null;
    }
  | { type: "guardrail"; action?: string; detail?: string }
  | { type: "persist"; text: string };

type ChatMessage = {
  id: number;
  role: "user" | "assistant";
  text: string;
  sources?: Source[];
  steps?: Step[];
  loading?: boolean;
};

type SessionSummary = { id: string; title: string; updatedAt: string; messageCount: number };
type Settings = { confidential: boolean; debug: boolean; persistence: boolean };

const MAX_LEN = 500;

/**
 * Example questions shown on the empty state, seeded per ENABLED jurisdiction only — the UI
 * must never advertise a parliament we cannot answer for.
 */
const EXAMPLES_BY_JURISDICTION: Record<string, string[]> = {
  de: [
    "Speeches by Hubertus Heil in the 21st electoral period",
    "What was said about pensions in the Bundestag in 2024?",
    "Bundestag debates on artificial intelligence",
  ],
  uk: [
    "What did MPs say about flood protection in the Commons?",
    "Commons debates on climate change in 2024",
  ],
  eu: [
    "European Parliament debates on the AI Act",
    "What did MEPs say about energy prices?",
  ],
  ch: ["Nationalrat debates on climate policy"],
  at: ["Nationalrat speeches on energy prices"],
  us: ["What was said in Congress about infrastructure spending?"],
  ca: ["House of Commons debates on housing"],
  au: ["What did members say about energy policy?"],
  fr: ["Débats à l'Assemblée nationale sur les retraites"],
  nl: ["Tweede Kamer debatten over stikstof"],
};

const EXAMPLES: string[] = enabledJurisdictions().flatMap(
  (j) => EXAMPLES_BY_JURISDICTION[j.key] ?? [],
);

/** Subtitle naming the parliaments actually covered (derived from the enabled list). */
const COVERAGE_SUBTITLE: string = (() => {
  const names = enabledJurisdictions().map((j) => j.label);
  if (names.length === 0) return "Questions about parliamentary debates and speeches";
  if (names.length === 1) return `Questions about debates and speeches in the ${names[0]}`;
  if (names.length <= 3) {
    const head = names.slice(0, -1).join(", ");
    return `Questions about debates and speeches in the ${head} and ${names[names.length - 1]}`;
  }
  return `Questions about debates and speeches in ${names.length} parliaments`;
})();

type SetMessages = React.Dispatch<React.SetStateAction<ChatMessage[]>>;

/** Replace the last (assistant) message in the list. */
function patchLast(setMessages: SetMessages, patch: (m: ChatMessage) => ChatMessage) {
  setMessages((m) => {
    const next = [...m];
    next[next.length - 1] = patch(next[next.length - 1]);
    return next;
  });
}

/**
 * Read the SSE stream and update the assistant message live as events arrive.
 * Returns true when a final `answer` event was received; the caller finalises the message
 * with an error state otherwise, so an interrupted stream never leaves a stuck spinner.
 */
async function consumeStream(
  body: ReadableStream<Uint8Array>,
  setMessages: SetMessages,
): Promise<boolean> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let sawAnswer = false;

  const handle = (event: Record<string, unknown>) => {
    const type = event.type;
    if (type === "answer") {
      sawAnswer = true;
      patchLast(setMessages, (m) => ({
        ...m,
        loading: false,
        text: typeof event.answer === "string" ? event.answer : "",
        sources: Array.isArray(event.sources) ? (event.sources as Source[]) : [],
      }));
    } else if (
      type === "reasoning" ||
      type === "tool_call" ||
      type === "tool_result" ||
      type === "model_call" ||
      type === "model_stop" ||
      type === "model_metrics" ||
      type === "guardrail"
    ) {
      patchLast(setMessages, (m) => ({
        ...m,
        steps: [...(m.steps ?? []), event as Step],
      }));
    }
  };

  const processBuffer = (flush: boolean) => {
    const frames = buffer.split("\n\n");
    buffer = flush ? "" : frames.pop() ?? "";
    for (const frame of frames) {
      for (const line of frame.split("\n")) {
        const trimmed = line.trim();
        if (!trimmed.startsWith("data:")) continue;
        const json = trimmed.slice(5).trim();
        if (!json) continue;
        try {
          handle(JSON.parse(json));
        } catch {
          /* ignore malformed frame */
        }
      }
    }
  };

  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      processBuffer(false);
    }
    buffer += decoder.decode();
    processBuffer(true);
  } finally {
    reader.releaseLock();
  }
  return sawAnswer;
}

function sessionIdFromUrl(): string | null {
  const id = new URLSearchParams(window.location.search).get("s");
  return id && /^[a-zA-Z0-9-]{1,64}$/.test(id) ? id : null;
}

function setSessionIdInUrl(id: string | null) {
  const url = new URL(window.location.href);
  if (id) url.searchParams.set("s", id);
  else url.searchParams.delete("s");
  window.history.replaceState(null, "", url);
}

export default function Home() {
  const [question, setQuestion] = useState("");
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [loading, setLoading] = useState(false);
  const [settings, setSettings] = useState<Settings>({
    confidential: false,
    // Conservative pre-fetch default; /api/settings delivers the account's real value
    // (and the deployment default) on mount. Never default the trace to visible.
    debug: false,
    persistence: false,
  });
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const endRef = useRef<HTMLDivElement>(null);
  const nextId = useRef(1);
  const messagesRef = useRef<ChatMessage[]>([]);
  messagesRef.current = messages;

  const refreshSessions = useCallback(async () => {
    try {
      const res = await fetch("/api/sessions");
      if (!res.ok) return;
      const data = await res.json();
      if (Array.isArray(data.sessions)) setSessions(data.sessions);
    } catch {
      /* sidebar stays as-is */
    }
  }, []);

  // Initial load: settings, session list, and (via ?s=) a previously persisted session.
  useEffect(() => {
    void (async () => {
      try {
        const res = await fetch("/api/settings");
        if (res.ok) {
          const s = await res.json();
          setSettings({
            confidential: Boolean(s.confidential),
            debug: Boolean(s.debug),
            persistence: Boolean(s.persistence),
          });
        }
      } catch {
        /* defaults stand */
      }
      await refreshSessions();
      const fromUrl = sessionIdFromUrl();
      if (fromUrl) await openSession(fromUrl);
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  async function saveSettings(next: Omit<Settings, "persistence">) {
    const merged = { ...settings, ...next };
    setSettings(merged);
    try {
      await fetch("/api/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ confidential: merged.confidential, debug: merged.debug }),
      });
    } catch {
      /* optimistic update stands; next GET reconciles */
    }
  }

  function newChat() {
    setMessages([]);
    setActiveSessionId(null);
    setSessionIdInUrl(null);
    setSidebarOpen(false);
  }

  async function openSession(id: string) {
    try {
      const res = await fetch(`/api/sessions/${id}`);
      if (!res.ok) return;
      const data = await res.json();
      const restored: ChatMessage[] = Array.isArray(data.messages)
        ? (data.messages as ChatMessage[]).filter(
            (m) => m && (m.role === "user" || m.role === "assistant") && typeof m.text === "string",
          )
        : [];
      restored.forEach((m, i) => {
        m.id = typeof m.id === "number" ? m.id : i + 1;
      });
      nextId.current = restored.reduce((max, m) => Math.max(max, m.id), 0) + 1;
      setMessages(restored);
      setActiveSessionId(id);
      setSessionIdInUrl(id);
      setSidebarOpen(false);
    } catch {
      /* leave current chat untouched */
    }
  }

  async function removeSession(id: string) {
    try {
      await fetch(`/api/sessions/${id}`, { method: "DELETE" });
    } catch {
      /* list refresh below reconciles */
    }
    if (id === activeSessionId) newChat();
    await refreshSessions();
  }

  /** Persist the finished exchange — or record why we did not. */
  async function persistAfterAnswer() {
    const current = messagesRef.current;
    const note = (text: string) =>
      patchLast(setMessages, (m) => ({ ...m, steps: [...(m.steps ?? []), { type: "persist", text }] }));

    if (!settings.persistence) return;
    if (settings.confidential) {
      note("Persistence skipped — confidential mode is on");
      return;
    }
    try {
      if (!activeSessionId) {
        const title = current.find((m) => m.role === "user")?.text.slice(0, 120) ?? "Untitled";
        const res = await fetch("/api/sessions", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ title, messages: current }),
        });
        if (res.ok) {
          const { id } = await res.json();
          setActiveSessionId(id);
          setSessionIdInUrl(id);
          note("Session saved");
        } else {
          note("Session not saved");
        }
      } else {
        const res = await fetch(`/api/sessions/${activeSessionId}`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ messages: current }),
        });
        note(res.ok ? "Session saved" : "Session not saved");
      }
    } catch {
      note("Session not saved");
    }
    await refreshSessions();
  }

  async function ask(raw: string) {
    const q = raw.trim();
    if (!q || loading) return;
    setLoading(true);
    setQuestion("");
    // Snapshot prior turns (completed messages) as context for this follow-up.
    const history = messages
      .filter((m) => m.text.trim().length > 0)
      .map((m) => ({ role: m.role, text: m.text }));
    setMessages((m) => [
      ...m,
      { id: nextId.current++, role: "user", text: q },
      { id: nextId.current++, role: "assistant", text: "", steps: [], loading: true },
    ]);
    try {
      const res = await fetch("/api/ask", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question: q.slice(0, MAX_LEN), history }),
        credentials: "same-origin",
      });

      const ct = res.headers.get("content-type") ?? "";
      if (ct.includes("text/event-stream") && res.body) {
        const answered = await consumeStream(res.body, setMessages);
        if (!answered) {
          patchLast(setMessages, (m) => ({
            ...m,
            loading: false,
            text: m.text || "The response was interrupted. Please try again.",
          }));
        }
      } else {
        const data = await res.json();
        patchLast(setMessages, (m) => ({
          ...m,
          loading: false,
          text: typeof data.answer === "string" ? data.answer : "",
          sources: Array.isArray(data.sources) ? data.sources : [],
          steps: Array.isArray(data.steps) ? data.steps : [],
        }));
      }
      await persistAfterAnswer();
    } catch {
      patchLast(setMessages, (m) => ({
        ...m,
        loading: false,
        text: "An error occurred. Please try again.",
      }));
    } finally {
      setLoading(false);
    }
  }

  function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    void ask(question);
  }

  async function logout() {
    try {
      const res = await fetch("/api/auth/logout", { method: "POST" });
      const data = await res.json().catch(() => null);
      window.location.href = typeof data?.redirect === "string" ? data.redirect : "/";
    } catch {
      window.location.href = "/";
    }
  }

  return (
    <main className="app-shell">
      <header className="top-nav">
        <div className="top-nav-inner">
          <div className="top-nav-left">
            {settings.persistence && (
              <button
                type="button"
                className="btn-header sidebar-toggle"
                onClick={() => setSidebarOpen((o) => !o)}
                aria-label="Toggle chat history"
              >
                ☰
              </button>
            )}
            <div>
              <h1 className="top-nav-title">
                {MESSAGES.appTitlePrefix}
                <span className="title-accent">{MESSAGES.appTitleAccent}</span>
              </h1>
              <p className="top-nav-subtitle">{COVERAGE_SUBTITLE}</p>
            </div>
          </div>
          <div className="top-nav-actions">
            {settings.persistence && (
              <Toggle
                label="Confidential"
                checked={settings.confidential}
                onChange={(v) => void saveSettings({ confidential: v, debug: settings.debug })}
              />
            )}
            <Toggle
              label="Debug"
              checked={settings.debug}
              onChange={(v) => void saveSettings({ confidential: settings.confidential, debug: v })}
            />
            <button type="button" onClick={() => void logout()} className="btn-header">
              {MESSAGES.signOut}
            </button>
          </div>
        </div>
        {settings.confidential && (
          <div className="confidential-banner" role="status">
            {MESSAGES.confidentialBanner}
          </div>
        )}
      </header>

      <div className="app-body">
        {settings.persistence && (
          <aside className={`sidebar${sidebarOpen ? " sidebar-open" : ""}`}>
            <button type="button" className="btn btn-normal sidebar-new" onClick={newChat}>
              + New chat
            </button>
            <div className="sidebar-list">
              {sessions.length === 0 && <p className="sidebar-empty">{MESSAGES.noSavedChats}</p>}
              {sessions.map((s) => (
                <div
                  key={s.id}
                  className={`sidebar-item${s.id === activeSessionId ? " sidebar-item-active" : ""}`}
                >
                  <button
                    type="button"
                    className="sidebar-item-open"
                    title={s.title}
                    onClick={() => void openSession(s.id)}
                  >
                    {s.title}
                  </button>
                  <button
                    type="button"
                    className="sidebar-item-delete"
                    aria-label={`Delete chat: ${s.title}`}
                    onClick={() => void removeSession(s.id)}
                  >
                    ×
                  </button>
                </div>
              ))}
            </div>
          </aside>
        )}

        <div className="chat-scroll">
          <div className="content-column chat-thread">
            {messages.length === 0 && (
              <div className="empty-state">
                <h2 className="empty-state-title">{MESSAGES.emptyStateTitle}</h2>
                <p className="empty-state-hint">{MESSAGES.emptyStateHint}</p>
                <div className="example-chips">
                  {EXAMPLES.map((ex) => (
                    <button
                      key={ex}
                      type="button"
                      onClick={() => void ask(ex)}
                      disabled={loading}
                      className="example-chip"
                    >
                      {ex}
                    </button>
                  ))}
                </div>
                <div className="container-card">
                  <div className="container-card-header">{MESSAGES.dataSourcesHeader}</div>
                  <div className="container-card-body sources-panel">
                    <p style={{ margin: "0 0 8px" }}>{MESSAGES.dataSourcesBody}</p>
                    <ul>
                      {enabledJurisdictions().map((j) => (
                        <li key={j.key}>
                          {j.flag} <strong>{j.label}</strong> —{" "}
                          <a href={j.sourceUrl} target="_blank" rel="noopener noreferrer">
                            {j.sourceName}
                          </a>
                          <br />
                          <span className="sources-attribution">{j.attribution}</span>
                        </li>
                      ))}
                    </ul>
                  </div>
                </div>
              </div>
            )}
            {messages.map((m) => (
              <MessageBubble key={m.id} message={m} debug={settings.debug} />
            ))}
            <div ref={endRef} />
          </div>
        </div>
      </div>

      <form onSubmit={onSubmit} className="input-bar">
        <div className="input-bar-inner">
          <input
            autoFocus
            aria-label="Question"
            placeholder="Your question…"
            value={question}
            maxLength={MAX_LEN}
            disabled={loading}
            onChange={(e) => setQuestion(e.target.value)}
            className="input-field"
          />
          <button type="submit" disabled={loading || !question.trim()} className="btn btn-primary">
            {loading ? "…" : "Send"}
          </button>
        </div>
      </form>
    </main>
  );
}

function Toggle({
  label,
  checked,
  onChange,
}: {
  label: string;
  checked: boolean;
  onChange: (value: boolean) => void;
}) {
  return (
    <label className="toggle">
      <span className="toggle-label">{label}</span>
      <input
        type="checkbox"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
        role="switch"
        aria-checked={checked}
      />
      <span className="toggle-slider" aria-hidden="true" />
    </label>
  );
}

function MessageBubble({ message, debug }: { message: ChatMessage; debug: boolean }) {
  if (message.role === "user") {
    return (
      <div className="chat-user-row">
        <div className="chat-user-bubble">{message.text}</div>
      </div>
    );
  }

  const thinking = message.loading;
  const steps = debug ? message.steps ?? [] : [];
  return (
    <div className="chat-assistant-row">
      {steps.length > 0 && <Trace steps={steps} live={thinking} />}
      {thinking ? (
        (!debug || steps.length === 0) && <p className="chat-pending">{MESSAGES.workingOnIt}</p>
      ) : (
        <div className="container-card">
          <div className="container-card-body">
            {message.text && (
              <div className="chat-answer">
                <ReactMarkdown
                  remarkPlugins={[remarkGfm]}
                  components={{
                    a: ({ children, href }) => (
                      <a href={href} target="_blank" rel="noopener noreferrer">
                        {children}
                      </a>
                    ),
                  }}
                >
                  {message.text}
                </ReactMarkdown>
              </div>
            )}
            {message.sources && message.sources.length > 0 && (
              <Citations sources={message.sources} />
            )}
          </div>
        </div>
      )}
    </div>
  );
}

/**
 * Render a tool-call step label. Gateway tool names arrive namespaced as
 * `{jurisdiction}___{tool}`; show "Germany · search_debates" instead.
 */
function formatToolLabel(step: { tool?: string; jurisdiction?: string }): string {
  const raw = step.tool ?? "tool";
  const [prefix, suffix] = raw.includes("___") ? raw.split("___", 2) : [undefined, raw];
  const key = step.jurisdiction ?? prefix;
  if (!key) return suffix;
  const meta = jurisdictionMeta(key);
  const label = meta?.label ?? key.charAt(0).toUpperCase() + key.slice(1);
  return `${label} · ${suffix}`;
}

/**
 * Only http(s) URLs may become citation links: source_url originates from upstream
 * parliament APIs (a lower-trust source), and an anchor href is an injection sink for
 * javascript:/data: schemes. See docs/threat-model.md (T-U2).
 */
function safeHttpUrl(url: string | null | undefined): url is string {
  return typeof url === "string" && /^https?:\/\//i.test(url);
}

/**
 * Citation list, grouped by parliament. The jurisdiction label and the machine-translation
 * / transcript-status badges are correctness requirements, not polish: presenting MT text
 * as a verbatim quote would be a factual claim we cannot support.
 */
function Citations({ sources }: { sources: Source[] }) {
  const shown = sources.slice(0, 10);
  const groups = new Map<string, Source[]>();
  for (const s of shown) {
    const key = s.jurisdiction ?? "unknown";
    const list = groups.get(key);
    if (list) list.push(s);
    else groups.set(key, [s]);
  }
  const multi = groups.size > 1;

  return (
    <div className="citations">
      <p className="citations-heading">{MESSAGES.sourcesHeading}</p>
      {Array.from(groups.entries()).map(([key, rows]) => (
        <div key={key} style={{ marginBottom: multi ? 12 : 0 }}>
          {multi && (
            <p className="citations-group-label">
              {displayFlag(key)} {displayLabel(key, rows[0]?.jurisdiction_label)}
            </p>
          )}
          <ul>
            {rows.map((s, i) => (
              <li key={i}>
                {!multi && (
                  <span title={displayLabel(s.jurisdiction, s.jurisdiction_label)}>
                    {displayFlag(s.jurisdiction)}{" "}
                  </span>
                )}
                {[s.title, s.speaker, s.group ?? s.party, s.date, s.session_ref]
                  .filter(Boolean)
                  .join(" · ")}
                {s.is_translation && (
                  <span className="badge badge-caveat">
                    {MESSAGES.machineTranslation}
                    {s.language_original ? ` — original: ${s.language_original}` : ""}
                  </span>
                )}
                {s.text_status && s.text_status !== "final" && (
                  <span className="badge badge-caveat">
                    {s.text_status === "scanned" ? "scanned transcript" : "uncorrected transcript"}
                  </span>
                )}
                {safeHttpUrl(s.source_url) ? (
                  <>
                    {" "}
                    <a href={s.source_url} target="_blank" rel="noopener noreferrer">
                      {MESSAGES.sourceLink}
                    </a>
                  </>
                ) : null}
              </li>
            ))}
          </ul>
        </div>
      ))}
    </div>
  );
}

/** Collapsible raw payload viewer (request/response bodies, capped server-side). */
function RawBlock({ label, content }: { label: string; content: string }) {
  return (
    <details className="raw-block">
      <summary>{label}</summary>
      <pre>{content}</pre>
    </details>
  );
}

/**
 * The transparent pipeline trace (debug mode): reasoning, tools, guardrails, persistence.
 * Stays expanded after the answer — collapsing it automatically hid the entire feature;
 * debug mode users explicitly asked for this view, so let them collapse it themselves.
 */
function Trace({ steps, live }: { steps: Step[]; live?: boolean }) {
  const [open, setOpen] = useState(true);

  return (
    <div className="reasoning-trace">
      <button onClick={() => setOpen((o) => !o)} className="btn-link">
        {open ? "▾" : "▸"} Trace ({steps.length} steps){live ? " · running…" : ""}
      </button>
      {open && (
        <div className="reasoning-steps">
          {steps.map((s, i) => {
            if (s.type === "reasoning") {
              return (
                <div key={i} className="reasoning-text">
                  {s.text}
                </div>
              );
            }
            if (s.type === "model_call") {
              return (
                <div key={i} className="step-stage">
                  <span className="step-stage-label">{MESSAGES.stageBedrock}</span> Converse → {s.model ?? "model"}
                </div>
              );
            }
            if (s.type === "model_stop") {
              return (
                <div key={i} className="step-stage">
                  <span className="step-stage-label">{MESSAGES.stageBedrock}</span> stop reason:{" "}
                  <code>{s.stop_reason ?? "unknown"}</code>
                </div>
              );
            }
            if (s.type === "model_metrics") {
              const parts = [
                s.input_tokens != null ? `${s.input_tokens} in` : null,
                s.output_tokens != null ? `${s.output_tokens} out` : null,
                s.latency_ms != null ? `${(s.latency_ms / 1000).toFixed(1)} s` : null,
              ].filter(Boolean);
              return (
                <div key={i} className="step-stage">
                  <span className="step-stage-label">{MESSAGES.stageBedrock}</span> tokens/latency:{" "}
                  {parts.length ? parts.join(" · ") : "n/a"}
                </div>
              );
            }
            if (s.type === "tool_call") {
              return (
                <div key={i}>
                  <div className="reasoning-tool-call">
                    <span className="step-stage-label">{MESSAGES.stageLambda}</span> {formatToolLabel(s)}
                  </div>
                  {s.input && Object.keys(s.input).length > 0 && (
                    <RawBlock label="request payload" content={JSON.stringify(s.input, null, 2)} />
                  )}
                </div>
              );
            }
            if (s.type === "tool_result") {
              return (
                <div key={i}>
                  <div className="reasoning-tool-result">
                    <span className="step-stage-label">{MESSAGES.stageLambda}</span> {s.count ?? 0} results
                  </div>
                  {s.raw && (
                    <RawBlock
                      label={`response payload${s.truncated ? " (truncated)" : ""}`}
                      content={s.raw}
                    />
                  )}
                </div>
              );
            }
            if (s.type === "guardrail") {
              return (
                <div key={i} className="step-guardrail">
                  {MESSAGES.guardrailIntervened}
                  {s.action ? ` — ${s.action.replace(/_/g, " ")}` : ""}
                  {s.detail ? `: ${s.detail}` : ""}
                </div>
              );
            }
            return (
              <div key={i} className="step-persist">
                {s.text}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
