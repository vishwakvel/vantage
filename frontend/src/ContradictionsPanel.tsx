import type { ContradictionItem } from "./api";
import { agentLabel, severityBadge } from "./labels";

/**
 * Renders the Synthesis-owned Contradictions list (MEMO-04, D-03/D-04).
 *
 * D-07: the panel is NEVER omitted, even when there are zero items — an
 * empty array (genuinely zero contradictions, or the backend's own D-02
 * degrade-on-parse-failure path, which the frontend cannot distinguish)
 * renders an explicit "no contradictions found" state instead of a blank
 * or missing section.
 *
 * V5 (untrusted LLM output): topic/agents/description are rendered as
 * plain JSX text interpolation only — raw HTML injection is forbidden.
 */
export default function ContradictionsPanel({
  contradictions,
}: {
  contradictions: ContradictionItem[];
}) {
  return (
    <div className="panel section-card">
      <h3 className="heading">Contradictions</h3>

      {contradictions.length === 0 ? (
        <div className="empty-state">
          <p className="body">No contradictions found — all agents agree.</p>
        </div>
      ) : (
        <ul className="contradiction-list">
          {contradictions.map((item, index) => {
            const badge = severityBadge(item.severity);
            return (
              <li className="contradiction-item" key={`${item.topic}-${index}`}>
                <div className="contradiction-item-header">
                  <span className="label">{item.topic}</span>
                  <span
                    className="badge"
                    style={{ backgroundColor: badge.color }}
                  >
                    {badge.text}
                  </span>
                </div>
                <div className="agent-tag-list">
                  {item.agents.map((agent) => (
                    <span className="agent-tag" key={agent}>
                      {agentLabel(agent)}
                    </span>
                  ))}
                </div>
                <p className="body">{item.description}</p>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
