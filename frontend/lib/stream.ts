export interface WorkflowEvent {
  id:          string;
  workflow_id: string;
  event_id:    string | null;
  agent:       "supervisor" | "guest" | "revenue" | "operations" | "worker";
  action:      string;
  status:      "completed" | "error" | "thinking" | "workflow_status";
  details:     Record<string, unknown> | null;
  timestamp:   string;
}

type Handler = (event: WorkflowEvent) => void;

const RECONNECT_DELAY_MS = 3000;

export function createEventStream(
  propertyId: string,
  onEvent: Handler,
  onConnected?: () => void,
  onDisconnected?: () => void,
): () => void {
  let es: EventSource | null = null;
  let stopped = false;
  let retryTimeout: ReturnType<typeof setTimeout> | null = null;

  function connect() {
    if (stopped) return;

    const url = `/api/stream/workflows?property_id=${encodeURIComponent(propertyId)}`;
    es = new EventSource(url);

    es.onopen = () => {
      onConnected?.();
    };

    es.onmessage = (e) => {
      try {
        const data = JSON.parse(e.data) as WorkflowEvent;
        onEvent(data);
      } catch {
        // malformed event — ignore
      }
    };

    es.onerror = () => {
      es?.close();
      es = null;
      onDisconnected?.();
      if (!stopped) {
        retryTimeout = setTimeout(connect, RECONNECT_DELAY_MS);
      }
    };
  }

  connect();

  return () => {
    stopped = true;
    if (retryTimeout) clearTimeout(retryTimeout);
    es?.close();
  };
}
