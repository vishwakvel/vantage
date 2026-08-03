import type { AnomalyItem, ContradictionItem, MemoResponse } from "./api";
import { AGENT_ORDER, agentLabel, statusBadge } from "./labels";
import ContradictionsPanel from "./ContradictionsPanel";
import AnomaliesPanel from "./AnomaliesPanel";
import FollowUpChat from "./FollowUpChat";

/**
 * Shape of a specialist agent's memo-body section — either its SUCCESS
 * output (`narrative` a string, optional `citations`) or the backend's
 * EXEC-04 FAILED marker (`narrative: null`, plus `status`/`reason`).
 * Mirrors `app/workers/tasks.py::_SECTION_STATE_FIELDS` assembly.
 */
interface SpecialistSection {
  narrative: string | null;
  citations?: unknown[];
  reason?: string | null;
  anomalies?: AnomalyItem[];
  metrics_note?: string | null;
}

/**
 * Shape of the Synthesis memo-body section — either its SUCCESS output
 * (`take` a string, plus `contradictions`) or the same FAILED marker shape
 * as a specialist section (`narrative: null`, `reason`). Mirrors
 * `app/agents/synthesis.py::synthesis_node` / `_fallback_output`.
 */
interface SynthesisSection {
  take?: string | null;
  narrative?: string | null;
  contradictions?: ContradictionItem[];
  reason?: string | null;
}

/**
 * Shape of the memo's aggregated generation cost (MEMO-06, D-04). Mirrors
 * `app/workers/tasks.py`'s `body[SECTION_COST]` assembly — a token count
 * summed across the plan's `AgentOutput` rows plus an external API call
 * count read from the plan-scoped Redis counter. No monetary figure by
 * design (D-04): Groq runs here on the free tier, so any derived cost
 * figure would be fiction.
 */
interface MemoCost {
  tokens: number;
  api_calls: number;
}

interface MemoBody {
  fundamentals?: SpecialistSection;
  sentiment?: SpecialistSection;
  risks?: SpecialistSection;
  macro?: SpecialistSection;
  comparables?: SpecialistSection;
  synthesis?: SynthesisSection;
  cost?: MemoCost;
}

type SpecialistSectionKey =
  | "fundamentals"
  | "sentiment"
  | "risks"
  | "macro"
  | "comparables";

/** Maps each specialist AGENT_ORDER entry to its memo body section key
 * (mirrors `app/workers/tasks.py::_AGENT_TYPE_BY_SECTION`, inverted). */
const SECTION_BY_AGENT: Record<string, SpecialistSectionKey> = {
  FundamentalAnalysis: "fundamentals",
  SentimentNLP: "sentiment",
  RiskAssessment: "risks",
  MacroSector: "macro",
  ComparableCompanies: "comparables",
};

const DEFAULT_UNAVAILABLE_REASON =
  "This section could not be completed for this run.";

function sourcesLine(citations: unknown[] | undefined): string | null {
  if (!citations || citations.length === 0) return null;
  return citations.length === 1 ? "1 source" : `${citations.length} sources`;
}

/**
 * Formatted memo view (MEMO-04/MEMO-05, D-05/D-06) — replaces the raw
 * `<pre>{memoJson}</pre>` dump. Renders a header with an at-a-glance status
 * badge and cost line, a synthesis take card, the Contradictions panel, then
 * one card per specialist section in AGENT_ORDER order.
 */
export default function MemoView({
  memo,
  token,
}: {
  memo: MemoResponse;
  token: string;
}) {
  const body = (memo.body as MemoBody | null) ?? null;
  const badge = statusBadge(memo.status);
  const synthesis = body?.synthesis;
  // Defensive `?? []` per T-07-UNDEF, even though the backend guarantees
  // this key is always present (app/workers/tasks.py `setdefault`).
  const contradictions = synthesis?.contradictions ?? [];
  const hasTake = typeof synthesis?.take === "string";
  // Defensive optional read, same T-07-UNDEF register as `contradictions`
  // above: `tasks.py` assigns `body[SECTION_COST]` unconditionally on every
  // body assembled from this phase forward (D-06), but memos persisted
  // before this phase carry no such key, so this read is load-bearing
  // rather than merely defensive.
  const cost = body?.cost ?? null;

  return (
    <div className="memo-view">
      <div className="memo-header">
        <h2 className="heading">{memo.ticker ?? "Memo"}</h2>
        <span className="badge" style={{ backgroundColor: badge.color }}>
          {badge.text}
        </span>
        {cost && (
          <span className="label">
            {cost.tokens} tokens · {cost.api_calls} external API calls
          </span>
        )}
      </div>

      <div className="panel section-card">
        <h3 className="heading">Overall Take</h3>
        {hasTake ? (
          <p className="body">{synthesis?.take}</p>
        ) : (
          <>
            <span className="badge badge-unavailable">Unavailable</span>
            <p className="body">
              {synthesis?.reason ?? DEFAULT_UNAVAILABLE_REASON}
            </p>
          </>
        )}
      </div>

      <ContradictionsPanel contradictions={contradictions} />

      {AGENT_ORDER.filter((agentType) => agentType !== "Synthesis").map(
        (agentType) => {
          const sectionKey = SECTION_BY_AGENT[agentType];
          const section = sectionKey ? body?.[sectionKey] : undefined;
          const narrative = section?.narrative ?? null;
          const sources = sourcesLine(section?.citations);
          return (
            <div className="panel section-card" key={agentType}>
              <h3 className="heading">{agentLabel(agentType)}</h3>
              {typeof narrative === "string" ? (
                <>
                  <p className="body">{narrative}</p>
                  {sources && <p className="label">{sources}</p>}
                </>
              ) : (
                <>
                  <span className="badge badge-unavailable">
                    Unavailable
                  </span>
                  <p className="body">
                    {section?.reason ?? DEFAULT_UNAVAILABLE_REASON}
                  </p>
                </>
              )}
              {sectionKey === "fundamentals" && (
                <AnomaliesPanel
                  anomalies={section?.anomalies ?? []}
                  note={section?.metrics_note}
                />
              )}
            </div>
          );
        },
      )}

      <FollowUpChat memoId={memo.memo_id} token={token} />
    </div>
  );
}
