"use client";

import { useEffect, useState, useCallback } from "react";
import { api, IntegrationStatus } from "@/lib/api";

function Section({ title, sub, children }: {
  title: string; sub?: string; children: React.ReactNode;
}) {
  return (
    <div className="bg-vayancy-surface border border-vayancy-border rounded-xl overflow-hidden">
      <div className="px-5 py-4 border-b border-vayancy-border">
        <h2 className="text-sm font-medium text-vayancy-text">{title}</h2>
        {sub && <p className="text-xs text-vayancy-dim mt-0.5">{sub}</p>}
      </div>
      <div className="px-5 py-4">{children}</div>
    </div>
  );
}

function FieldRow({ label, sub, children }: {
  label: string; sub?: string; children: React.ReactNode;
}) {
  return (
    <div className="flex items-start justify-between gap-4 py-3 border-b border-vayancy-border last:border-0">
      <div className="min-w-0 flex-1">
        <p className="text-sm text-vayancy-text">{label}</p>
        {sub && <p className="text-xs text-vayancy-dim mt-0.5">{sub}</p>}
      </div>
      <div className="flex-shrink-0">{children}</div>
    </div>
  );
}

function IntegrationRow({ name, status, detail }: {
  name: string; status: string; detail: string;
}) {
  const ok = status === "connected" || status === "active";
  return (
    <div className="flex items-center justify-between py-3 border-b border-vayancy-border last:border-0">
      <div>
        <p className="text-sm text-vayancy-text">{name}</p>
        <p className="text-xs text-vayancy-dim mt-0.5">{detail}</p>
      </div>
      <div className="flex items-center gap-1.5">
        <span className={`w-1.5 h-1.5 rounded-full ${ok ? "bg-vayancy-green" : "bg-red-400"}`} />
        <span className={`text-xs ${ok ? "text-vayancy-green" : "text-red-400"}`}>
          {ok ? "Connected" : "Error"}
        </span>
      </div>
    </div>
  );
}

export default function SettingsPage() {
  const [integrations, setIntegrations] = useState<IntegrationStatus | null>(null);
  const [intLoading,   setIntLoading]   = useState(true);

  // Agent settings
  const [hitlPct,     setHitlPct]     = useState("25");
  const [floor,       setFloor]       = useState("150");
  const [ceiling,     setCeiling]     = useState("2500");
  const [ownerPhone,  setOwnerPhone]  = useState("");

  const [saving,   setSaving]   = useState(false);
  const [saved,    setSaved]    = useState(false);
  const [error,    setError]    = useState("");

  const loadIntegrations = useCallback(async () => {
    try {
      const res = await api.owner.integrations();
      setIntegrations(res);
    } catch {
      // silent — not critical
    } finally {
      setIntLoading(false);
    }
  }, []);

  useEffect(() => { loadIntegrations(); }, [loadIntegrations]);

  async function saveSettings() {
    setSaving(true); setError(""); setSaved(false);
    try {
      await api.owner.updateSettings({
        hitl_threshold_pct:   parseFloat(hitlPct)  || 25,
        pricing_floor_eur:    parseFloat(floor)     || 150,
        pricing_ceiling_eur:  parseFloat(ceiling)   || 2500,
        owner_whatsapp_phone: ownerPhone || undefined,
      });
      setSaved(true);
      setTimeout(() => setSaved(false), 2500);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Save failed");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="max-w-2xl mx-auto px-4 sm:px-6 py-6 space-y-5">

      <h1 className="text-lg font-medium text-vayancy-text">Settings</h1>

      {error && (
        <div className="bg-red-950 border border-red-800 text-red-300 rounded-xl px-4 py-3 text-sm">
          {error}
        </div>
      )}

      {/* Agent settings */}
      <Section
        title="Agent behaviour"
        sub="Control how aggressively the AI acts on your behalf."
      >
        <FieldRow
          label="HITL threshold"
          sub="Revenue agent asks for your approval before making any single rate change above this percentage."
        >
          <div className="flex items-center gap-2">
            <input
              type="number"
              value={hitlPct}
              onChange={e => setHitlPct(e.target.value)}
              min="5"
              max="100"
              className="w-16 bg-vayancy-bg border border-vayancy-border rounded-lg px-2 py-1.5 text-sm text-vayancy-text text-center focus:outline-none focus:border-vayancy-accent"
            />
            <span className="text-sm text-vayancy-dim">%</span>
          </div>
        </FieldRow>

        <FieldRow
          label="Pricing floor"
          sub="Revenue agent will never set a nightly rate below this amount."
        >
          <div className="flex items-center gap-2">
            <span className="text-sm text-vayancy-dim">€</span>
            <input
              type="number"
              value={floor}
              onChange={e => setFloor(e.target.value)}
              min="50"
              className="w-20 bg-vayancy-bg border border-vayancy-border rounded-lg px-2 py-1.5 text-sm text-vayancy-text text-center focus:outline-none focus:border-vayancy-accent"
            />
          </div>
        </FieldRow>

        <FieldRow
          label="Pricing ceiling"
          sub="Revenue agent will never set a nightly rate above this amount."
        >
          <div className="flex items-center gap-2">
            <span className="text-sm text-vayancy-dim">€</span>
            <input
              type="number"
              value={ceiling}
              onChange={e => setCeiling(e.target.value)}
              min="100"
              className="w-24 bg-vayancy-bg border border-vayancy-border rounded-lg px-2 py-1.5 text-sm text-vayancy-text text-center focus:outline-none focus:border-vayancy-accent"
            />
          </div>
        </FieldRow>

        <FieldRow
          label="Owner WhatsApp"
          sub="Your number for escalation alerts and urgent agent notifications."
        >
          <input
            type="tel"
            value={ownerPhone}
            onChange={e => setOwnerPhone(e.target.value)}
            placeholder="+306912345678"
            className="w-40 bg-vayancy-bg border border-vayancy-border rounded-lg px-2 py-1.5 text-sm text-vayancy-text placeholder-vayancy-border focus:outline-none focus:border-vayancy-accent"
          />
        </FieldRow>

        <div className="pt-3 flex items-center gap-3">
          <button
            onClick={saveSettings}
            disabled={saving}
            className="bg-vayancy-accent text-vayancy-bg font-medium text-sm px-5 py-2 rounded-lg disabled:opacity-40 transition-opacity"
          >
            {saving ? "Saving…" : "Save settings"}
          </button>
          {saved && (
            <span className="text-xs text-vayancy-green flex items-center gap-1.5">
              <span className="w-1.5 h-1.5 rounded-full bg-vayancy-green" />
              Saved
            </span>
          )}
        </div>
      </Section>

      {/* Integrations */}
      <Section
        title="Integrations"
        sub="Live connection status. Reconnect from the setup flow if any show an error."
      >
        {intLoading ? (
          <div className="flex items-center justify-center py-6">
            <div className="w-4 h-4 border-2 border-vayancy-accent border-t-transparent rounded-full animate-spin" />
          </div>
        ) : integrations ? (
          <>
            <IntegrationRow
              name="WebHotelier PMS"
              status={integrations.webhotelier.status}
              detail={`Property ID: ${integrations.webhotelier.property_id || "—"}`}
            />
            <IntegrationRow
              name="WhatsApp Business"
              status={integrations.whatsapp.status}
              detail={integrations.whatsapp.phone_number || "Phone number not verified"}
            />
            <IntegrationRow
              name="AI agents (Claude)"
              status={integrations.ai_agents.status}
              detail={`Model: ${integrations.ai_agents.model}`}
            />
            <IntegrationRow
              name="Voyage AI embeddings"
              status={integrations.voyage_ai.status}
              detail={integrations.voyage_ai.status === "active"
                ? "1024-dim semantic memory active"
                : "Set VOYAGE_API_KEY to enable guest memory"}
            />
          </>
        ) : (
          <p className="text-sm text-vayancy-dim text-center py-4">
            Could not load integration status
          </p>
        )}
      </Section>

      {/* Webhook reference */}
      <Section
        title="Webhook reference"
        sub="These URLs need to be set in your integrations. They receive events from WebHotelier and Meta."
      >
        <FieldRow label="WebHotelier webhook" sub="Set in WebHotelier portal → Settings → Webhooks">
          <code className="text-xs text-vayancy-green font-mono">
            /api/webhook/webhotelier
          </code>
        </FieldRow>
        <FieldRow label="WhatsApp webhook" sub="Set in Meta Developer Portal → App → WhatsApp → Configuration">
          <code className="text-xs text-vayancy-green font-mono">
            /api/webhook/whatsapp
          </code>
        </FieldRow>
        <FieldRow label="WhatsApp verify token" sub="The token Meta uses to verify ownership">
          <code className="text-xs text-vayancy-green font-mono">
            vayancy-verify
          </code>
        </FieldRow>
      </Section>

      {/* Danger zone */}
      <Section title="Account">
        <FieldRow label="Need to reconnect integrations?" sub="Go back through the setup wizard">
          <a
            href="/onboarding"
            className="text-xs text-vayancy-accent border border-vayancy-border px-3 py-1.5 rounded-lg hover:border-vayancy-accent transition-colors"
          >
            Rerun setup
          </a>
        </FieldRow>
      </Section>

    </div>
  );
}
