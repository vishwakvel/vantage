import { API_BASE } from "./config";

export interface LoginResponse {
  access_token: string;
  token_type: string;
}

export interface RunAsyncResponse {
  memo_id: string;
  plan_id: string;
  status: string;
}

/**
 * A single Contradictions-panel entry (MEMO-04, D-03/D-04). Produced by the
 * Synthesis agent and carried at `MemoResponse.body.synthesis.contradictions`.
 */
export interface ContradictionItem {
  topic: string;
  agents: string[];
  description: string;
  severity: "High" | "Medium" | "Low";
}

/**
 * Shape returned by both GET memo routes (matches
 * `app/api/v1/research.py::MemoResponse`). `body` is the structured
 * per-section memo payload rendered by MemoView/ContradictionsPanel.
 */
export interface MemoResponse {
  memo_id: string;
  plan_id: string;
  status: string;
  ticker: string | null;
  body: Record<string, unknown> | null;
}

/**
 * POST /auth/login — exchanges email/password for a bearer JWT.
 * Returns the raw access_token string on success; throws on any non-2xx.
 */
export async function login(email: string, password: string): Promise<string> {
  const response = await fetch(`${API_BASE}/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  if (!response.ok) {
    throw new Error(`Login failed with status ${response.status}`);
  }
  const body = (await response.json()) as LoginResponse;
  return body.access_token;
}

/**
 * POST /research/{planId}/run — dispatches an async research run.
 * The caller is responsible for mapping a 404 to the UI-SPEC
 * "Couldn't start research" copy.
 */
export async function startRun(
  planId: string,
  token: string,
): Promise<RunAsyncResponse> {
  const response = await fetch(`${API_BASE}/research/${planId}/run`, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!response.ok) {
    throw new Error(`Start run failed with status ${response.status}`);
  }
  return (await response.json()) as RunAsyncResponse;
}

/**
 * GET /research/memo/{memoId} — fetches a memo by id.
 * Returns the parsed MemoResponse for MemoView/ContradictionsPanel to render
 * as a formatted view (Phase 7 — replaces the raw JSON dump, D-05).
 */
export async function getMemo(
  memoId: string,
  token: string,
): Promise<MemoResponse> {
  const response = await fetch(`${API_BASE}/research/memo/${memoId}`, {
    method: "GET",
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!response.ok) {
    throw new Error(`Get memo failed with status ${response.status}`);
  }
  return (await response.json()) as MemoResponse;
}
