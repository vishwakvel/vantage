// Agent-name and status-badge mapping for the live progress panel
// (06-UI-SPEC.md — Status Badge Colors table + Agent name display mapping).
//
// AGENT_ORDER uses the ACTUAL backend PascalCase agent_type values (as
// persisted by the agents and emitted by the WS route in 06-04), NOT the
// UI-SPEC's speculative FUNDAMENTAL_ANALYSIS-style enum names.

export const AGENT_ORDER: string[] = [
  "FundamentalAnalysis",
  "SentimentNLP",
  "RiskAssessment",
  "MacroSector",
  "ComparableCompanies",
  "Synthesis",
];

const AGENT_LABELS: Record<string, string> = {
  FundamentalAnalysis: "Fundamental Analysis",
  SentimentNLP: "Sentiment NLP",
  RiskAssessment: "Risk Assessment",
  MacroSector: "Macro Sector",
  ComparableCompanies: "Comparable Companies",
  Synthesis: "Synthesis",
};

/**
 * Maps a backend agent_type value to its human-readable display label.
 * Falls back to the raw value for any unrecognized agent_type.
 */
export function agentLabel(agentType: string): string {
  return AGENT_LABELS[agentType] ?? agentType;
}

export interface StatusBadge {
  color: string;
  text: string;
}

const STATUS_BADGES: Record<string, StatusBadge> = {
  Queued: { color: "#9CA3AF", text: "Queued" },
  RUNNING: { color: "#2563EB", text: "Running" },
  SUCCESS: { color: "#16A34A", text: "Success" },
  PARTIAL: { color: "#D97706", text: "Partial" },
  FAILED: { color: "#DC2626", text: "Failed" },
  COMPLETE: { color: "#16A34A", text: "Complete" },
};

/**
 * Maps a status value (backend AgentTask enum member, or the frontend-only
 * "Queued" rendering default) to its badge color + display text per the
 * UI-SPEC Status Badge Colors table.
 */
export function statusBadge(status: string): StatusBadge {
  return STATUS_BADGES[status] ?? STATUS_BADGES.Queued;
}

export const SEVERITY_BADGES: Record<string, StatusBadge> = {
  High: { color: "#DC2626", text: "High" },
  Medium: { color: "#D97706", text: "Medium" },
  Low: { color: "#16A34A", text: "Low" },
};

/**
 * Maps a contradiction severity tier (High/Medium/Low) to its badge color +
 * display text per the UI-SPEC Severity Badge Colors table (MEMO-04, D-03).
 * Falls back to the Low tier for any unrecognized severity value.
 */
export function severityBadge(severity: string): StatusBadge {
  return SEVERITY_BADGES[severity] ?? SEVERITY_BADGES.Low;
}

export const METRIC_LABELS: Record<string, string> = {
  revenue: "Revenue",
  net_income: "Net Income",
  gross_margin: "Gross Margin",
  operating_margin: "Operating Margin",
  debt_to_equity: "Debt-to-Equity",
  free_cash_flow: "Free Cash Flow",
};

/**
 * Maps a backend `metric_name` identifier to its human-readable display
 * label (METRIC-02). Covers the six D-01 core metrics. Falls back to the
 * raw identifier for any unrecognized metric_name, same convention as
 * `agentLabel`.
 */
export function metricLabel(metricName: string): string {
  return METRIC_LABELS[metricName] ?? metricName;
}

export const COVERAGE_BADGE: StatusBadge = { color: "#D97706", text: "Exceeds Coverage" };

/**
 * Maps a chat message's `coverage_exceeded` flag to its badge (or absence of
 * one) per the UI-SPEC Coverage-Exceeded Badge Color table (D-07, CHAT-04) —
 * a distinct, explicit amber "caution" signal (same hue as PARTIAL/Medium,
 * never folded into the answer's prose). Boolean-keyed rather than a
 * `Record<string,...>` map since `coverage_exceeded` is a bool, not an enum
 * (Claude's-discretion adaptation of the severityBadge/statusBadge map
 * convention to the actual data type). Returns `null` when the flag is
 * false — no badge is rendered for in-coverage answers.
 */
export function coverageBadge(coverageExceeded: boolean): StatusBadge | null {
  return coverageExceeded ? COVERAGE_BADGE : null;
}
