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
};

/**
 * Maps a status value (backend AgentTask enum member, or the frontend-only
 * "Queued" rendering default) to its badge color + display text per the
 * UI-SPEC Status Badge Colors table.
 */
export function statusBadge(status: string): StatusBadge {
  return STATUS_BADGES[status] ?? STATUS_BADGES.Queued;
}
