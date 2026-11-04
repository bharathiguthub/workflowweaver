import { apiUrl } from "./client";
import { EXECUTION_EVENT_TYPES, TERMINAL_EVENT_TYPES, type ExecutionEvent } from "./types";

/**
 * connecting   - EventSource created, not yet open
 * open         - receiving events
 * reconnecting - connection dropped; the browser is retrying (resumes with Last-Event-ID)
 * closed       - terminal event received; closed by us
 * failed       - the browser gave up (e.g. 404 expired stream / wrong token, or 204)
 */
export type ConnectionState = "connecting" | "open" | "reconnecting" | "closed" | "failed";

export interface StreamHandlers {
  onEvent: (event: ExecutionEvent) => void;
  onState: (state: ConnectionState) => void;
}

/** Appends last_event_id so a manual reconnect resumes where we left off. */
export function streamUrlFrom(streamUrl: string, lastEventId: number): string {
  if (lastEventId <= 0) return streamUrl;
  return `${streamUrl}${streamUrl.includes("?") ? "&" : "?"}last_event_id=${lastEventId}`;
}

/**
 * Opens the SSE stream. Events are delivered in event_id order; anything at or below the
 * last delivered id (possible after a reconnect) is ignored. Returns a close function.
 */
export function openExecutionStream(streamUrl: string, handlers: StreamHandlers, lastEventId = 0): () => void {
  const source = new EventSource(apiUrl(streamUrlFrom(streamUrl, lastEventId)));
  let lastId = lastEventId;
  let closedByUs = false;

  const close = () => {
    closedByUs = true;
    source.close();
  };

  handlers.onState("connecting");
  source.onopen = () => handlers.onState("open");
  source.onerror = () => {
    if (closedByUs) return;
    // CLOSED: the browser will not retry (HTTP error such as 404, or 204). Otherwise it
    // is reconnecting automatically.
    handlers.onState(source.readyState === EventSource.CLOSED ? "failed" : "reconnecting");
  };

  for (const type of EXECUTION_EVENT_TYPES) {
    source.addEventListener(type, (message: MessageEvent) => {
      let event: ExecutionEvent;
      try {
        event = JSON.parse(String(message.data)) as ExecutionEvent;
      } catch {
        return; // malformed frame: ignore rather than crash the UI
      }
      if (typeof event.event_id !== "number" || event.event_id <= lastId) return;
      lastId = event.event_id;
      handlers.onEvent(event);
      if (TERMINAL_EVENT_TYPES.has(event.event_type)) {
        close();
        handlers.onState("closed");
      }
    });
  }
  return close;
}
