import { useEffect, useState } from "react";
import { getChatMessages, sendChatMessage, type ChatMessage } from "./api";
import { coverageBadge } from "./labels";

/** Client-only id for the transient "Vantage is thinking…" placeholder row
 * appended during an in-flight send (UI-SPEC state 7) — never persisted,
 * always removed on POST success or failure. */
const THINKING_PLACEHOLDER_ID = "__thinking__";

function makeOptimisticId(): string {
  return `optimistic-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

/**
 * Follow-up chat panel for a completed memo (CHAT-01/CHAT-03/CHAT-04, D-07).
 * Fetches persisted history on mount, then supports optimistic send with a
 * transient "thinking" placeholder while the POST is in flight.
 *
 * V5 (untrusted content): both the user's typed text and the assistant's
 * LLM-generated `content` are rendered as plain JSX text interpolation only
 * — React's raw-HTML injection escape hatch is never used.
 */
export default function FollowUpChat({
  memoId,
  token,
}: {
  memoId: string;
  token: string;
}) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [loadError, setLoadError] = useState(false);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [sendError, setSendError] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setLoaded(false);
    setLoadError(false);

    (async () => {
      try {
        const response = await getChatMessages(memoId, token);
        if (cancelled) return;
        setMessages(response.messages);
        setLoaded(true);
      } catch {
        if (cancelled) return;
        setLoadError(true);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [memoId, token]);

  async function handleSend(e: React.FormEvent) {
    e.preventDefault();
    const question = input.trim();
    if (!question || sending) return;

    const optimisticUser: ChatMessage = {
      id: makeOptimisticId(),
      role: "user",
      content: question,
      coverage_exceeded: false,
      created_at: new Date().toISOString(),
    };
    const thinkingPlaceholder: ChatMessage = {
      id: THINKING_PLACEHOLDER_ID,
      role: "assistant",
      content: "Vantage is thinking…",
      coverage_exceeded: false,
      created_at: new Date().toISOString(),
    };

    setMessages((prev) => [...prev, optimisticUser, thinkingPlaceholder]);
    setInput("");
    setSending(true);
    setSendError(false);

    try {
      const reply = await sendChatMessage(memoId, question, token);
      setMessages((prev) => [
        ...prev.filter((m) => m.id !== THINKING_PLACEHOLDER_ID),
        reply,
      ]);
      setSendError(false);
    } catch {
      setMessages((prev) =>
        prev.filter((m) => m.id !== THINKING_PLACEHOLDER_ID),
      );
      setSendError(true);
    } finally {
      setSending(false);
    }
  }

  return (
    <div className="panel section-card follow-up-chat">
      <h3 className="heading">Follow-Up Questions</h3>

      {loadError ? (
        <p className="body chat-error">Couldn't load this chat. Try again.</p>
      ) : (
        loaded && (
          <>
            {messages.length === 0 ? (
              <div className="empty-state">
                <h4 className="heading">No follow-up questions yet</h4>
                <p className="body">
                  Ask something about this memo — answers are grounded only
                  in the text above, not new research.
                </p>
              </div>
            ) : (
              <ul className="chat-message-list">
                {messages.map((message) => {
                  const badge =
                    message.role === "assistant"
                      ? coverageBadge(message.coverage_exceeded)
                      : null;
                  return (
                    <li className="chat-message" key={message.id}>
                      <span className="label">
                        {message.role === "user" ? "You" : "Vantage"}
                      </span>
                      <div className="chat-bubble body">
                        {message.content}
                      </div>
                      {badge && (
                        <span
                          className="badge"
                          style={{ backgroundColor: badge.color }}
                        >
                          {badge.text}
                        </span>
                      )}
                    </li>
                  );
                })}
              </ul>
            )}

            <form className="chat-input-row" onSubmit={handleSend}>
              <input
                className="input"
                type="text"
                placeholder="Ask a follow-up about this memo…"
                value={input}
                onChange={(e) => setInput(e.target.value)}
              />
              <button
                className="button-accent"
                type="submit"
                disabled={input.trim().length === 0 || sending}
              >
                {sending ? "Sending…" : "Send Message"}
              </button>
            </form>

            {sendError && (
              <p className="body chat-error">
                Couldn't send your message. Try again.
              </p>
            )}
          </>
        )
      )}
    </div>
  );
}
