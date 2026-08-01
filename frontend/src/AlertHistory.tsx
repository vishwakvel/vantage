import { useState } from "react";
import { getAlertHistory, type NotificationEntry } from "./api";

/**
 * Per-ticker alert history section (WATCH-07), nested inside each
 * `.watchlist-entry` alongside `AlertRuleForm`.
 *
 * The fetch is lazy — it fires only on expand (`handleToggle`), never on
 * mount. There is deliberately no mount-time fetch effect here: an eager
 * fetch on mount would issue one request per watchlisted ticker on every
 * page load, which is exactly what this lazy-on-expand decision avoids
 * (D-08).
 *
 * History intentionally includes triggers from alert rules the user has
 * since disabled (D-07): disabling a rule is a soft toggle (WATCH-08), and
 * a trigger that really happened is not retroactively invalidated by a
 * later disable.
 *
 * History rows carry no read/unread state at all — WATCH-07 is a
 * historical record, while read/unread semantics belong solely to the
 * notification bell. No row here ever reads `e.read` or renders an unread
 * accent.
 *
 * V5 (untrusted-adjacent output): `message` strings are rendered as plain
 * JSX text interpolation only; React's raw-HTML injection escape hatch is
 * never used.
 */
export default function AlertHistory({
  entryId,
  ticker,
  token,
}: {
  entryId: string;
  ticker: string;
  token: string;
}) {
  const [expanded, setExpanded] = useState(false);
  const [events, setEvents] = useState<NotificationEntry[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [loadError, setLoadError] = useState(false);

  async function handleToggle() {
    if (expanded) {
      setExpanded(false);
      setEvents([]);
      setLoaded(false);
      setLoadError(false);
      return;
    }

    setExpanded(true);
    setLoaded(false);
    setLoadError(false);
    try {
      const response = await getAlertHistory(entryId, token);
      setEvents(response.events);
      setLoaded(true);
    } catch {
      setLoadError(true);
    }
  }

  return (
    <div className="alert-history">
      <button
        type="button"
        className="button-secondary"
        onClick={() => {
          void handleToggle();
        }}
        aria-expanded={expanded}
      >
        {expanded ? "Hide Alert History" : "View Alert History"}
      </button>

      {expanded && (
        <>
          {loadError && (
            <div className="error-banner">
              Couldn't load alert history. Try again.
            </div>
          )}
          {!loadError && loaded && events.length === 0 && (
            <div className="empty-state">
              <p className="body">No alerts have fired for {ticker} yet.</p>
            </div>
          )}
          {!loadError && loaded && events.length > 0 && (
            <ul className="alert-history-list">
              {events.map((e) => (
                <li key={e.id} className="alert-history-item">
                  <span className="notification-message">{e.message}</span>
                  <span className="notification-time">
                    {e.triggered_at.split("T")[0]}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </div>
  );
}
