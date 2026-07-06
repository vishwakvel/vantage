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
 * Returned as unknown; this phase renders it raw (no memo-formatting polish).
 */
export async function getMemo(memoId: string, token: string): Promise<unknown> {
  const response = await fetch(`${API_BASE}/research/memo/${memoId}`, {
    method: "GET",
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!response.ok) {
    throw new Error(`Get memo failed with status ${response.status}`);
  }
  return response.json();
}
