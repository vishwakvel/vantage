import { WS_BASE } from "./config";

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
