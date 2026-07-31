import { useEffect, useState } from "react";
import {
  addToWatchlist,
  getWatchlist,
  removeFromWatchlist,
  toggleAlertRule,
  type AlertRuleResponse,
  type PriceMoveConfig,
  type ScheduledConfig,
  type WatchlistEntryResponse,
} from "./api";
import { statusBadge } from "./labels";
import AlertRuleForm, {
  CADENCE_LABELS,
  DIRECTION_LABELS,
  RULE_TYPE_LABELS,
} from "./AlertRuleForm";
import AlertHistory from "./AlertHistory";

/**
 * Type guards narrowing on `rule.rule_type` so `describeRule` can read a
 * config field without a type assertion. `AlertRuleResponse.rule_type` and
 * `.config` are separate sibling properties (not a discriminated union at
 * the `api.ts` interface level, which this plan leaves unmodified), so
 * TypeScript cannot correlate them from a bare `rule.rule_type === "..."`
 * check alone — a user-defined type predicate is the assertion-free way to
 * teach the compiler the correlation.
 */
function isPriceMoveRule(
  rule: AlertRuleResponse,
): rule is AlertRuleResponse & { config: PriceMoveConfig } {
  return rule.rule_type === "PRICE_MOVE";
}

function isScheduledRule(
  rule: AlertRuleResponse,
): rule is AlertRuleResponse & { config: ScheduledConfig } {
  return rule.rule_type === "SCHEDULED";
}

/**
 * Turns a single alert rule into a one-line human-readable summary, reusing
 * the label maps `AlertRuleForm` exports so the two components never drift.
 * Narrows on `rule.rule_type` before reading a config field — no type
 * assertion is used to read `threshold_pct` or `cadence`.
 */
function describeRule(rule: AlertRuleResponse): string {
  if (isPriceMoveRule(rule)) {
    return `${rule.config.threshold_pct}% — ${DIRECTION_LABELS[rule.config.direction]}`;
  }
  if (isScheduledRule(rule)) {
    return CADENCE_LABELS[rule.config.cadence];
  }
  return RULE_TYPE_LABELS.NEW_FILING;
}

/**
 * The watchlist section: add/remove tickers (WATCH-01), each entry's D-13
 * latest research status, nested alert rules with per-rule enable/disable
 * (WATCH-08), and a type-specific creation form for NEW_FILING/PRICE_MOVE/
 * SCHEDULED rules (WATCH-03/04/05).
 *
 * D-13: "latest status" renders the real latest `ResearchMemo` status/date
 * for the ticker via `statusBadge` (reused unchanged from `labels.ts`), or
 * an explicit "No research yet" when none exists — never a placeholder.
 * D-03: removing a ticker cascade-deletes its alert rules server-side; the
 * Remove control's title text states this consequence before the click.
 * D-07/D-08: there is no rule-delete control and no entry-level
 * enable/disable control anywhere in this component — disable is the only
 * retirement path, and it is always per rule.
 * Each entry also renders its own expandable per-ticker alert history
 * (WATCH-07) via the sibling `AlertHistory` component.
 *
 * V5 (untrusted-adjacent output): ticker strings and rule summaries are
 * server-derived values echoed back from the database; they are rendered
 * as plain JSX text interpolation only, React's raw-HTML injection escape
 * hatch is never used (T-10-04-XSS).
 */
export default function Watchlist({ token }: { token: string }) {
  const [entries, setEntries] = useState<WatchlistEntryResponse[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [loadError, setLoadError] = useState(false);
  const [tickerInput, setTickerInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoaded(false);
    setLoadError(false);

    (async () => {
      try {
        const response = await getWatchlist(token);
        if (cancelled) return;
        setEntries(response.entries);
        setLoaded(true);
      } catch {
        if (cancelled) return;
        setLoadError(true);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [token]);

  async function refresh() {
    try {
      const response = await getWatchlist(token);
      setEntries(response.entries);
      setLoaded(true);
      setLoadError(false);
    } catch {
      setLoadError(true);
    }
  }

  async function handleAddTicker(e: React.FormEvent) {
    e.preventDefault();
    const trimmed = tickerInput.trim();
    if (!trimmed || busy) return;

    setBusy(true);
    setActionError(null);
    try {
      await addToWatchlist(trimmed.toUpperCase(), token);
      setTickerInput("");
      await refresh();
    } catch {
      setActionError("Couldn't add that ticker. Use 1-10 letters or digits.");
    } finally {
      setBusy(false);
    }
  }

  async function handleRemove(entryId: string) {
    if (busy) return;
    setBusy(true);
    setActionError(null);
    try {
      await removeFromWatchlist(entryId, token);
      await refresh();
    } catch {
      setActionError("Couldn't remove that ticker. Try again.");
    } finally {
      setBusy(false);
    }
  }

  async function handleToggle(rule: AlertRuleResponse) {
    setActionError(null);
    try {
      const updated = await toggleAlertRule(rule.id, !rule.enabled, token);
      setEntries((prev) =>
        prev.map((entry) => ({
          ...entry,
          alert_rules: entry.alert_rules.map((r) =>
            r.id === updated.id ? updated : r,
          ),
        })),
      );
    } catch {
      setActionError("Couldn't update that alert rule. Try again.");
    }
  }

  return (
    <div className="watchlist section panel">
      <h2 className="heading">Watchlist</h2>

      <form className="watchlist-add-row" onSubmit={handleAddTicker}>
        <div className="field">
          <label className="label" htmlFor="watchlist-ticker-input">
            Ticker
          </label>
          <input
            id="watchlist-ticker-input"
            className="input"
            type="text"
            maxLength={10}
            value={tickerInput}
            onChange={(e) => setTickerInput(e.target.value)}
          />
        </div>
        <button className="button-secondary" type="submit" disabled={busy}>
          Add
        </button>
      </form>

      {loadError && (
        <div className="error-banner">Couldn't load your watchlist.</div>
      )}
      {actionError && <div className="error-banner">{actionError}</div>}

      {loaded && entries.length === 0 ? (
        <div className="empty-state">
          <p className="body">
            No tickers are being watched yet. Add one above to start
            tracking it.
          </p>
        </div>
      ) : (
        <ul className="watchlist-list">
          {entries.map((entry) => {
            const badge = entry.latest_memo_status
              ? statusBadge(entry.latest_memo_status)
              : null;
            return (
              <li className="watchlist-entry" key={entry.id}>
                <div className="watchlist-entry-header">
                  <span className="watchlist-ticker">{entry.ticker}</span>
                  {badge ? (
                    <>
                      <span
                        className="badge"
                        style={{ backgroundColor: badge.color }}
                      >
                        {badge.text}
                      </span>
                      <span className="watchlist-meta">
                        {entry.latest_memo_date?.split("T")[0]}
                      </span>
                    </>
                  ) : (
                    <span className="watchlist-meta">No research yet</span>
                  )}
                  <button
                    className="button-secondary"
                    type="button"
                    aria-label={`Remove ${entry.ticker} from watchlist`}
                    title={`Removing ${entry.ticker} also removes its alert rules`}
                    onClick={() => handleRemove(entry.id)}
                    disabled={busy}
                  >
                    Remove
                  </button>
                </div>

                {entry.alert_rules.length === 0 ? (
                  <p className="body">
                    No alert rules are set for {entry.ticker} yet.
                  </p>
                ) : (
                  <ul className="alert-rule-list">
                    {entry.alert_rules.map((rule) => (
                      <li className="alert-rule-item" key={rule.id}>
                        <span className="alert-rule-summary">
                          {describeRule(rule)}
                        </span>
                        <label className="alert-rule-toggle">
                          <input
                            type="checkbox"
                            checked={rule.enabled}
                            aria-label={`Enable or disable rule: ${describeRule(rule)}`}
                            onChange={() => handleToggle(rule)}
                          />
                          Enabled
                        </label>
                      </li>
                    ))}
                  </ul>
                )}

                <AlertRuleForm
                  entryId={entry.id}
                  ticker={entry.ticker}
                  token={token}
                  onCreated={() => {
                    void refresh();
                  }}
                />

                <AlertHistory
                  entryId={entry.id}
                  ticker={entry.ticker}
                  token={token}
                />
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
