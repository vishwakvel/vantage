import { useEffect, useRef, useState } from "react";
import { getMemo, login, startRun } from "./api";
import { agentLabel, AGENT_ORDER, statusBadge } from "./labels";
import { connectProgress, type ProgressConnection } from "./ws";

type StatusMap = Record<string, string>;

export default function App() {
  // Auth state — token lives only in React state, no browser persistence
  // (UI-SPEC / threat model T-06-09-TOKEN).
  const [token, setToken] = useState<string | null>(null);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");

  // Run state
  const [planId, setPlanId] = useState("");
  const [memoId, setMemoId] = useState<string | null>(null);
  const [statuses, setStatuses] = useState<StatusMap>({});
  const [terminalStatus, setTerminalStatus] = useState<string | null>(null);
  const [wsDropped, setWsDropped] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [memoJson, setMemoJson] = useState<string | null>(null);

  const connectionRef = useRef<ProgressConnection | null>(null);

  useEffect(() => {
    // Tear down any open socket on unmount.
    return () => {
      connectionRef.current?.close();
    };
  }, []);

  async function handleLogin(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      const accessToken = await login(email, password);
      setToken(accessToken);
    } catch {
      setError("Couldn't log in — check your email and password.");
    }
  }

  async function handleStartRun(e: React.FormEvent) {
    e.preventDefault();
    if (!token) return;
    setError(null);

    try {
      const response = await startRun(planId, token);

      // Tear down any prior socket before starting a new run.
      connectionRef.current?.close();

      setMemoId(response.memo_id);
      setTerminalStatus(null);
      setWsDropped(false);
      setMemoJson(null);

      const seeded: StatusMap = {};
      for (const agentType of AGENT_ORDER) {
        seeded[agentType] = "Queued";
      }
      setStatuses(seeded);

      let sawTerminal = false;

      connectionRef.current = connectProgress(response.memo_id, token, {
        onSnapshot: (agents) => {
          setStatuses((prev) => {
            const next = { ...prev };
            for (const entry of agents) {
              next[entry.agent_type] = entry.status;
            }
            return next;
          });
        },
        onAgent: (agentType, status) => {
          setStatuses((prev) => ({ ...prev, [agentType]: status }));
        },
        onTerminal: (memoStatus) => {
          sawTerminal = true;
          setTerminalStatus(memoStatus);
        },
        onClose: (wasTerminal) => {
          if (!wasTerminal && !sawTerminal) {
            setWsDropped(true);
          }
        },
      });
    } catch {
      setError("Couldn't start research — check the plan ID and try again.");
    }
  }

  async function handleViewMemo() {
    if (!memoId || !token) return;
    try {
      const memo = await getMemo(memoId, token);
      setMemoJson(JSON.stringify(memo, null, 2));
    } catch {
      setError("Couldn't load the memo. Try again.");
    }
  }

  const hasRun = memoId !== null;

  return (
    <div className="page">
      <h1 className="heading">Vantage Research</h1>

      {!token && (
        <form className="section" onSubmit={handleLogin}>
          <div className="field">
            <label className="label" htmlFor="email">
              Email
            </label>
            <input
              id="email"
              className="input"
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              required
            />
          </div>
          <div className="field">
            <label className="label" htmlFor="password">
              Password
            </label>
            <input
              id="password"
              className="input"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
            />
          </div>
          <button className="button-secondary" type="submit">
            Log In
          </button>
        </form>
      )}

      {token && (
        <form className="section" onSubmit={handleStartRun}>
          <div className="field">
            <label className="label" htmlFor="planId">
              Plan ID
            </label>
            <input
              id="planId"
              className="input"
              type="text"
              value={planId}
              onChange={(e) => setPlanId(e.target.value)}
              required
            />
          </div>
          <button className="button-accent" type="submit">
            Start Research
          </button>
        </form>
      )}

      {error && <div className="error-banner">{error}</div>}

      <div className="section panel">
        <h2 className="heading">Agent Progress</h2>

        {!hasRun && (
          <div className="empty-state">
            <h3 className="heading">No research running</h3>
            <p className="body">
              Enter a plan ID above and select Start Research to see live
              agent progress.
            </p>
          </div>
        )}

        {hasRun && (
          <ul className="agent-list">
            {AGENT_ORDER.map((agentType) => {
              const status = statuses[agentType] ?? "Queued";
              const badge = statusBadge(status);
              return (
                <li className="agent-row" key={agentType}>
                  <span className="label agent-name">
                    {agentLabel(agentType)}
                  </span>
                  <span
                    className="badge"
                    style={{ backgroundColor: badge.color }}
                  >
                    {badge.text}
                  </span>
                </li>
              );
            })}
          </ul>
        )}

        {wsDropped && (
          <div className="error-banner">
            Connection lost. Refresh the page to check the latest status.
          </div>
        )}

        {terminalStatus && (
          <div className="terminal-actions">
            <a className="link-accent" href="#" onClick={(e) => {
              e.preventDefault();
              void handleViewMemo();
            }}>
              View Memo
            </a>
          </div>
        )}

        {memoJson && <pre className="memo-json">{memoJson}</pre>}
      </div>
    </div>
  );
}
