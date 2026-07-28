import { useState } from "react";
import {
  createAlertRule,
  type AlertRuleConfig,
  type AlertRuleResponse,
  type AlertRuleType,
  type PriceMoveDirection,
  type ScheduledCadence,
} from "./api";

/**
 * Rule-type-specific alert rule creation form (WATCH-03/04/05).
 *
 * D-15: rule creation must feel like filling out a real form for the
 * specific rule type, never a generic JSON textarea — the product's
 * audience is individual investors, not developers. The rendered field set
 * branches on the selected `ruleType`: a percentage input + direction
 * selector for PRICE_MOVE (D-09), a cadence dropdown for SCHEDULED (D-11),
 * and no fields at all for NEW_FILING (D-12, no filing-type filter this
 * phase).
 *
 * V5 (untrusted-adjacent output): all values — labels, ticker, error copy —
 * are rendered as plain JSX text interpolation only; React's raw-HTML
 * injection escape hatch is never used, matching the convention already
 * documented in `AnomaliesPanel`/`ContradictionsPanel`.
 */

export const RULE_TYPE_LABELS: Record<AlertRuleType, string> = {
  NEW_FILING: "New EDGAR filing",
  PRICE_MOVE: "Price move",
  SCHEDULED: "Scheduled re-research",
};

export const DIRECTION_LABELS: Record<PriceMoveDirection, string> = {
  up: "Moves up",
  down: "Moves down",
  either: "Moves either way",
};

export const CADENCE_LABELS: Record<ScheduledCadence, string> = {
  daily: "Daily",
  weekly: "Weekly",
  monthly: "Monthly",
};

const RULE_TYPES: AlertRuleType[] = ["NEW_FILING", "PRICE_MOVE", "SCHEDULED"];
const DIRECTIONS: PriceMoveDirection[] = ["up", "down", "either"];
const CADENCES: ScheduledCadence[] = ["daily", "weekly", "monthly"];

export default function AlertRuleForm({
  entryId,
  ticker,
  token,
  onCreated,
}: {
  entryId: string;
  ticker: string;
  token: string;
  onCreated: (rule: AlertRuleResponse) => void;
}) {
  const [ruleType, setRuleType] = useState<AlertRuleType>("NEW_FILING");
  const [thresholdPct, setThresholdPct] = useState("5");
  const [direction, setDirection] = useState<PriceMoveDirection>("either");
  const [cadence, setCadence] = useState<ScheduledCadence>("weekly");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);

    let config: AlertRuleConfig;
    if (ruleType === "NEW_FILING") {
      config = {};
    } else if (ruleType === "PRICE_MOVE") {
      const parsed = Number(thresholdPct);
      if (!Number.isFinite(parsed) || parsed <= 0 || parsed > 100) {
        setError(
          "Couldn't create that alert rule. Check the values and try again.",
        );
        return;
      }
      config = { threshold_pct: parsed, direction };
    } else {
      config = { cadence };
    }

    setSubmitting(true);
    try {
      const rule = await createAlertRule(entryId, ruleType, config, token);
      onCreated(rule);
      setRuleType("NEW_FILING");
      setThresholdPct("5");
      setDirection("either");
      setCadence("weekly");
    } catch {
      setError(
        "Couldn't create that alert rule. Check the values and try again.",
      );
    } finally {
      setSubmitting(false);
    }
  }

  const ruleTypeId = `alert-rule-type-${entryId}`;
  const thresholdId = `alert-rule-threshold-${entryId}`;
  const directionId = `alert-rule-direction-${entryId}`;
  const cadenceId = `alert-rule-cadence-${entryId}`;

  return (
    <form className="alert-rule-form" onSubmit={handleSubmit}>
      <div className="field">
        <label className="label" htmlFor={ruleTypeId}>
          Rule type
        </label>
        <select
          id={ruleTypeId}
          className="input"
          value={ruleType}
          onChange={(e) => setRuleType(e.target.value as AlertRuleType)}
        >
          {RULE_TYPES.map((type) => (
            <option key={type} value={type}>
              {RULE_TYPE_LABELS[type]}
            </option>
          ))}
        </select>
      </div>

      <div className="alert-rule-form-fields">
        {ruleType === "PRICE_MOVE" && (
          <>
            <div className="field">
              <label className="label" htmlFor={thresholdId}>
                Percentage threshold
              </label>
              <input
                id={thresholdId}
                className="input"
                type="number"
                min="0.1"
                max="100"
                step="0.1"
                value={thresholdPct}
                onChange={(e) => setThresholdPct(e.target.value)}
              />
            </div>
            <div className="field">
              <label className="label" htmlFor={directionId}>
                Direction
              </label>
              <select
                id={directionId}
                className="input"
                value={direction}
                onChange={(e) =>
                  setDirection(e.target.value as PriceMoveDirection)
                }
              >
                {DIRECTIONS.map((d) => (
                  <option key={d} value={d}>
                    {DIRECTION_LABELS[d]}
                  </option>
                ))}
              </select>
            </div>
          </>
        )}

        {ruleType === "SCHEDULED" && (
          <div className="field">
            <label className="label" htmlFor={cadenceId}>
              Cadence
            </label>
            <select
              id={cadenceId}
              className="input"
              value={cadence}
              onChange={(e) => setCadence(e.target.value as ScheduledCadence)}
            >
              {CADENCES.map((c) => (
                <option key={c} value={c}>
                  {CADENCE_LABELS[c]}
                </option>
              ))}
            </select>
          </div>
        )}

        {ruleType === "NEW_FILING" && (
          <p className="body">
            This rule fires whenever {ticker} has a new EDGAR filing.
          </p>
        )}
      </div>

      <button className="button-accent" type="submit" disabled={submitting}>
        Add alert rule
      </button>

      {error && <div className="error-banner">{error}</div>}
    </form>
  );
}
