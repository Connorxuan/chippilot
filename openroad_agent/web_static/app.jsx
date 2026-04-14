const { useEffect, useLayoutEffect, useMemo, useRef, useState } = React;

const AUTO_SCROLL_THRESHOLD_PX = 96;

const ROLE_META = {
  user: { badge: "You", tone: "user" },
  assistant: { badge: <ChipLogo />, tone: "assistant" },
  error: { badge: "!", tone: "error" },
  activity: { badge: "··", tone: "activity" },
};

function ChipLogo() {
  return (
    <svg className="chip-logo" viewBox="0 0 64 64" aria-hidden="true">
      <rect className="chip-logo-bg" width="64" height="64" rx="12" />
      <g className="chip-logo-pins">
        <rect x="11" y="20" width="5" height="4" rx="1" />
        <rect x="11" y="30" width="5" height="4" rx="1" />
        <rect x="11" y="40" width="5" height="4" rx="1" />
        <rect x="48" y="20" width="5" height="4" rx="1" />
        <rect x="48" y="30" width="5" height="4" rx="1" />
        <rect x="48" y="40" width="5" height="4" rx="1" />
        <rect x="20" y="11" width="4" height="5" rx="1" />
        <rect x="30" y="11" width="4" height="5" rx="1" />
        <rect x="40" y="11" width="4" height="5" rx="1" />
        <rect x="20" y="48" width="4" height="5" rx="1" />
        <rect x="30" y="48" width="4" height="5" rx="1" />
        <rect x="40" y="48" width="4" height="5" rx="1" />
      </g>
      <rect className="chip-logo-core" x="18" y="18" width="28" height="28" rx="4" />
      <rect className="chip-logo-die" x="23" y="23" width="18" height="18" rx="2" />
    </svg>
  );
}

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

function formatDuration(seconds) {
  if (seconds === null || seconds === undefined || seconds === "") {
    return "";
  }
  const value = Number(seconds);
  if (!Number.isFinite(value) || value < 0) {
    return "";
  }
  if (value < 1) {
    return "<1s";
  }
  if (value < 10) {
    return `${value.toFixed(1)}s`;
  }
  if (value < 60) {
    return `${Math.round(value)}s`;
  }
  const totalSeconds = Math.round(value);
  const minutes = Math.floor(totalSeconds / 60);
  const remainingSeconds = totalSeconds % 60;
  if (minutes < 60) {
    return `${minutes}m ${String(remainingSeconds).padStart(2, "0")}s`;
  }
  const hours = Math.floor(minutes / 60);
  const remainingMinutes = minutes % 60;
  return `${hours}h ${String(remainingMinutes).padStart(2, "0")}m`;
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

function renderInlineMarkdown(text, keyPrefix) {
  const pattern = /(`[^`]+`|\*\*[^*]+\*\*|\[[^\]]+\]\([^)]+\))/g;
  const parts = text.split(pattern).filter(Boolean);

  return parts.map((part, index) => {
    const key = `${keyPrefix}-${index}`;

    if (part.startsWith("`") && part.endsWith("`")) {
      return <code className="message-inline-code" key={key}>{part.slice(1, -1)}</code>;
    }

    if (part.startsWith("**") && part.endsWith("**")) {
      return <strong key={key}>{part.slice(2, -2)}</strong>;
    }

    const linkMatch = part.match(/^\[([^\]]+)\]\(([^)]+)\)$/);
    if (linkMatch) {
      return (
        <a
          className="message-link"
          href={linkMatch[2]}
          key={key}
          target="_blank"
          rel="noreferrer"
        >
          {linkMatch[1]}
        </a>
      );
    }

    return <React.Fragment key={key}>{part}</React.Fragment>;
  });
}

function renderMarkdownBlock(text, keyPrefix) {
  const lines = text.split("\n");
  const elements = [];
  let paragraph = [];
  let listItems = [];
  let listType = null;

  function flushParagraph() {
    if (!paragraph.length) {
      return;
    }
    elements.push(
      <p className="message-paragraph" key={`${keyPrefix}-p-${elements.length}`}>
        {renderInlineMarkdown(paragraph.join(" "), `${keyPrefix}-p-${elements.length}`)}
      </p>,
    );
    paragraph = [];
  }

  function flushList() {
    if (!listItems.length || !listType) {
      return;
    }
    const Tag = listType === "ol" ? "ol" : "ul";
    elements.push(
      <Tag className="message-list-block" key={`${keyPrefix}-list-${elements.length}`}>
        {listItems.map((item, index) => (
          <li key={`${keyPrefix}-li-${index}`}>
            {renderInlineMarkdown(item, `${keyPrefix}-li-${index}`)}
          </li>
        ))}
      </Tag>,
    );
    listItems = [];
    listType = null;
  }

  lines.forEach((line) => {
    const trimmed = line.trim();

    if (!trimmed) {
      flushParagraph();
      flushList();
      return;
    }

    const headingMatch = trimmed.match(/^(#{1,3})\s+(.*)$/);
    if (headingMatch) {
      flushParagraph();
      flushList();
      const level = Math.min(headingMatch[1].length, 3);
      const Tag = `h${level}`;
      elements.push(
        <Tag className="message-heading" key={`${keyPrefix}-h-${elements.length}`}>
          {renderInlineMarkdown(headingMatch[2], `${keyPrefix}-h-${elements.length}`)}
        </Tag>,
      );
      return;
    }

    const unorderedMatch = trimmed.match(/^[-*]\s+(.*)$/);
    if (unorderedMatch) {
      flushParagraph();
      if (listType && listType !== "ul") {
        flushList();
      }
      listType = "ul";
      listItems.push(unorderedMatch[1]);
      return;
    }

    const orderedMatch = trimmed.match(/^\d+\.\s+(.*)$/);
    if (orderedMatch) {
      flushParagraph();
      if (listType && listType !== "ol") {
        flushList();
      }
      listType = "ol";
      listItems.push(orderedMatch[1]);
      return;
    }

    const quoteMatch = trimmed.match(/^>\s+(.*)$/);
    if (quoteMatch) {
      flushParagraph();
      flushList();
      elements.push(
        <blockquote className="message-quote" key={`${keyPrefix}-q-${elements.length}`}>
          {renderInlineMarkdown(quoteMatch[1], `${keyPrefix}-q-${elements.length}`)}
        </blockquote>,
      );
      return;
    }

    flushList();
    paragraph.push(trimmed);
  });

  flushParagraph();
  flushList();
  return elements;
}

function MessageBody({ content, markdown = false }) {
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

        if (markdown) {
          return (
            <React.Fragment key={block.id}>
              {renderMarkdownBlock(block.value, block.id)}
            </React.Fragment>
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

function summarizeActivity(message) {
  const resultText = message.result_content ? ` -> ${(message.result_content || "").replace(/\s+/g, " ").trim()}` : "";
  const text = `${(message.content || "").replace(/\s+/g, " ").trim()}${resultText}`;
  if (!text) {
    return message.label || "Agent activity";
  }
  return text.length > 120 ? `${text.slice(0, 117)}...` : text;
}

function ActivityBody({ message }) {
  if (message.result_content) {
    const elapsed = formatDuration(message.result_elapsed_s);
    const resultMeta = elapsed || (message.result_ts ? formatWhen(message.result_ts) : "");
    return (
      <div className="activity-merged-body">
        <div className="activity-block">
          <span>Tool call</span>
          <MessageBody content={message.content} />
        </div>
        <div className="activity-block activity-block-result">
          <span>Tool result{resultMeta ? ` · ${resultMeta}` : ""}</span>
          <MessageBody content={message.result_content} />
        </div>
      </div>
    );
  }

  return <MessageBody content={message.content} />;
}

function sameAttachments(left = [], right = []) {
  return left.length === right.length && left.every((item, index) => item === right[index]);
}

function mergeLiveDetail(current, detail) {
  const confirmedMessages = detail.messages || [];
  const optimisticMessages = (current?.messages || []).filter((message) =>
    String(message.id || "").startsWith("optimistic-"),
  );
  const pendingOptimistic = optimisticMessages.filter((optimistic) =>
    !confirmedMessages.some((confirmed) =>
      confirmed.role === "user" &&
      confirmed.content === optimistic.content &&
      sameAttachments(confirmed.attachments || [], optimistic.attachments || []),
    ),
  );

  return {
    ...detail,
    messages: [...confirmedMessages, ...pendingOptimistic],
  };
}

function AttachmentChips({ attachments, onRemove, compact = false }) {
  if (!attachments?.length) {
    return null;
  }

  return (
    <div className={`attachment-list ${compact ? "attachment-list-compact" : ""}`}>
      {attachments.map((attachment) => (
        <div className="attachment-chip" key={attachment}>
          <span className="attachment-chip-name" title={attachment}>{attachment}</span>
          {onRemove ? (
            <button className="attachment-chip-remove" onClick={() => onRemove(attachment)} type="button">
              ×
            </button>
          ) : null}
        </div>
      ))}
    </div>
  );
}

function SessionList({ sessions, activeSession, onSelect, onDelete, onNewSession, mobileOpen, setMobileOpen }) {
  return (
    <aside className={`sidebar ${mobileOpen ? "sidebar-open" : ""}`}>
      <div className="brand">
        <button className="sidebar-close" onClick={() => setMobileOpen(false)} aria-label="Close sidebar">
          ×
        </button>
        <div className="brand-mark" aria-hidden="true">
          <ChipLogo />
        </div>
        <div>
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
              <div
                key={session.session_name}
                className={`session-card ${activeSession === session.session_name ? "session-card-active" : ""}`}
              >
                <button
                  className="session-card-main"
                  onClick={() => {
                    onSelect(session.session_name);
                    setMobileOpen(false);
                  }}
                  type="button"
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
                <button
                  className="session-delete-button"
                  onClick={() => onDelete(session)}
                  title={`Delete ${session.title}`}
                  type="button"
                  aria-label={`Delete ${session.title}`}
                >
                  ×
                </button>
              </div>
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
      <div className="message-row message-row-activity">
        <details className="activity-details">
          <summary className="activity-summary">
            <div className="activity-summary-copy">
              <div className="activity-summary-head">
                <strong>{message.label || "Agent activity"}</strong>
                <span>{formatWhen(message.ts)}</span>
              </div>
              <p>{summarizeActivity(message)}</p>
            </div>
            <span className="activity-summary-chevron">⌄</span>
          </summary>
          <div className="activity-details-body">
            <ActivityBody message={message} />
          </div>
        </details>
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
        {message.attachments?.length ? <AttachmentChips attachments={message.attachments} compact /> : null}
        <MessageBody content={message.content} markdown={message.role === "assistant"} />
      </div>
    </div>
  );
}

function Composer({ draft, setDraft, onSend, onPickFiles, pendingAttachments, onRemoveAttachment, busy, uploading }) {
  const textareaRef = useRef(null);
  const fileInputRef = useRef(null);

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
        <input
          ref={fileInputRef}
          className="file-input-hidden"
          type="file"
          multiple
          onChange={(event) => {
            const files = Array.from(event.target.files || []);
            if (files.length > 0) {
              onPickFiles(files);
            }
            event.target.value = "";
          }}
        />
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
        <div className="composer-attachments">
          <AttachmentChips attachments={pendingAttachments} onRemove={onRemoveAttachment} />
        </div>
        <div className="composer-footer">
          <div className="composer-hints">
            <button
              className="composer-upload"
              type="button"
              onClick={() => fileInputRef.current?.click()}
              disabled={busy || uploading}
            >
              {uploading ? "Uploading..." : "Attach files"}
            </button>
            <span>Shift + Enter for newline</span>
            <span>OpenROAD-aware agent</span>
          </div>
          <button className="send-button" onClick={() => onSend()} disabled={busy || (!draft.trim() && !pendingAttachments.length)}>
            {busy ? "Thinking..." : "Send"}
          </button>
        </div>
      </div>
    </div>
  );
}

function AuthScreen({ mode, setMode, onSubmit, error }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [working, setWorking] = useState(false);
  const isRegister = mode === "register";

  async function handleSubmit(event) {
    event.preventDefault();
    if (!username.trim() || !password || working) {
      return;
    }
    setWorking(true);
    try {
      await onSubmit(username.trim(), password);
    } finally {
      setWorking(false);
    }
  }

  return (
    <div className="auth-shell">
      <form className="auth-panel" onSubmit={handleSubmit}>
        <div className="auth-brand">
          <div className="brand-mark">
            <ChipLogo />
          </div>
          <div>
            <p className="brand-kicker">Chippilot</p>
            <h1>{isRegister ? "Create your workspace" : "Welcome back"}</h1>
          </div>
        </div>

        <label className="auth-field">
          <span>Username</span>
          <input
            value={username}
            onChange={(event) => setUsername(event.target.value)}
            autoComplete="username"
            pattern="[A-Za-z0-9_-]{3,32}"
            minLength="3"
            maxLength="32"
            required
          />
        </label>

        <label className="auth-field">
          <span>Password</span>
          <input
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            autoComplete={isRegister ? "new-password" : "current-password"}
            type="password"
            minLength="8"
            required
          />
        </label>

        {error ? <div className="error-banner auth-error">{error}</div> : null}

        <button className="auth-submit" type="submit" disabled={working}>
          {working ? "Working..." : isRegister ? "Register" : "Log in"}
        </button>

        <button
          className="auth-switch"
          type="button"
          onClick={() => setMode(isRegister ? "login" : "register")}
        >
          {isRegister ? "I already have an account" : "Create an account"}
        </button>
      </form>
    </div>
  );
}

function App() {
  const [authUser, setAuthUser] = useState(null);
  const [authChecked, setAuthChecked] = useState(false);
  const [authMode, setAuthMode] = useState("login");
  const [sessions, setSessions] = useState([]);
  const [starterPrompts, setStarterPrompts] = useState([]);
  const [activeSession, setActiveSession] = useState(null);
  const [activeDetail, setActiveDetail] = useState(null);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [pendingAttachments, setPendingAttachments] = useState([]);
  const [error, setError] = useState("");
  const [mobileOpen, setMobileOpen] = useState(false);
  const messageListRef = useRef(null);
  const messagesEndRef = useRef(null);
  const shouldAutoScrollRef = useRef(true);

  function clearWorkspace() {
    setSessions([]);
    setStarterPrompts([]);
    setActiveSession(null);
    setActiveDetail(null);
    setDraft("");
    setPendingAttachments([]);
    setBusy(false);
    setUploading(false);
    setMobileOpen(false);
  }

  async function fetchJson(path, options = {}) {
    const response = await fetch(path, {
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        ...(options.headers || {}),
      },
      ...options,
    });

    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      if (response.status === 401 && !path.startsWith("/api/auth/")) {
        setAuthUser(null);
        clearWorkspace();
        setAuthChecked(true);
      }
      throw new Error(payload.detail || `Request failed with ${response.status}`);
    }

    return response.json();
  }

  async function loadBootstrap() {
    const data = await fetchJson("/api/bootstrap");
    setSessions(data.sessions || []);
    setStarterPrompts(data.starter_prompts || []);
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

  async function ensureSessionForUpload() {
    if (activeSession) {
      return activeSession;
    }

    const detail = await fetchJson("/api/sessions", {
      method: "POST",
      body: JSON.stringify({ name_hint: "uploaded_designs" }),
    });
    setActiveSession(detail.session_name);
    setActiveDetail(detail);
    await refreshSessions(detail.session_name);
    return detail.session_name;
  }

  async function ensureSessionForChat(content) {
    if (activeSession) {
      return activeSession;
    }

    const detail = await fetchJson("/api/sessions", {
      method: "POST",
      body: JSON.stringify({ name_hint: content || "new_chat" }),
    });
    setActiveSession(detail.session_name);
    setActiveDetail(detail);
    return detail.session_name;
  }

  useEffect(() => {
    async function loadAuth() {
      try {
        const payload = await fetchJson("/api/auth/me");
        setAuthUser(payload.user);
        await loadBootstrap();
      } catch (err) {
        setAuthUser(null);
      } finally {
        setAuthChecked(true);
      }
    }
    loadAuth();
  }, []);

  async function handleAuthSubmit(username, password) {
    setError("");
    try {
      const payload = await fetchJson(`/api/auth/${authMode}`, {
        method: "POST",
        body: JSON.stringify({ username, password }),
      });
      setAuthUser(payload.user);
      clearWorkspace();
      await loadBootstrap();
    } catch (err) {
      setError(err.message);
    }
  }

  async function handleLogout() {
    await fetchJson("/api/auth/logout", { method: "POST" }).catch(() => null);
    setAuthUser(null);
    clearWorkspace();
    setError("");
  }

  useEffect(() => {
    shouldAutoScrollRef.current = true;
  }, [activeSession]);

  function updateAutoScrollPreference() {
    const list = messageListRef.current;
    if (!list) {
      shouldAutoScrollRef.current = true;
      return;
    }

    const distanceFromBottom = list.scrollHeight - list.scrollTop - list.clientHeight;
    shouldAutoScrollRef.current = distanceFromBottom <= AUTO_SCROLL_THRESHOLD_PX;
  }

  function scrollMessagesToBottom(behavior = "auto") {
    if (messagesEndRef.current) {
      messagesEndRef.current.scrollIntoView({ behavior, block: "end" });
    }
  }

  useLayoutEffect(() => {
    if (shouldAutoScrollRef.current) {
      scrollMessagesToBottom();
    }
  }, [activeDetail, busy]);

  useEffect(() => {
    if (!busy || !activeSession) {
      return undefined;
    }

    let cancelled = false;
    let inFlight = false;

    async function pollActiveSession() {
      if (inFlight) {
        return;
      }

      inFlight = true;
      try {
        const detail = await fetchJson(`/api/sessions/${encodeURIComponent(activeSession)}`);
        if (!cancelled) {
          if (detail.session_name && detail.session_name !== activeSession) {
            setActiveSession(detail.session_name);
          }
          setActiveDetail((current) => mergeLiveDetail(current, detail));
        }
      } catch (err) {
        if (!cancelled) {
          console.warn("Unable to refresh live agent activity", err);
        }
      } finally {
        inFlight = false;
      }
    }

    const initialPoll = window.setTimeout(pollActiveSession, 250);
    const pollTimer = window.setInterval(pollActiveSession, 900);

    return () => {
      cancelled = true;
      window.clearTimeout(initialPoll);
      window.clearInterval(pollTimer);
    };
  }, [busy, activeSession]);

  async function handleSend(overrideText) {
    const content = (overrideText || draft).trim();
    if ((!content && !pendingAttachments.length) || busy) {
      return;
    }

    shouldAutoScrollRef.current = true;
    scrollMessagesToBottom("auto");
    setBusy(true);
    setError("");

    const optimisticId = `optimistic-${Date.now()}`;
    const optimisticMessage = {
      id: optimisticId,
      role: "user",
      kind: "message",
      label: "You",
      content,
      attachments: [...pendingAttachments],
      ts: new Date().toISOString(),
    };

    const outgoingAttachments = [...pendingAttachments];
    const previousDetail = activeDetail;
    const previousSession = activeSession;

    setDraft("");
    setPendingAttachments([]);

    try {
      const targetSession = await ensureSessionForChat(content || outgoingAttachments.join(", "));

      setActiveDetail((current) => {
        if (!current) {
          return {
            session_name: targetSession,
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

      const payload = await fetchJson("/api/chat", {
        method: "POST",
        body: JSON.stringify({
          message: content,
          session_name: targetSession,
          attachments: outgoingAttachments,
        }),
      });

      setActiveSession(payload.session.session_name);
      setActiveDetail(payload.session);
      await refreshSessions(payload.session.session_name);
    } catch (err) {
      setActiveSession(previousSession);
      setActiveDetail(previousDetail);
      setError(err.message);
      setDraft(content);
      setPendingAttachments(outgoingAttachments);
    } finally {
      setBusy(false);
    }
  }

  async function handleUpload(files) {
    if (!files.length || uploading || busy) {
      return;
    }

    setUploading(true);
    setError("");

    try {
      const sessionName = await ensureSessionForUpload();
      const formData = new FormData();
      formData.append("session_name", sessionName);
      files.forEach((file) => formData.append("files", file));

      const response = await fetch("/api/files", {
        method: "POST",
        credentials: "same-origin",
        body: formData,
      });

      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        if (response.status === 401) {
          setAuthUser(null);
          clearWorkspace();
          setAuthChecked(true);
        }
        throw new Error(payload.detail || `Upload failed with ${response.status}`);
      }

      const payload = await response.json();
      setActiveSession(payload.session.session_name);
      setActiveDetail(payload.session);
      setPendingAttachments((current) => [...current, ...payload.uploaded_files]);
      await refreshSessions(payload.session.session_name);
    } catch (err) {
      setError(err.message);
    } finally {
      setUploading(false);
    }
  }

  async function handleRemoveAttachment(filePath) {
    if (!activeSession && !pendingAttachments.includes(filePath)) {
      return;
    }

    try {
      if (activeSession) {
        const payload = await fetchJson("/api/files/delete", {
          method: "POST",
          body: JSON.stringify({
            session_name: activeSession,
            file_path: filePath,
          }),
        });
        setActiveDetail(payload.session);
      }
      setPendingAttachments((current) => current.filter((item) => item !== filePath));
      if (activeSession) {
        await refreshSessions(activeSession);
      }
    } catch (err) {
      setError(err.message);
    }
  }

  async function handleDeleteSession(session) {
    if (busy || uploading) {
      return;
    }

    const confirmed = window.confirm(`Delete "${session.title}" and all files in this session?`);
    if (!confirmed) {
      return;
    }

    try {
      setError("");
      const payload = await fetchJson(`/api/sessions/${encodeURIComponent(session.session_name)}`, {
        method: "DELETE",
      });
      setSessions(payload.sessions || []);
      if (activeSession === session.session_name) {
        setActiveSession(null);
        setActiveDetail(null);
        setDraft("");
        setPendingAttachments([]);
      }
    } catch (err) {
      setError(err.message);
    }
  }

  function handleDownloadResults() {
    if (!activeSession || !(sessionHeader.artifact_count > 0)) {
      return;
    }
    window.location.assign(`/api/sessions/${encodeURIComponent(activeSession)}/download.zip`);
  }

  function handleExportChat() {
    if (!activeSession || !hasMessages) {
      return;
    }
    window.location.assign(`/api/sessions/${encodeURIComponent(activeSession)}/export.md`);
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
    artifact_count: 0,
    messages: [],
  };

  const hasMessages = (activeDetail?.messages || []).length > 0;

  if (!authChecked) {
    return (
      <div className="auth-shell">
        <div className="auth-panel auth-loading">Loading workspace...</div>
      </div>
    );
  }

  if (!authUser) {
    return (
      <AuthScreen
        mode={authMode}
        setMode={setAuthMode}
        onSubmit={handleAuthSubmit}
        error={error}
      />
    );
  }

  return (
    <div className="app-shell">
      <div className={`app-frame ${mobileOpen ? "frame-sidebar-open" : ""}`}>
        <SessionList
          sessions={sessions}
          activeSession={activeSession}
          onSelect={loadSession}
          onDelete={handleDeleteSession}
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
              <h2>{sessionHeader.title || "New chat"}</h2>
            </div>

            <div className="topbar-meta">
              <span>{sessionHeader.run_count || 0} runs</span>
              <span>{sessionHeader.design_file_count || 0} design files</span>
              <span>{sessionHeader.artifact_count || 0} outputs</span>
              <span>{formatWhen(sessionHeader.created)}</span>
            </div>

            <button
              className="download-results-button"
              onClick={handleDownloadResults}
              disabled={!activeSession || !(sessionHeader.artifact_count > 0)}
              type="button"
            >
              Download results
            </button>

            <button
              className="export-chat-button"
              onClick={handleExportChat}
              disabled={!activeSession || !hasMessages}
              type="button"
            >
              Export chat
            </button>

            <div className="user-menu">
              <span>{authUser.username}</span>
              <button onClick={handleLogout} type="button">Logout</button>
            </div>
          </header>

          <section className="conversation">
            {error ? <div className="error-banner">{error}</div> : null}

            {!hasMessages ? (
              <EmptyState prompts={starterPrompts} onPrompt={handleSend} />
            ) : (
              <div className="message-list" ref={messageListRef} onScroll={updateAutoScrollPreference}>
                {(activeDetail?.messages || []).map((message) => (
                  <MessageItem key={message.id} message={message} />
                ))}

                {busy ? (
                  <div className="message-row message-row-assistant">
                    <div className="avatar avatar-assistant"><ChipLogo /></div>
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

          <Composer
            draft={draft}
            setDraft={setDraft}
            onSend={handleSend}
            onPickFiles={handleUpload}
            pendingAttachments={pendingAttachments}
            onRemoveAttachment={handleRemoveAttachment}
            busy={busy}
            uploading={uploading}
          />
        </main>
      </div>
    </div>
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(<App />);
