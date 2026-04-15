"use client";

import { useEffect, useState, useCallback } from "react";
import { useRouter } from "next/navigation";
import { api, DashboardData } from "@/lib/api";

function Stat({ label, value, sub, accent }: {
  label: string; value: string | number; sub?: string; accent?: boolean;
}) {
  return (
    <div className="bg-vayancy-surface border border-vayancy-border rounded-xl p-4">
      <p className="text-xs text-vayancy-dim uppercase tracking-wider mb-1">{label}</p>
      <p className={`text-2xl font-medium ${accent ? "text-vayancy-accent" : "text-vayancy-text"}`}>
        {value}
      </p>
      {sub && <p className="text-xs text-vayancy-dim mt-0.5">{sub}</p>}
    </div>
  );
}

function AgentDot({ agent }: { agent: string }) {
  const colors: Record<string, string> = {
    supervisor: "bg-vayancy-accent",
    guest:      "bg-vayancy-green",
    revenue:    "bg-vayancy-amber",
    operations: "bg-vayancy-blue",
  };
  return (
    <span className={`inline-block w-1.5 h-1.5 rounded-full flex-shrink-0 mt-1.5 ${colors[agent] || "bg-vayancy-dim"}`} />
  );
}

function ChannelBadge({ channel }: { channel: string }) {
  const labels: Record<string, string> = {
    "travelos_mcp":    "Direct",
    "travelos_stripe": "Direct (paid)",
    "webhotelier":     "WebHotelier",
  };
  return (
    <span className="text-xs text-vayancy-dim bg-vayancy-bg border border-vayancy-border px-2 py-0.5 rounded-full">
      {labels[channel] || channel}
    </span>
  );
}

function nights(checkIn: string, checkOut: string): number {
  const d1 = new Date(checkIn);
  const d2 = new Date(checkOut);
  return Math.round((d2.getTime() - d1.getTime()) / 86400000);
}

export default function DashboardPage() {
  const router = useRouter();
  const [data,    setData]    = useState<DashboardData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error,   setError]   = useState("");
  const [approving, setApproving] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const d = await api.owner.dashboard();
      setData(d);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  async function handleApprove(id: string) {
    setApproving(id);
    try {
      await api.owner.approveHitl(id);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed");
    } finally {
      setApproving(null);
    }
  }

  async function handleReject(id: string) {
    setApproving(id);
    try {
      await api.owner.rejectHitl(id);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed");
    } finally {
      setApproving(null);
    }
  }

  if (loading) return (
    <div className="flex items-center justify-center h-64">
      <div className="w-5 h-5 border-2 border-vayancy-accent border-t-transparent rounded-full animate-spin" />
    </div>
  );

  if (error) return (
    <div className="p-6">
      <div className="bg-red-950 border border-red-800 text-red-300 rounded-xl px-4 py-3 text-sm">
        {error}
      </div>
    </div>
  );

  const s = data?.stats;

  return (
    <div className="max-w-5xl mx-auto px-4 sm:px-6 py-6 space-y-6">

      {/* Stats */}
      <section>
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
          <Stat
            label="This month"
            value={s?.this_month ?? 0}
            sub="confirmed bookings"
            accent
          />
          <Stat
            label="Direct bookings"
            value={s?.direct_bookings ?? 0}
            sub="via AI — no OTA"
          />
          <Stat
            label="Commission saved"
            value={`€${Math.round(s?.commission_saved_eur ?? 0).toLocaleString()}`}
            sub="vs Booking.com 17%"
          />
          <Stat
            label="Total guests"
            value={s?.unique_guests ?? 0}
            sub="all time"
          />
        </div>
      </section>

      {/* HITL decisions — show only if pending */}
      {(data?.pending_hitl?.length ?? 0) > 0 && (
        <section>
          <div className="flex items-center gap-2 mb-3">
            <h2 className="text-sm font-medium text-vayancy-text">Decisions waiting for you</h2>
            <span className="text-xs bg-vayancy-amber text-vayancy-bg font-medium px-2 py-0.5 rounded-full">
              {data!.pending_hitl.length}
            </span>
          </div>
          <div className="space-y-2">
            {data!.pending_hitl.map(h => (
              <div
                key={h.id}
                className="bg-vayancy-surface border border-vayancy-amber border-opacity-40 rounded-xl p-4"
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="flex-1 min-w-0">
                    <p className="text-xs text-vayancy-amber uppercase tracking-wider mb-1">
                      Revenue agent — rate change
                    </p>
                    <p className="text-sm text-vayancy-text leading-relaxed">
                      {h.impact_summary}
                    </p>
                  </div>
                  <div className="flex gap-2 flex-shrink-0">
                    <button
                      onClick={() => handleReject(h.id)}
                      disabled={approving === h.id}
                      className="text-xs border border-vayancy-border text-vayancy-dim px-3 py-1.5 rounded-lg hover:border-red-700 hover:text-red-400 disabled:opacity-40 transition-colors"
                    >
                      Reject
                    </button>
                    <button
                      onClick={() => handleApprove(h.id)}
                      disabled={approving === h.id}
                      className="text-xs bg-vayancy-green text-vayancy-bg font-medium px-3 py-1.5 rounded-lg disabled:opacity-40 transition-opacity"
                    >
                      {approving === h.id ? "…" : "Approve"}
                    </button>
                  </div>
                </div>
              </div>
            ))}
          </div>
        </section>
      )}

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-6">

        {/* Recent bookings */}
        <section>
          <div className="flex items-center justify-between mb-3">
            <h2 className="text-sm font-medium text-vayancy-text">Recent bookings</h2>
            <button
              onClick={() => router.push("/dashboard/hitl")}
              className="text-xs text-vayancy-dim hover:text-vayancy-accent transition-colors"
            >
              View all →
            </button>
          </div>
          <div className="bg-vayancy-surface border border-vayancy-border rounded-xl overflow-hidden">
            {(data?.recent_bookings?.length ?? 0) === 0 ? (
              <p className="text-sm text-vayancy-dim text-center py-8">
                No bookings yet. Your first will appear here.
              </p>
            ) : (
              <div className="divide-y divide-vayancy-border">
                {data!.recent_bookings.map(b => (
                  <div key={b.booking_id} className="px-4 py-3 flex items-start justify-between gap-3">
                    <div className="min-w-0 flex-1">
                      <p className="text-sm font-medium text-vayancy-text truncate">
                        {b.guest_name}
                      </p>
                      <p className="text-xs text-vayancy-dim mt-0.5">
                        {b.check_in} → {b.check_out}
                        <span className="ml-1.5 text-vayancy-border">·</span>
                        <span className="ml-1.5">{nights(b.check_in, b.check_out)} nights</span>
                      </p>
                    </div>
                    <ChannelBadge channel={b.channel} />
                  </div>
                ))}
              </div>
            )}
          </div>
        </section>

        {/* Agent activity */}
        <section>
          <h2 className="text-sm font-medium text-vayancy-text mb-3">What the AI did</h2>
          <div className="bg-vayancy-surface border border-vayancy-border rounded-xl overflow-hidden">
            {(data?.recent_activity?.length ?? 0) === 0 ? (
              <p className="text-sm text-vayancy-dim text-center py-8">
                Agent activity will appear here.
              </p>
            ) : (
              <div className="divide-y divide-vayancy-border max-h-72 overflow-auto">
                {data!.recent_activity.map((a, i) => (
                  <div key={i} className="px-4 py-2.5 flex items-start gap-2.5">
                    <AgentDot agent={a.agent} />
                    <div className="flex-1 min-w-0">
                      <p className="text-xs text-vayancy-text leading-relaxed">{a.action}</p>
                      <p className="text-xs text-vayancy-dim mt-0.5">
                        {new Date(a.timestamp).toLocaleTimeString("en-GB", {
                          hour: "2-digit", minute: "2-digit",
                        })}
                      </p>
                    </div>
                    {a.status === "error" && (
                      <span className="w-1.5 h-1.5 rounded-full bg-red-400 flex-shrink-0 mt-1.5" />
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>
        </section>
      </div>
    </div>
  );
}
