"use client";

import { useEffect, useState, useCallback } from "react";
import { api, HITLDecision } from "@/lib/api";

export default function HITLPage() {
  const [decisions, setDecisions] = useState<HITLDecision[]>([]);
  const [pending,   setPending]   = useState(0);
  const [loading,   setLoading]   = useState(true);
  const [error,     setError]     = useState("");
  const [acting,    setActing]    = useState<string | null>(null);
  const [notes,     setNotes]     = useState<Record<string, string>>({});

  const load = useCallback(async () => {
    try {
      const res = await api.owner.hitl();
      setDecisions(res.decisions);
      setPending(res.pending);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  async function approve(id: string) {
    setActing(id);
    try {
      await api.owner.approveHitl(id, notes[id]);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed");
    } finally {
      setActing(null);
    }
  }

  async function reject(id: string) {
    setActing(id);
    try {
      await api.owner.rejectHitl(id, notes[id]);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed");
    } finally {
      setActing(null);
    }
  }

  const statusColors: Record<string, string> = {
    pending:  "text-vayancy-amber bg-amber-950 border-amber-800",
    approved: "text-vayancy-green bg-green-950 border-green-800",
    rejected: "text-red-400 bg-red-950 border-red-800",
  };

  return (
    <div className="max-w-3xl mx-auto px-4 sm:px-6 py-6">

      <div className="flex items-center gap-3 mb-6">
        <h1 className="text-lg font-medium text-vayancy-text">Pricing decisions</h1>
        {pending > 0 && (
          <span className="text-xs bg-vayancy-amber text-vayancy-bg font-medium px-2.5 py-0.5 rounded-full">
            {pending} pending
          </span>
        )}
      </div>

      {error && (
        <div className="bg-red-950 border border-red-800 text-red-300 rounded-xl px-4 py-3 text-sm mb-4">
          {error}
        </div>
      )}

      {loading ? (
        <div className="flex items-center justify-center h-32">
          <div className="w-5 h-5 border-2 border-vayancy-accent border-t-transparent rounded-full animate-spin" />
        </div>
      ) : decisions.length === 0 ? (
        <div className="bg-vayancy-surface border border-vayancy-border rounded-xl p-8 text-center">
          <p className="text-vayancy-dim text-sm">
            No pricing decisions yet. When the revenue agent wants to make a large rate change,
            it will appear here for your approval.
          </p>
        </div>
      ) : (
        <div className="space-y-3">
          {decisions.map(d => (
            <div
              key={d.id}
              className="bg-vayancy-surface border border-vayancy-border rounded-xl p-5"
            >
              <div className="flex items-start justify-between gap-3 mb-3">
                <div>
                  <span className={`text-xs font-medium px-2 py-0.5 rounded-full border ${statusColors[d.status || "pending"]}`}>
                    {d.status || "pending"}
                  </span>
                </div>
                <span className="text-xs text-vayancy-dim">
                  {new Date(d.created_at).toLocaleDateString("en-GB", {
                    day: "numeric", month: "short", hour: "2-digit", minute: "2-digit",
                  })}
                </span>
              </div>

              <p className="text-sm text-vayancy-text leading-relaxed mb-3">
                {d.impact_summary}
              </p>

              {d.status === "pending" && (
                <>
                  <div className="mb-3">
                    <input
                      type="text"
                      value={notes[d.id] || ""}
                      onChange={e => setNotes(p => ({ ...p, [d.id]: e.target.value }))}
                      placeholder="Add a note (optional)"
                      className="w-full bg-vayancy-bg border border-vayancy-border rounded-lg px-3 py-2 text-sm text-vayancy-text placeholder-vayancy-border focus:outline-none focus:border-vayancy-accent"
                    />
                  </div>
                  <div className="flex gap-2">
                    <button
                      onClick={() => reject(d.id)}
                      disabled={acting === d.id}
                      className="flex-1 border border-vayancy-border text-vayancy-dim text-sm py-2 rounded-lg hover:border-red-700 hover:text-red-400 disabled:opacity-40 transition-colors"
                    >
                      Reject
                    </button>
                    <button
                      onClick={() => approve(d.id)}
                      disabled={acting === d.id}
                      className="flex-1 bg-vayancy-green text-vayancy-bg font-medium text-sm py-2 rounded-lg disabled:opacity-40 transition-opacity"
                    >
                      {acting === d.id ? "…" : "Approve and execute"}
                    </button>
                  </div>
                </>
              )}

              {d.status !== "pending" && d.decision_note && (
                <p className="text-xs text-vayancy-dim mt-2">
                  Note: {d.decision_note}
                  {d.decided_at && (
                    <span className="ml-2">
                      · {new Date(d.decided_at).toLocaleDateString("en-GB")}
                    </span>
                  )}
                </p>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
