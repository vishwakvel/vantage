import { useEffect, useRef, useState } from "react";
import {
  getNotifications,
  markNotificationsRead,
  type NotificationEntry,
} from "./api";
import {
  connectNotifications,
  type NotificationConnection,
} from "./ws";

/**
 * A small SVG bell icon, hand-authored — no icon library is installed, and
 * this is the only icon in the entire application (UI-SPEC Design System
 * table). Purely decorative alongside the button's own `aria-label`, so it
 * is marked `aria-hidden`.
 */
function BellIcon() {
  return (
    <svg
      width={24}
      height={24}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9" />
      <path d="M13.73 21a2 2 0 0 1-3.46 0" />
    </svg>
  );
}

/**
 * The notification bell + dropdown: WATCH-06's user-facing surface. Builds
 * on D-09's two-layer delivery (a REST fetch on mount is the durable layer,
 * so a user who was offline when a rule fired still sees it once they load
 * the app; the `/ws/notifications` socket is the live layer, pushing new
 * events without a page refresh) and D-10's bell/badge/dropdown pattern
 * (one bulk mark-read action on open, no per-item control).
 *
 * The socket opens once per session, on login, and is not reopened per
 * research run — a distinct lifecycle from the per-run progress socket
 * used elsewhere in `ws.ts`.
 *
 * V5 (untrusted-adjacent output): `message` strings are server-composed by
 * the evaluator, not raw user input, but are still rendered as plain JSX
 * text interpolation only — React's raw-HTML injection escape hatch is
 * never used, regardless of the string's provenance (T-11-09-XSS).
 */
export default function NotificationBell({ token }: { token: string }) {
  const [notifications, setNotifications] = useState<NotificationEntry[]>([]);
  const [unreadCount, setUnreadCount] = useState(0);
  const [open, setOpen] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [loadError, setLoadError] = useState(false);

  const connectionRef = useRef<NotificationConnection | null>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoaded(false);
    setLoadError(false);

    (async () => {
      try {
        const response = await getNotifications(token);
        if (cancelled) return;
        setNotifications(response.notifications);
        setUnreadCount(response.unread_count);
        setLoaded(true);
      } catch {
        if (cancelled) return;
        setLoadError(true);
      }
    })();

    connectionRef.current = connectNotifications(token, {
      onSnapshot: (snapshotNotifications, snapshotUnreadCount) => {
        if (cancelled) return;
        setNotifications(snapshotNotifications);
        setUnreadCount(snapshotUnreadCount);
        setLoaded(true);
      },
      onNotification: (entry) => {
        if (cancelled) return;
        setNotifications((prev) => [entry, ...prev]);
        setUnreadCount((prev) => prev + 1);
      },
      onClose: () => {
        connectionRef.current = null;
      },
    });

    return () => {
      cancelled = true;
      connectionRef.current?.close();
    };
  }, [token]);

  useEffect(() => {
    if (!open) return;

    function handleMouseDown(event: MouseEvent) {
      if (
        containerRef.current &&
        !containerRef.current.contains(event.target as Node)
      ) {
        setOpen(false);
      }
    }

    document.addEventListener("mousedown", handleMouseDown);
    return () => {
      document.removeEventListener("mousedown", handleMouseDown);
    };
  }, [open]);

  function handleToggle() {
    setOpen((prevOpen) => {
      const nextOpen = !prevOpen;
      if (nextOpen) {
        setUnreadCount(0);
        void markNotificationsRead(token).catch(() => {
          // Recoverable on the next open — must not block the dropdown.
        });
      } else {
        setNotifications((prev) =>
          prev.map((n) => ({ ...n, read: true })),
        );
      }
      return nextOpen;
    });
  }

  return (
    <div className="notification-bell" ref={containerRef}>
      <button
        type="button"
        className="notification-bell-button"
        onClick={handleToggle}
        aria-label={
          unreadCount === 0
            ? "Notifications"
            : `Notifications (${unreadCount} unread)`
        }
        aria-expanded={open}
      >
        <BellIcon />
        {unreadCount > 0 && (
          <span className="notification-badge">{unreadCount}</span>
        )}
      </button>

      {open && (
        <div className="notification-dropdown">
          <h2 className="heading">Notifications</h2>
          {loadError ? (
            <div className="error-banner">
              Couldn't load notifications. Try again.
            </div>
          ) : !loaded ? null : notifications.length === 0 ? (
            <div className="empty-state">
              <p className="body">No notifications yet.</p>
            </div>
          ) : (
            <ul className="notification-list">
              {notifications.map((n) => (
                <li
                  key={n.id}
                  className={
                    n.read
                      ? "notification-item"
                      : "notification-item notification-item-unread"
                  }
                >
                  <span className="notification-message">{n.message}</span>
                  <span className="notification-time">
                    {n.triggered_at.split("T")[0]}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}
