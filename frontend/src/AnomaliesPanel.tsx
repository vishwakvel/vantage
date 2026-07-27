import type { AnomalyItem } from "./api";
import { metricLabel, severityBadge } from "./labels";

/**
 * Renders the isolation-forest anomaly sub-list nested inside the
 * Fundamental Analysis section card (METRIC-02, D-09).
 *
 * D-05: this sub-list is NEVER omitted. It distinguishes two independent
 * silences — "detection ran and found nothing" (empty-state) from
 * "detection was skipped" (`note`, e.g. insufficient history or metrics
 * unavailable) — and the two can co-occur since the backend's
 * `metrics_note` is set per-metric, not all-or-nothing.
 *
 * D-10 / V5 (untrusted-adjacent output): `description`, `period`, and
 * `metric_name` are code-generated from stored numeric values, never LLM
 * output, but are still rendered as plain JSX text interpolation only —
 * no raw-HTML escape hatch, same rule `ContradictionsPanel` documents.
 */
export default function AnomaliesPanel({
  anomalies,
  note,
}: {
  anomalies: AnomalyItem[];
  note?: string | null;
}) {
  return (
    <div className="anomalies-panel">
      <h4 className="heading">Anomalies</h4>

      {note && (
        <p className="label anomaly-note">{note}</p>
      )}

      {anomalies.length === 0 ? (
        <div className="empty-state">
          <p className="body">
            No anomalies detected in this company&apos;s recent quarters.
          </p>
        </div>
      ) : (
        <ul className="anomaly-list">
          {anomalies.map((item, index) => {
            const badge = severityBadge(item.severity);
            return (
              <li
                className="anomaly-item"
                key={`${item.metric_name}-${item.period}-${index}`}
              >
                <div className="anomaly-item-header">
                  <span className="label">{metricLabel(item.metric_name)}</span>
                  <span
                    className="badge"
                    style={{ backgroundColor: badge.color }}
                  >
                    {badge.text}
                  </span>
                </div>
                <span className="label">{item.period}</span>
                <p className="body">{item.description}</p>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
