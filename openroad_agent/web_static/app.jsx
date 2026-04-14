const { useEffect, useMemo, useRef, useState } = React;

const ROLE_META = {
  user: { badge: "You", tone: "user" },
  assistant: { badge: "CP", tone: "assistant" },
  error: { badge: "!", tone: "error" },
};

function formatWhen(value) {
  if (!value) {
    return "Just now";
  }

  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }

  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function formatRelative(value) {
  if (!value) {
    return "No activity yet";
  }

  const ts = new Date(value).getTime();
  if (Number.isNaN(ts)) {
    return value;
  }

  const diff = Date.now() - ts;
  const minute = 60 * 1000;
  const hour = 60 * minute;
  const day = 24 * hour;

  if (diff < minute) {
    return "Just now";
  }
  if (diff < hour) {
    return `${Math.round(diff / minute)}m ago`;
  }
  if (diff < day) {
    return `${Math.round(diff / hour)}h ago`;
  }
  return `${Math.round(diff / day)}d ago`;
}

function splitCodeBlocks(content) {
  const parts = content.split(/```/g);
  return parts.map((part, index) => ({
    type: index % 2 === 0 ? "text" : "code",
    value: part.replace(/^\n+|\n+$/g, ""),
    id: `${index}-${part.length}`,
  }));
}

function MessageBody({ content }) {
  const blocks = useMemo(() => splitCodeBlocks(content || ""), [content]);

  return (
    <div className="message-body">
      {blocks.map((block) => {
        if (block.type === "code") {
          return (
            <pre className="message-code" key={block.id}>
              <code>{block.value}</code>
            </pre>
          );
        }

        return block.value.split(/\n{2,}/g).map((paragraph, index) => (
          <p className="message-paragraph" key={`${block.id}-${index}`}>
            {paragraph}
          </p>
        ));
      })}
    </div>
  );
}

function SessionList({ sessions, activeSession, onSelect, onNewSession, mobileOpen, setMobileOpen }) {
  return (
    <aside className={`sidebar ${mobileOpen ? "sidebar-open" : ""}`}>
      <div className="brand">
        <button className="sidebar-close" onClick={() => setMobileOpen(false)} aria-label="Close sidebar">
          ×
        </button>
        <div className="brand-mark" aria-hidden="true">
          <span></span>
          <span></span>
          <span></span>
          <span></span>
        </div>
        <div>
          <p className="brand-kicker">React Frontend</p>
          <h1>Chippilot</h1>
        </div>
      </div>

      <button className="new-chat-button" onClick={onNewSession}>
        <span>+</span>
        <span>New chat</span>
      </button>

      <div className="sidebar-section">
        <div className="sidebar-section-header">
          <span>Recent sessions</span>
          <span>{sessions.length}</span>
        </div>

        <div className="session-list">
          {sessions.length === 0 ? (
            <div className="session-empty">
              <p>No conversations yet.</p>
              <p>Start with a flow request, optimization question, or log analysis task.</p>
            </div>
          ) : (
            sessions.map((session) => (
              <button
                key={session.session_name}
                className={`session-card ${activeSession === session.session_name ? "session-card-active" : ""}`}
                onClick={() => {
                  onSelect(session.session_name);
                  setMobileOpen(false);
                }}
              >
                <div className="session-card-top">
                  <strong>{session.title}</strong>
                  <span>{formatRelative(session.last_message_at || session.created)}</span>
                </div>
                <p>{session.preview || "Fresh workspace ready for the next chip task."}</p>
                <div className="session-meta">
                  <span>{session.run_count} runs</span>
                  <span>{session.design_file_count} files</span>
                </div>
              </button>
            ))
          )}
        </div>
      </div>
    </aside>
  );
}

function EmptyState({ prompts, onPrompt }) {
  return (
    <div className="empty-state">
      <div className="empty-state-copy">
        <span className="eyebrow">ChatGPT-inspired workflow shell</span>
        <h2>What are we taping out today?</h2>
        <p>
          Ask Chippilot to run OpenROAD flows, debug synthesis and routing issues,
          compare metrics, or plan the next optimization move.
        </p>
      </div>

      <div className="prompt-grid">
        {prompts.map((prompt) => (
          <button key={prompt.title} className="prompt-card" onClick={() => onPrompt(prompt.prompt)}>
            <strong>{prompt.title}</strong>
            <span>{prompt.prompt}</span>
          </button>
        ))}
      </div>
    </div>
  );
}

function MessageItem({ message }) {
  if (message.kind === "activity") {
    return (
      <div className="activity-row">
        <div className="activity-label">{message.label}</div>
        <pre className="activity-content">{message.content}</pre>
      </div>
    );
  }

  const meta = ROLE_META[message.role] || ROLE_META.assistant;

  return (
    <div className={`message-row message-row-${meta.tone}`}>
      <div className={`avatar avatar-${meta.tone}`}>{meta.badge}</div>
      <div className={`message-card message-card-${meta.tone}`}>
        <div className="message-head">
          <strong>{message.role === "assistant" ? "Chippilot" : message.label}</strong>
          <span>{formatWhen(message.ts)}</span>
        </div>
        <MessageBody content={message.content} />
      </div>
    </div>
  );
}

function Composer({ draft, setDraft, onSend, busy }) {
  const textareaRef = useRef(null);

  useEffect(() => {
    const el = textareaRef.current;
    if (!el) {
      return;
    }
    el.style.height = "0px";
    el.style.height = `${Math.min(el.scrollHeight, 240)}px`;
  }, [draft]);

  return (
    <div className="composer-shell">
      <div className="composer-card">
        <textarea
          ref={textareaRef}
          className="composer-input"
          placeholder="Message Chippilot about your flow, logs, or optimization goals..."
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              onSend();
            }
          }}
          disabled={busy}
        />
        <div className="composer-footer">
          <div className="composer-hints">
            <span>Shift + Enter for newline</span>
            <span>OpenROAD-aware agent</span>
          </div>
          <button className="send-button" onClick={() => onSend()} disabled={busy || !draft.trim()}>
            {busy ? "Thinking..." : "Send"}
          </button>
        </div>
      </div>
    </div>
  );
}

function App() {
  const [sessions, setSessions] = useState([]);
  const [starterPrompts, setStarterPrompts] = useState([]);
  const [activeSession, setActiveSession] = useState(null);
  const [activeDetail, setActiveDetail] = useState(null);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [mobileOpen, setMobileOpen] = useState(false);
  const messagesEndRef = useRef(null);

  async function fetchJson(path, options = {}) {
    const response = await fetch(path, {
      headers: {
        "Content-Type": "application/json",
        ...(options.headers || {}),
      },
      ...options,
    });

    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      throw new Error(payload.detail || `Request failed with ${response.status}`);
    }

    return response.json();
  }

  async function loadBootstrap() {
    const data = await fetchJson("/api/bootstrap");
    setSessions(data.sessions || []);
    setStarterPrompts(data.starter_prompts || []);
    if (data.sessions && data.sessions.length > 0) {
      await loadSession(data.sessions[0].session_name, false);
    }
  }

  async function loadSession(sessionName, markActive = true) {
    const detail = await fetchJson(`/api/sessions/${encodeURIComponent(sessionName)}`);
    setActiveDetail(detail);
    if (markActive) {
      setActiveSession(sessionName);
    } else {
      setActiveSession(detail.session_name);
    }
  }

  async function refreshSessions(preferredSession = null) {
    const data = await fetchJson("/api/sessions");
    setSessions(data.sessions || []);
    if (preferredSession) {
      const exists = (data.sessions || []).some((item) => item.session_name === preferredSession);
      if (!exists) {
        setActiveSession(null);
        setActiveDetail(null);
      }
    }
  }

  useEffect(() => {
    loadBootstrap().catch((err) => setError(err.message));
  }, []);

  useEffect(() => {
    if (messagesEndRef.current) {
      messagesEndRef.current.scrollIntoView({ behavior: "smooth", block: "end" });
    }
  }, [activeDetail, busy]);

  async function handleSend(overrideText) {
    const content = (overrideText || draft).trim();
    if (!content || busy) {
      return;
    }

    setBusy(true);
    setError("");

    const optimisticId = `optimistic-${Date.now()}`;
    const optimisticMessage = {
      id: optimisticId,
      role: "user",
      kind: "message",
      label: "You",
      content,
      ts: new Date().toISOString(),
    };

    const previousDetail = activeDetail;

    setDraft("");
    setActiveDetail((current) => {
      if (!current) {
        return {
          session_name: null,
          title: "New chat",
          messages: [optimisticMessage],
          run_count: 0,
          design_file_count: 0,
          created: new Date().toISOString(),
        };
      }

      return {
        ...current,
        messages: [...(current.messages || []), optimisticMessage],
      };
    });

    try {
      const payload = await fetchJson("/api/chat", {
        method: "POST",
        body: JSON.stringify({
          message: content,
          session_name: activeSession,
        }),
      });

      setActiveSession(payload.session.session_name);
      setActiveDetail(payload.session);
      await refreshSessions(payload.session.session_name);
    } catch (err) {
      setActiveDetail(previousDetail);
      setError(err.message);
      setDraft(content);
    } finally {
      setBusy(false);
    }
  }

  function handleNewSession() {
    setActiveSession(null);
    setActiveDetail(null);
    setDraft("");
    setError("");
    setMobileOpen(false);
  }

  const sessionHeader = activeDetail || {
    title: "New chat",
    created: "",
    run_count: 0,
    design_file_count: 0,
    messages: [],
  };

  const hasMessages = (activeDetail?.messages || []).length > 0;

  return (
    <div className="app-shell">
      <div className={`app-frame ${mobileOpen ? "frame-sidebar-open" : ""}`}>
        <SessionList
          sessions={sessions}
          activeSession={activeSession}
          onSelect={loadSession}
          onNewSession={handleNewSession}
          mobileOpen={mobileOpen}
          setMobileOpen={setMobileOpen}
        />

        <main className="main-panel">
          <header className="topbar">
            <button className="menu-button" onClick={() => setMobileOpen(true)} aria-label="Open sidebar">
              ☰
            </button>

            <div className="topbar-copy">
              <p className="topbar-kicker">Chippilot agent workspace</p>
              <h2>{sessionHeader.title || "New chat"}</h2>
            </div>

            <div className="topbar-meta">
              <span>{sessionHeader.run_count || 0} runs</span>
              <span>{sessionHeader.design_file_count || 0} design files</span>
              <span>{formatWhen(sessionHeader.created)}</span>
            </div>
          </header>

          <section className="conversation">
            {error ? <div className="error-banner">{error}</div> : null}

            {!hasMessages ? (
              <EmptyState prompts={starterPrompts} onPrompt={handleSend} />
            ) : (
              <div className="message-list">
                {activeDetail.messages.map((message) => (
                  <MessageItem key={message.id} message={message} />
                ))}

                {busy ? (
                  <div className="message-row message-row-assistant">
                    <div className="avatar avatar-assistant">CP</div>
                    <div className="message-card message-card-assistant typing-card">
                      <div className="typing-dots">
                        <span></span>
                        <span></span>
                        <span></span>
                      </div>
                    </div>
                  </div>
                ) : null}
                <div ref={messagesEndRef}></div>
              </div>
            )}
          </section>

          <Composer draft={draft} setDraft={setDraft} onSend={handleSend} busy={busy} />
        </main>
      </div>
    </div>
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(<App />);
