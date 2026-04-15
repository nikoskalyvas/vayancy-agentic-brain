"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/lib/auth";
import { api } from "@/lib/api";

type Step = 1 | 2 | 3 | 4;

const AMENITY_OPTIONS = [
  "pool", "sea_view", "wifi", "ac", "bbq", "parking",
  "gym", "beach_access", "garden", "concierge",
];

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  function copy() {
    navigator.clipboard.writeText(text);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  }
  return (
    <button
      onClick={copy}
      className="text-xs text-vayancy-accent border border-vayancy-border px-2 py-1 rounded hover:bg-vayancy-surface transition-colors"
    >
      {copied ? "Copied!" : "Copy"}
    </button>
  );
}

function CodeBlock({ children }: { children: string }) {
  return (
    <div className="flex items-center justify-between bg-vayancy-bg border border-vayancy-border rounded-lg px-3 py-2 mt-1.5">
      <code className="text-xs text-vayancy-green font-mono break-all">{children}</code>
      <CopyButton text={children} />
    </div>
  );
}

function StatusPill({ ok, testing }: { ok: boolean | null; testing: boolean }) {
  if (testing) return (
    <span className="text-xs text-vayancy-amber flex items-center gap-1.5">
      <span className="w-3.5 h-3.5 border-2 border-vayancy-amber border-t-transparent rounded-full animate-spin inline-block" />
      Testing…
    </span>
  );
  if (ok === true)  return <span className="text-xs text-vayancy-green flex items-center gap-1.5"><span className="w-2 h-2 rounded-full bg-vayancy-green inline-block" />Connected</span>;
  if (ok === false) return <span className="text-xs text-red-400 flex items-center gap-1.5"><span className="w-2 h-2 rounded-full bg-red-400 inline-block" />Failed</span>;
  return null;
}

export default function OnboardingPage() {
  const { user, login, isLoading } = useAuth();
  const router = useRouter();
  const [step, setStep] = useState<Step>(1);
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);

  // WebHotelier
  const [whKey,    setWhKey]    = useState("");
  const [whPropId, setWhPropId] = useState("");
  const [whOk,     setWhOk]     = useState<boolean | null>(null);
  const [whTesting, setWhTesting] = useState(false);

  // WhatsApp
  const [waToken,  setWaToken]  = useState("");
  const [waPid,    setWaPid]    = useState("");
  const [waOk,     setWaOk]     = useState<boolean | null>(null);
  const [waTesting, setWaTesting] = useState(false);
  const [waPhone,  setWaPhone]  = useState("");

  // Property
  const [propName,     setPropName]     = useState("");
  const [propLocation, setPropLocation] = useState("");
  const [maxGuests,    setMaxGuests]    = useState(4);
  const [bedrooms,     setBedrooms]     = useState(2);
  const [baseRate,     setBaseRate]     = useState("");
  const [amenities,    setAmenities]    = useState<string[]>([]);

  useEffect(() => {
    if (!isLoading && !user) router.replace("/login");
    if (!isLoading && user?.onboarding_complete) router.replace("/dashboard");
  }, [user, isLoading, router]);

  async function testWebHotelier() {
    if (!whKey || !whPropId) return;
    setWhTesting(true); setWhOk(null); setError("");
    try {
      const res = await api.onboarding.testWebHotelier(whKey, whPropId);
      setWhOk(res.success);
      if (!res.success) setError(res.error || "Connection failed");
    } catch (e) {
      setWhOk(false);
      setError(e instanceof Error ? e.message : "Test failed");
    } finally {
      setWhTesting(false);
    }
  }

  async function testWhatsApp() {
    if (!waToken || !waPid) return;
    setWaTesting(true); setWaOk(null); setError("");
    try {
      const res = await api.onboarding.testWhatsApp(waToken, waPid);
      setWaOk(res.success);
      if (res.success) setWaPhone(res.phone_number || "");
      else setError(res.error || "Connection failed");
    } catch (e) {
      setWaOk(false);
      setError(e instanceof Error ? e.message : "Test failed");
    } finally {
      setWaTesting(false);
    }
  }

  async function completeOnboarding() {
    if (!propName || !propLocation) return;
    setSubmitting(true); setError("");
    try {
      const res = await api.onboarding.complete({
        wh_api_key:        whKey,
        wh_property_id:    whPropId,
        wa_access_token:   waToken,
        wa_phone_number_id: waPid,
        property_name:     propName,
        property_location: propLocation,
        max_guests:        maxGuests,
        bedrooms,
        base_rate:         baseRate ? parseFloat(baseRate) : null,
        amenities,
      });
      login(res.access_token);
      setStep(4);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Setup failed");
    } finally {
      setSubmitting(false);
    }
  }

  const apiUrl = typeof window !== "undefined"
    ? `${window.location.origin}/api/webhook/webhotelier`
    : "https://owners.vayancy.gr/api/webhook/webhotelier";

  const waWebhookUrl = typeof window !== "undefined"
    ? `${window.location.origin}/api/webhook/whatsapp`
    : "https://owners.vayancy.gr/api/webhook/whatsapp";

  if (isLoading) return (
    <div className="min-h-screen bg-vayancy-bg flex items-center justify-center">
      <div className="w-5 h-5 border-2 border-vayancy-accent border-t-transparent rounded-full animate-spin" />
    </div>
  );

  // ── Step progress ─────────────────────────────────────────────────────────
  const steps = [
    { n: 1, label: "WebHotelier" },
    { n: 2, label: "WhatsApp" },
    { n: 3, label: "Your property" },
    { n: 4, label: "Ready" },
  ];

  return (
    <div className="min-h-screen bg-vayancy-bg flex flex-col items-center justify-center px-4 py-10">
      <div className="w-full max-w-lg">

        {/* Logo */}
        <div className="text-center mb-8">
          <p className="text-vayancy-accent font-semibold tracking-widest text-sm">VAYANCY</p>
        </div>

        {/* Progress steps */}
        {step < 4 && (
          <div className="flex items-center justify-center gap-0 mb-8">
            {steps.slice(0, 3).map((s, i) => (
              <div key={s.n} className="flex items-center">
                <div className="flex flex-col items-center">
                  <div className={`w-7 h-7 rounded-full flex items-center justify-center text-xs font-medium border transition-colors ${
                    step > s.n
                      ? "bg-vayancy-green border-vayancy-green text-vayancy-bg"
                      : step === s.n
                      ? "bg-vayancy-accent border-vayancy-accent text-vayancy-bg"
                      : "bg-vayancy-surface border-vayancy-border text-vayancy-dim"
                  }`}>
                    {step > s.n ? (
                      <svg width="12" height="12" viewBox="0 0 12 12" fill="none">
                        <path d="M2 6l3 3 5-5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/>
                      </svg>
                    ) : s.n}
                  </div>
                  <span className={`text-xs mt-1 ${step === s.n ? "text-vayancy-text" : "text-vayancy-dim"}`}>
                    {s.label}
                  </span>
                </div>
                {i < 2 && (
                  <div className={`w-16 h-px mx-2 mb-4 ${step > s.n ? "bg-vayancy-green" : "bg-vayancy-border"}`} />
                )}
              </div>
            ))}
          </div>
        )}

        {/* Card */}
        <div className="bg-vayancy-surface border border-vayancy-border rounded-xl p-7">

          {error && (
            <div className="bg-red-950 border border-red-800 text-red-300 rounded-lg px-4 py-3 text-sm mb-5">
              {error}
            </div>
          )}

          {/* ── Step 1: WebHotelier ─────────────────────────────────────────── */}
          {step === 1 && (
            <>
              <h2 className="text-base font-medium text-vayancy-text mb-1">Connect WebHotelier</h2>
              <p className="text-sm text-vayancy-dim mb-5">
                This is how Vayancy reads your reservations and manages availability.
              </p>

              <div className="space-y-4 mb-5">
                <div>
                  <label className="block text-xs text-vayancy-dim mb-1.5">API Key</label>
                  <input
                    type="password"
                    value={whKey}
                    onChange={e => { setWhKey(e.target.value); setWhOk(null); }}
                    placeholder="wh_live_..."
                    className="w-full bg-vayancy-bg border border-vayancy-border rounded-lg px-3 py-2.5 text-sm text-vayancy-text placeholder-vayancy-border focus:outline-none focus:border-vayancy-accent"
                  />
                  <p className="text-xs text-vayancy-dim mt-1">
                    Found in WebHotelier portal → Settings → API Access
                  </p>
                </div>
                <div>
                  <label className="block text-xs text-vayancy-dim mb-1.5">Property ID</label>
                  <input
                    type="text"
                    value={whPropId}
                    onChange={e => { setWhPropId(e.target.value); setWhOk(null); }}
                    placeholder="12345"
                    className="w-full bg-vayancy-bg border border-vayancy-border rounded-lg px-3 py-2.5 text-sm text-vayancy-text placeholder-vayancy-border focus:outline-none focus:border-vayancy-accent"
                  />
                </div>
              </div>

              <div className="flex items-center gap-3 mb-5">
                <button
                  onClick={testWebHotelier}
                  disabled={!whKey || !whPropId || whTesting}
                  className="border border-vayancy-border text-vayancy-dim text-sm px-4 py-2 rounded-lg hover:border-vayancy-accent hover:text-vayancy-accent disabled:opacity-40 transition-colors"
                >
                  Test connection
                </button>
                <StatusPill ok={whOk} testing={whTesting} />
              </div>

              {/* Webhook guide */}
              <div className="bg-vayancy-bg border border-vayancy-border rounded-lg p-4 mb-5">
                <p className="text-xs font-medium text-vayancy-text mb-2">
                  Set this webhook URL in WebHotelier portal
                </p>
                <CodeBlock>{apiUrl}</CodeBlock>
                <p className="text-xs text-vayancy-dim mt-2">
                  Go to WebHotelier → Settings → Webhooks → Add webhook URL → paste above.
                  This is how new bookings reach your AI agents instantly.
                </p>
              </div>

              <button
                onClick={() => { setError(""); setStep(2); }}
                disabled={!whOk}
                className="w-full bg-vayancy-accent text-vayancy-bg font-medium text-sm rounded-lg py-2.5 disabled:opacity-40 transition-opacity"
              >
                Continue →
              </button>
              {!whOk && (
                <p className="text-center text-xs text-vayancy-dim mt-2">
                  Test the connection before continuing
                </p>
              )}
            </>
          )}

          {/* ── Step 2: WhatsApp ─────────────────────────────────────────────── */}
          {step === 2 && (
            <>
              <h2 className="text-base font-medium text-vayancy-text mb-1">Connect WhatsApp Business</h2>
              <p className="text-sm text-vayancy-dim mb-5">
                Your AI guest agent sends and receives messages through your WhatsApp Business number.
              </p>

              <div className="space-y-4 mb-5">
                <div>
                  <label className="block text-xs text-vayancy-dim mb-1.5">Access Token</label>
                  <input
                    type="password"
                    value={waToken}
                    onChange={e => { setWaToken(e.target.value); setWaOk(null); }}
                    placeholder="EAAxxxx..."
                    className="w-full bg-vayancy-bg border border-vayancy-border rounded-lg px-3 py-2.5 text-sm text-vayancy-text placeholder-vayancy-border focus:outline-none focus:border-vayancy-accent"
                  />
                  <p className="text-xs text-vayancy-dim mt-1">
                    Meta Developer Portal → Your App → WhatsApp → API Setup → Temporary access token
                  </p>
                </div>
                <div>
                  <label className="block text-xs text-vayancy-dim mb-1.5">Phone Number ID</label>
                  <input
                    type="text"
                    value={waPid}
                    onChange={e => { setWaPid(e.target.value); setWaOk(null); }}
                    placeholder="123456789012345"
                    className="w-full bg-vayancy-bg border border-vayancy-border rounded-lg px-3 py-2.5 text-sm text-vayancy-text placeholder-vayancy-border focus:outline-none focus:border-vayancy-accent"
                  />
                  <p className="text-xs text-vayancy-dim mt-1">
                    Same page → Phone Number ID (below the access token)
                  </p>
                </div>
              </div>

              <div className="flex items-center gap-3 mb-5">
                <button
                  onClick={testWhatsApp}
                  disabled={!waToken || !waPid || waTesting}
                  className="border border-vayancy-border text-vayancy-dim text-sm px-4 py-2 rounded-lg hover:border-vayancy-accent hover:text-vayancy-accent disabled:opacity-40 transition-colors"
                >
                  Test connection
                </button>
                <StatusPill ok={waOk} testing={waTesting} />
                {waOk && waPhone && (
                  <span className="text-xs text-vayancy-green">{waPhone}</span>
                )}
              </div>

              {/* Webhook guide */}
              <div className="bg-vayancy-bg border border-vayancy-border rounded-lg p-4 mb-5">
                <p className="text-xs font-medium text-vayancy-text mb-1">
                  Configure webhook in Meta Developer Portal
                </p>
                <p className="text-xs text-vayancy-dim mb-2">
                  App → WhatsApp → Configuration → Webhook URL:
                </p>
                <CodeBlock>{waWebhookUrl}</CodeBlock>
                <p className="text-xs text-vayancy-dim mt-2 mb-1">Verify token:</p>
                <CodeBlock>vayancy-verify</CodeBlock>
                <p className="text-xs text-vayancy-dim mt-2">
                  Subscribe to: <strong>messages</strong>
                </p>
              </div>

              <div className="flex gap-3">
                <button
                  onClick={() => setStep(1)}
                  className="flex-1 border border-vayancy-border text-vayancy-dim text-sm rounded-lg py-2.5 hover:border-vayancy-accent hover:text-vayancy-accent transition-colors"
                >
                  ← Back
                </button>
                <button
                  onClick={() => { setError(""); setStep(3); }}
                  disabled={!waOk}
                  className="flex-1 bg-vayancy-accent text-vayancy-bg font-medium text-sm rounded-lg py-2.5 disabled:opacity-40 transition-opacity"
                >
                  Continue →
                </button>
              </div>
              {!waOk && (
                <p className="text-center text-xs text-vayancy-dim mt-2">
                  Test the connection before continuing
                </p>
              )}
            </>
          )}

          {/* ── Step 3: Property ─────────────────────────────────────────────── */}
          {step === 3 && (
            <>
              <h2 className="text-base font-medium text-vayancy-text mb-1">Add your property</h2>
              <p className="text-sm text-vayancy-dim mb-5">
                This creates your listing in TravelOS — the AI booking catalog.
              </p>

              <div className="space-y-4 mb-5">
                <div>
                  <label className="block text-xs text-vayancy-dim mb-1.5">Property name</label>
                  <input
                    type="text"
                    value={propName}
                    onChange={e => setPropName(e.target.value)}
                    placeholder="Villa Azure"
                    className="w-full bg-vayancy-bg border border-vayancy-border rounded-lg px-3 py-2.5 text-sm text-vayancy-text placeholder-vayancy-border focus:outline-none focus:border-vayancy-accent"
                  />
                </div>

                <div>
                  <label className="block text-xs text-vayancy-dim mb-1.5">Location</label>
                  <input
                    type="text"
                    value={propLocation}
                    onChange={e => setPropLocation(e.target.value)}
                    placeholder="Mykonos"
                    className="w-full bg-vayancy-bg border border-vayancy-border rounded-lg px-3 py-2.5 text-sm text-vayancy-text placeholder-vayancy-border focus:outline-none focus:border-vayancy-accent"
                  />
                </div>

                <div className="grid grid-cols-2 gap-3">
                  <div>
                    <label className="block text-xs text-vayancy-dim mb-1.5">Max guests</label>
                    <select
                      value={maxGuests}
                      onChange={e => setMaxGuests(parseInt(e.target.value))}
                      className="w-full bg-vayancy-bg border border-vayancy-border rounded-lg px-3 py-2.5 text-sm text-vayancy-text focus:outline-none focus:border-vayancy-accent"
                    >
                      {[2,3,4,5,6,7,8,10,12].map(n => (
                        <option key={n} value={n}>{n} guests</option>
                      ))}
                    </select>
                  </div>
                  <div>
                    <label className="block text-xs text-vayancy-dim mb-1.5">Bedrooms</label>
                    <select
                      value={bedrooms}
                      onChange={e => setBedrooms(parseInt(e.target.value))}
                      className="w-full bg-vayancy-bg border border-vayancy-border rounded-lg px-3 py-2.5 text-sm text-vayancy-text focus:outline-none focus:border-vayancy-accent"
                    >
                      {[1,2,3,4,5,6].map(n => (
                        <option key={n} value={n}>{n} bed{n > 1 ? "s" : ""}</option>
                      ))}
                    </select>
                  </div>
                </div>

                <div>
                  <label className="block text-xs text-vayancy-dim mb-1.5">
                    Base rate (€/night) — optional
                  </label>
                  <input
                    type="number"
                    value={baseRate}
                    onChange={e => setBaseRate(e.target.value)}
                    placeholder="300"
                    min="0"
                    className="w-full bg-vayancy-bg border border-vayancy-border rounded-lg px-3 py-2.5 text-sm text-vayancy-text placeholder-vayancy-border focus:outline-none focus:border-vayancy-accent"
                  />
                </div>

                <div>
                  <label className="block text-xs text-vayancy-dim mb-2">Amenities</label>
                  <div className="flex flex-wrap gap-2">
                    {AMENITY_OPTIONS.map(a => (
                      <button
                        key={a}
                        type="button"
                        onClick={() => setAmenities(prev =>
                          prev.includes(a) ? prev.filter(x => x !== a) : [...prev, a]
                        )}
                        className={`text-xs px-3 py-1.5 rounded-full border transition-colors ${
                          amenities.includes(a)
                            ? "bg-vayancy-accent text-vayancy-bg border-vayancy-accent"
                            : "bg-vayancy-bg border-vayancy-border text-vayancy-dim hover:border-vayancy-accent hover:text-vayancy-accent"
                        }`}
                      >
                        {a.replace("_", " ")}
                      </button>
                    ))}
                  </div>
                </div>
              </div>

              <div className="flex gap-3">
                <button
                  onClick={() => setStep(2)}
                  className="flex-1 border border-vayancy-border text-vayancy-dim text-sm rounded-lg py-2.5 hover:border-vayancy-accent hover:text-vayancy-accent transition-colors"
                >
                  ← Back
                </button>
                <button
                  onClick={completeOnboarding}
                  disabled={!propName || !propLocation || submitting}
                  className="flex-1 bg-vayancy-accent text-vayancy-bg font-medium text-sm rounded-lg py-2.5 disabled:opacity-40 transition-opacity"
                >
                  {submitting ? "Setting up…" : "Go live →"}
                </button>
              </div>
            </>
          )}

          {/* ── Step 4: Success ───────────────────────────────────────────────── */}
          {step === 4 && (
            <div className="text-center py-4">
              <div className="w-16 h-16 bg-green-950 border border-green-800 rounded-full flex items-center justify-center mx-auto mb-5">
                <svg width="28" height="28" viewBox="0 0 28 28" fill="none">
                  <path d="M6 14l5.5 5.5L22 8" stroke="#4ade80" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/>
                </svg>
              </div>
              <h2 className="text-xl font-medium text-vayancy-text mb-2">
                Your villa is live.
              </h2>
              <p className="text-sm text-vayancy-dim mb-6 leading-relaxed">
                Your AI agents are active. New bookings from WebHotelier will be handled automatically.
                Guest messages are answered 24/7.
              </p>
              <div className="bg-vayancy-bg border border-vayancy-border rounded-lg p-4 text-left mb-6 space-y-2">
                <div className="flex items-start gap-2">
                  <span className="w-1.5 h-1.5 rounded-full bg-vayancy-green mt-1.5 flex-shrink-0" />
                  <p className="text-xs text-vayancy-dim">Guest agent answering WhatsApp messages</p>
                </div>
                <div className="flex items-start gap-2">
                  <span className="w-1.5 h-1.5 rounded-full bg-vayancy-green mt-1.5 flex-shrink-0" />
                  <p className="text-xs text-vayancy-dim">Revenue agent reviewing pricing every 4 hours</p>
                </div>
                <div className="flex items-start gap-2">
                  <span className="w-1.5 h-1.5 rounded-full bg-vayancy-green mt-1.5 flex-shrink-0" />
                  <p className="text-xs text-vayancy-dim">Operations agent dispatching on every checkout</p>
                </div>
                <div className="flex items-start gap-2">
                  <span className="w-1.5 h-1.5 rounded-full bg-vayancy-amber mt-1.5 flex-shrink-0" />
                  <p className="text-xs text-vayancy-dim">
                    Rate changes above 25% will ask for your approval first
                  </p>
                </div>
              </div>
              <button
                onClick={() => router.push("/dashboard")}
                className="w-full bg-vayancy-accent text-vayancy-bg font-medium text-sm rounded-lg py-2.5"
              >
                Open dashboard →
              </button>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
