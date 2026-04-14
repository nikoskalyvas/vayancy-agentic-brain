"use client";

import { useEffect, useRef, useState } from "react";
import { createEventStream, WorkflowEvent } from "@/lib/stream";

const MAX_EVENTS = 200;

const AGENT_META: Record<string, { label: string; color: string }> = {
  supervisor: { label: "Core AI",     color: "text-vayancy-accent" },
  guest:      { label: "Guest",       color: "text-vayancy-green"  },
  revenue:    { label: "Revenue",     color: "text-vayancy-amber"  },
  operations: { label: "Operations",  color: "text-vayancy-blue"   },
  worker:     { label: "Worker",      color: "text-vayancy-dim"    },
};

const STATUS_DOT: Record<string, string> = {
  completed:       "bg-vayancy-green",
  error:           "bg-vayancy-red",
  thinking:        "bg-vayancy-amber animate-pulse-dot",
  workflow_status: "bg-vayancy-blue",
};

function formatTime(iso: string): string {
  try {
    return new Date(iso).toLocaleTimeString("en-GB", {
      hour:   "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  } catch {
    return "—";
  }
}

function formatAction(action: string): string {
  return action.replace(/^tool:/, "").replace(/_/g, " ");
}

interface Props {
  propertyId: string;
}

export default function WorkflowMatrix({ propertyId }: Props) {
  const [events,      setEvents]      = useState<WorkflowEvent[]>([]);
  const [connected,   setConnected]   = useState(false);
  const [stats,       setStats]       = useState({ total: 0, errors: 0 });
  const bottomRef = useRef<HTMLDivElement>(null);
  const autoScroll = useRef(true);

  useEffect(() => {
    const stop = createEventStream(
      propertyId,
      (ev) => {
        setEvents((prev) => {
          const next = [ev, ...prev].slice(0, MAX_EVENTS);
          return next;
        });
        setStats((s) => ({
          total:  s.total + 1,
          errors: s.errors + (ev.status === "error" ? 1 : 0),
        }));
      },
      () => setConnected(true),
      () => setConnected(false),
    );
    return stop;
  }, [propertyId]);

  // Auto-scroll newest events into view
  useEffect(() => {
    if (autoScroll.current && bottomRef.current) {
      bottomRef.current.scrollIntoView({ behavior: "smooth" });
    }
  }, [events]);

  const agentCounts = events.reduce<Record<string, number>>((acc, ev) => {
    acc[ev.agent] = (acc[ev.agent] || 0) + 1;
    return acc;
  }, {});

  return (
    <div className="flex flex-col gap-4">

      {/* Header row */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span
            className={`inline-block w-2 h-2 rounded-full ${
              connected ? "bg-vayancy-green animate-pulse-dot" : "bg-vayancy-dim"
            }`}
          />
          <span className="text-xs text-vayancy-dim font-mono">
            {connected ? "LIVE" : "RECONNECTING..."}
          </span>
        </div>
        <div className="flex items-center gap-4 text-xs text-vayancy-dim font-mono">
          <span>{stats.total} events</span>
          {stats.errors > 0 && (
            <span className="text-vayancy-red">{stats.errors} errors</span>
          )}
        </div>
      </div>

      {/* Agent activity pills */}
      <div className="flex gap-2 flex-wrap">
        {Object.entries(AGENT_META).map(([key, meta]) => (
          <div
            key={key}
            className="flex items-center gap-1.5 px-2.5 py-1 rounded-full
                       bg-vayancy-surface border border-vayancy-border
                       text-xs font-mono"
          >
            <span className={meta.color}>●</span>
            <span className="text-vayancy-dim">{meta.label}</span>
            <span className="text-vayancy-text font-medium">
              {agentCounts[key] || 0}
            </span>
          </div>
        ))}
      </div>

      {/* Log table */}
      <div
        className="rounded-lg border border-vayancy-border bg-vayancy-surface
                   overflow-y-auto max-h-[540px] font-mono text-xs"
        onScroll={(e) => {
          const el = e.currentTarget;
          autoScroll.current =
            el.scrollTop + el.clientHeight >= el.scrollHeight - 40;
        }}
      >
        {events.length === 0 ? (
          <div className="flex items-center justify-center h-32 text-vayancy-dim">
            Waiting for workflow events…
          </div>
        ) : (
          <table className="w-full">
            <thead className="sticky top-0 bg-vayancy-surface border-b border-vayancy-border">
              <tr className="text-vayancy-dim text-left">
                <th className="px-3 py-2 w-20">Time</th>
                <th className="px-3 py-2 w-24">Agent</th>
                <th className="px-3 py-2 w-4">·</th>
                <th className="px-3 py-2">Action</th>
                <th className="px-3 py-2 w-16 text-right">Status</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-vayancy-border">
              {events.map((ev) => {
                const meta = AGENT_META[ev.agent] ?? {
                  label: ev.agent,
                  color: "text-vayancy-dim",
                };
                const tool = ev.action.startsWith("tool:")
                  ? ev.action.slice(5)
                  : null;
                return (
                  <tr
                    key={ev.id}
                    className="log-row hover:bg-vayancy-muted transition-colors"
                  >
                    <td className="px-3 py-1.5 text-vayancy-dim whitespace-nowrap">
                      {formatTime(ev.timestamp)}
                    </td>
                    <td className={`px-3 py-1.5 font-medium ${meta.color}`}>
                      {meta.label}
                    </td>
                    <td className="px-1 py-1.5 text-vayancy-border">│</td>
                    <td className="px-3 py-1.5 text-vayancy-text">
                      {tool ? (
                        <>
                          <span className="text-vayancy-dim">tool </span>
                          <span className="text-vayancy-accent">
                            {formatAction(tool)}
                          </span>
                        </>
                      ) : (
                        <span>{formatAction(ev.action)}</span>
                      )}
                      {ev.details?.reason != null && (
                        <span className="ml-2 text-vayancy-dim">
                          — {String(ev.details.reason as string).slice(0, 60)}
                        </span>
                      )}
                    </td>
                    <td className="px-3 py-1.5 text-right">
                      <span
                        className={`inline-block w-1.5 h-1.5 rounded-full ${
                          STATUS_DOT[ev.status] ?? "bg-vayancy-dim"
                        }`}
                      />
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
        <div ref={bottomRef} />
      </div>

    </div>
  );
}
