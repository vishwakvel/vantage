// Single source of truth for the backend base URL. No other module in this
// app may hardcode an http(s):// or ws(s):// base — everything imports
// API_BASE / WS_BASE from here (mirrors app/core/config.py Settings on the
// backend — PATTERNS.md §Frontend).

export const API_BASE: string =
  import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000/api/v1";

// Derive the WS base by swapping the http(s) scheme for ws(s) on the same
// host/prefix, e.g. "http://localhost:8000/api/v1" -> "ws://localhost:8000/api/v1".
export const WS_BASE: string = API_BASE.replace(/^http/, "ws");
