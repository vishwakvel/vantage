import { WS_BASE } from "./config";
import type { NotificationEntry } from "./api";

export interface AgentSnapshotEntry {
  agent_type: string;
  status: string;
}

export type ProgressMessage =
  | { type: "snapshot"; agents: AgentSnapshotEntry[] }
  | { type: "agent"; agent_type: string; status: string }
  | { type: "terminal"; memo_status: string };

export interface ProgressHandlers {
  onSnapshot: (agents: AgentSnapshotEntry[]) => void;
  onAgent: (agentType: string, status: string) => void;
  onTerminal: (memoStatus: string) => void;
  /**
   * Called on WS close. `wasTerminal` is true if a terminal message was
   * observed before the close (expected server-initiated close per D-10),
   * false if the socket dropped unexpectedly (UI-SPEC interaction state 6 —
   * show "Connection lost", never attempt to reconnect).
   */
  onClose: (wasTerminal: boolean) => void;
}

export interface ProgressConnection {
  close: () => void;
}

/**
 * Opens a native WebSocket to /ws/research/{memoId}?token=... and dispatches
 * snapshot / agent / terminal messages (06-04's WS->browser protocol) to the
 * supplied handlers. No socket.io, no reconnection logic — the server closes
 * the socket itself on a terminal event (D-10); an unexpected close (no
 * terminal message seen first) is surfaced via onClose(false) instead of a
 * retry.
 */
export function connectProgress(
  memoId: string,
  token: string,
  handlers: ProgressHandlers,
): ProgressConnection {
  const url = `${WS_BASE}/ws/research/${memoId}?token=${encodeURIComponent(token)}`;
  const socket = new WebSocket(url);
  let sawTerminal = false;

  socket.onmessage = (event: MessageEvent<string>) => {
    const message = JSON.parse(event.data) as ProgressMessage;
    switch (message.type) {
      case "snapshot":
        handlers.onSnapshot(message.agents);
        break;
      case "agent":
        handlers.onAgent(message.agent_type, message.status);
        break;
      case "terminal":
        sawTerminal = true;
        handlers.onTerminal(message.memo_status);
        break;
    }
  };

  socket.onclose = () => {
    handlers.onClose(sawTerminal);
  };

  return {
    close: () => socket.close(),
  };
}

/**
 * WS->browser protocol for the notifications socket (WATCH-06, D-09). The
 * server sends one `{"type": "snapshot", ...}` frame on connect, then zero
 * or more `{"type": "notification", ...}` frames — one per
 * `NotificationEntry`'s fields spread directly alongside `type`, rather than
 * nested under a sub-object.
 */
export type NotificationMessage =
  | { type: "snapshot"; notifications: NotificationEntry[]; unread_count: number }
  | ({ type: "notification" } & NotificationEntry);

export interface NotificationHandlers {
  onSnapshot: (notifications: NotificationEntry[], unreadCount: number) => void;
  onNotification: (notification: NotificationEntry) => void;
  /**
   * Called on WS close. Takes NO argument, unlike
   * `ProgressHandlers.onClose(wasTerminal)` — this channel has no terminal
   * event to distinguish an expected close from an unexpected one, so there
   * is no `wasTerminal` flag to report.
   */
  onClose: () => void;
}

export interface NotificationConnection {
  close: () => void;
}

/**
 * Opens a native WebSocket to the long-lived notifications route and
 * dispatches snapshot / notification messages to the supplied handlers.
 *
 * Three lifecycle divergences from `connectProgress`, so no future reader
 * ports the wrong behaviour:
 *   (a) it connects once per session at login rather than once per run;
 *   (b) the server never closes it — it has no terminal event and the
 *       listen loop never breaks (D-09);
 *   (c) there is still no reconnection logic and no socket.io, matching the
 *       existing convention — an unexpected close surfaces through
 *       `onClose` for the component to render, never a silent retry.
 */
export function connectNotifications(
  token: string,
  handlers: NotificationHandlers,
): NotificationConnection {
  const url = `${WS_BASE}/ws/notifications?token=${encodeURIComponent(token)}`;
  const socket = new WebSocket(url);

  socket.onmessage = (event: MessageEvent<string>) => {
    const message = JSON.parse(event.data) as NotificationMessage;
    switch (message.type) {
      case "snapshot":
        handlers.onSnapshot(message.notifications, message.unread_count);
        break;
      case "notification":
        handlers.onNotification(message);
        break;
    }
  };

  socket.onclose = () => {
    handlers.onClose();
  };

  return {
    close: () => socket.close(),
  };
}
