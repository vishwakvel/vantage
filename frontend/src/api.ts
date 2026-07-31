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
 * A single detected-anomaly entry (METRIC-02). Produced by
 * `app/services/anomaly_detection.py::detect_anomalies` and carried at
 * `MemoResponse.body.fundamentals.anomalies`.
 */
export interface AnomalyItem {
  metric_name: string;
  period: string;
  value: number;
  severity: "High" | "Medium" | "Low";
  description: string;
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
 * A single follow-up chat row (CHAT-01/03). Mirrors the backend
 * `app/api/v1/research.py::ChatMessageResponse` shape exactly.
 */
export interface ChatMessage {
  id: string;
  role: string;
  content: string;
  coverage_exceeded: boolean;
  created_at: string;
}

/**
 * Shape returned by `GET /research/memo/{memoId}/chat` (matches
 * `app/api/v1/research.py::ChatMessagesResponse`).
 */
export interface ChatMessagesResponse {
  messages: ChatMessage[];
}

/**
 * The three supported alert-rule kinds (D-05; mirrors
 * `app/db/models.py::AlertRuleType`).
 */
export type AlertRuleType = "NEW_FILING" | "PRICE_MOVE" | "SCHEDULED";

/**
 * Direction a PRICE_MOVE rule watches for (D-09).
 */
export type PriceMoveDirection = "up" | "down" | "either";

/**
 * Fixed recurrence presets for a SCHEDULED rule — never a raw cron string
 * (D-11).
 */
export type ScheduledCadence = "daily" | "weekly" | "monthly";

/**
 * A NEW_FILING rule's config is always an empty object (D-12) — triggers on
 * any new EDGAR filing for the ticker, no filing-type filter in this phase.
 */
export type NewFilingConfig = Record<string, never>;

/**
 * A PRICE_MOVE rule's config (D-09). Note D-10: no baseline price is stored
 * here — the reference price is runtime state Phase 11's evaluator owns.
 */
export interface PriceMoveConfig {
  threshold_pct: number;
  direction: PriceMoveDirection;
}

/**
 * A SCHEDULED rule's config (D-11).
 */
export interface ScheduledConfig {
  cadence: ScheduledCadence;
}

/**
 * Discriminable union of every rule type's config shape, keyed by
 * `AlertRuleType` at the call site (D-09/D-11/D-12) so a component cannot
 * construct an invalid payload.
 */
export type AlertRuleConfig = NewFilingConfig | PriceMoveConfig | ScheduledConfig;

/**
 * A single alert rule (matches `app/api/v1/watchlist.py::AlertRuleResponse`,
 * plan 10-05).
 */
export interface AlertRuleResponse {
  id: string;
  rule_type: AlertRuleType;
  config: AlertRuleConfig;
  enabled: boolean;
  created_at: string;
}

/**
 * A single watchlisted ticker with its nested alert rules and latest
 * research status (matches
 * `app/api/v1/watchlist.py::WatchlistEntryResponse`, plan 10-05).
 * `latest_memo_status`/`latest_memo_date` are D-13: the status and creation
 * date of this user's most recent research memo for the ticker, both `null`
 * when no research exists yet.
 */
export interface WatchlistEntryResponse {
  id: string;
  ticker: string;
  created_at: string;
  latest_memo_status: string | null;
  latest_memo_date: string | null;
  alert_rules: AlertRuleResponse[];
}

/**
 * Shape returned by `GET /watchlist` (matches
 * `app/api/v1/watchlist.py::WatchlistResponse`, plan 10-05) — the
 * single-round-trip nested shape, entries carrying their own rules and
 * memo status.
 */
export interface WatchlistResponse {
  entries: WatchlistEntryResponse[];
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

/**
 * GET /research/memo/{memoId}/chat — fetches the persisted follow-up chat
 * history for a memo (CHAT-01), grounded on memo text (no RAG re-query).
 */
export async function getChatMessages(
  memoId: string,
  token: string,
): Promise<ChatMessagesResponse> {
  const response = await fetch(`${API_BASE}/research/memo/${memoId}/chat`, {
    method: "GET",
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!response.ok) {
    throw new Error(`Get chat messages failed with status ${response.status}`);
  }
  return (await response.json()) as ChatMessagesResponse;
}

/**
 * POST /research/memo/{memoId}/chat — submits a follow-up question and
 * returns the persisted assistant reply row (CHAT-03), including its
 * `coverage_exceeded` flag (CHAT-04, D-07).
 */
export async function sendChatMessage(
  memoId: string,
  question: string,
  token: string,
): Promise<ChatMessage> {
  const response = await fetch(`${API_BASE}/research/memo/${memoId}/chat`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify({ question }),
  });
  if (!response.ok) {
    throw new Error(`Send chat message failed with status ${response.status}`);
  }
  return (await response.json()) as ChatMessage;
}

/**
 * GET /watchlist — fetches every watchlisted ticker for the current user
 * (WATCH-02), each carrying its nested alert rules and latest-memo status
 * in one round trip.
 */
export async function getWatchlist(token: string): Promise<WatchlistResponse> {
  const response = await fetch(`${API_BASE}/watchlist`, {
    method: "GET",
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!response.ok) {
    throw new Error(`Get watchlist failed with status ${response.status}`);
  }
  return (await response.json()) as WatchlistResponse;
}

/**
 * POST /watchlist — adds a ticker to the current user's watchlist
 * (WATCH-01). The backend normalises the ticker to uppercase and, per D-04,
 * re-adding a ticker already on the list is idempotent — it returns the
 * existing entry rather than an error, so the caller never needs a
 * duplicate check.
 */
export async function addToWatchlist(
  ticker: string,
  token: string,
): Promise<WatchlistEntryResponse> {
  const response = await fetch(`${API_BASE}/watchlist`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify({ ticker }),
  });
  if (!response.ok) {
    throw new Error(`Add to watchlist failed with status ${response.status}`);
  }
  return (await response.json()) as WatchlistEntryResponse;
}

/**
 * DELETE /watchlist/{entryId} — removes a ticker from the current user's
 * watchlist (WATCH-01). Per D-03 this cascade-deletes the entry's alert
 * rules on the backend. The backend answers 204 with no body, so this
 * function never calls `response.json()`.
 */
export async function removeFromWatchlist(
  entryId: string,
  token: string,
): Promise<void> {
  const response = await fetch(`${API_BASE}/watchlist/${entryId}`, {
    method: "DELETE",
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!response.ok) {
    throw new Error(`Remove from watchlist failed with status ${response.status}`);
  }
}

/**
 * POST /watchlist/{entryId}/rules — creates a new alert rule on a
 * watchlisted ticker (WATCH-03/04/05). `config`'s shape is constrained at
 * compile time by `ruleType` via the `AlertRuleConfig` union, but the
 * backend re-validates every payload server-side (defence in depth).
 */
export async function createAlertRule(
  entryId: string,
  ruleType: AlertRuleType,
  config: AlertRuleConfig,
  token: string,
): Promise<AlertRuleResponse> {
  const response = await fetch(`${API_BASE}/watchlist/${entryId}/rules`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify({ rule_type: ruleType, config }),
  });
  if (!response.ok) {
    throw new Error(`Create alert rule failed with status ${response.status}`);
  }
  return (await response.json()) as AlertRuleResponse;
}

/**
 * PATCH /watchlist/rules/{ruleId} — enables or disables an existing alert
 * rule (WATCH-08). Per D-07 there is no delete-rule counterpart in this
 * phase: disabling is the only way to retire a rule.
 */
export async function toggleAlertRule(
  ruleId: string,
  enabled: boolean,
  token: string,
): Promise<AlertRuleResponse> {
  const response = await fetch(`${API_BASE}/watchlist/rules/${ruleId}`, {
    method: "PATCH",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify({ enabled }),
  });
  if (!response.ok) {
    throw new Error(`Toggle alert rule failed with status ${response.status}`);
  }
  return (await response.json()) as AlertRuleResponse;
}

/**
 * A single alert event, delivered over both REST and the `/ws/notifications`
 * socket (WATCH-06/07). This is the browser-side mirror of
 * `app/api/v1/notifications.py::NotificationResponse` and of
 * `app/services/notification_publisher.py::notification_payload` — a field
 * added on the server must be added here or it is silently dropped.
 */
export interface NotificationEntry {
  id: string;
  alert_rule_id: string;
  message: string;
  triggered_at: string;
  read: boolean;
}

/**
 * Shape returned by `GET /notifications` (matches
 * `app/api/v1/notifications.py::NotificationListResponse`). `unread_count`
 * is the caller's true unread total and is NOT capped by the 50-row
 * `notifications` window, so the bell badge stays accurate past 50.
 */
export interface NotificationListResponse {
  notifications: NotificationEntry[];
  unread_count: number;
}

/**
 * Shape returned by the bulk mark-read route (matches
 * `app/api/v1/notifications.py::MarkReadResponse`).
 */
export interface MarkReadResponse {
  marked: number;
}

/**
 * Shape returned by `GET /notifications/history/{entryId}` (matches
 * `app/api/v1/notifications.py::AlertHistoryResponse`).
 */
export interface AlertHistoryResponse {
  ticker: string;
  events: NotificationEntry[];
}

/**
 * GET /notifications — WATCH-06's durable layer. Returns the caller's most
 * recent 50 events newest-first plus the uncapped unread count. An empty
 * list is a normal 200, never a 404.
 */
export async function getNotifications(
  token: string,
): Promise<NotificationListResponse> {
  const response = await fetch(`${API_BASE}/notifications`, {
    method: "GET",
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!response.ok) {
    throw new Error(`Get notifications failed with status ${response.status}`);
  }
  return (await response.json()) as NotificationListResponse;
}

/**
 * Fires the bulk mark-read action (D-10) when the dropdown opens. There is
 * no per-item mark-read counterpart anywhere in this API. The route takes
 * no request body, so none is sent.
 */
export async function markNotificationsRead(
  token: string,
): Promise<MarkReadResponse> {
  const response = await fetch(`${API_BASE}/notifications/mark-read`, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!response.ok) {
    throw new Error(`Mark notifications read failed with status ${response.status}`);
  }
  return (await response.json()) as MarkReadResponse;
}

/**
 * GET /notifications/history/{entryId} — WATCH-07. Returns the most recent
 * 50 events for that watchlisted ticker (D-08 — no pagination exists, by
 * decision); includes events from alert rules the user has since disabled
 * (D-07). A non-owned or unknown `entryId` answers 404, which surfaces here
 * as a thrown error the caller maps to its error state.
 */
export async function getAlertHistory(
  entryId: string,
  token: string,
): Promise<AlertHistoryResponse> {
  const response = await fetch(`${API_BASE}/notifications/history/${entryId}`, {
    method: "GET",
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!response.ok) {
    throw new Error(`Get alert history failed with status ${response.status}`);
  }
  return (await response.json()) as AlertHistoryResponse;
}
