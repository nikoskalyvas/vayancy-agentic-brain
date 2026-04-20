"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/lib/auth";
import { api } from "@/lib/api";

type Step = 1 | 2 | 3 | 4 | 5;

interface PropertyDraft {
  name:        string;
  location:    string;
  max_guests:  number;
  bedrooms:    number;
  base_rate:   string;
  amenities:   string[];
  sources:     Record<string, string>;
  completeness: number;
}

const AMENITY_OPTIONS = ["pool","sea_view","wifi","ac","bbq","parking","gym","beach_access","garden","concierge"];
const AMENITY_LABELS: Record<string, string> = {
  pool:"Pool", sea_view:"Sea view", wifi:"WiFi", ac:"AC", bbq:"BBQ",
  parking:"Parking", gym:"Gym", beach_access:"Beach access", garden:"Garden", concierge:"Concierge",
};

function SrcBadge({ src }: { src?: string }) {
  const map: Record<string,string> = { website:"website", google_places:"Google",
    booking_com:"Booking.com", webhotelier:"WebHotelier", claude_vision:"photos" };
  if (!src) return null;
  return <span className="ml-1.5 text-xs text-vayancy-green bg-green-950 border border-green-900 px-1.5 py-0.5 rounded-full">{map[src]||src}</span>;
}

function CopyBtn({ text }: { text: string }) {
  const [ok, setOk] = useState(false);
  return <button onClick={() => { navigator.clipboard.writeText(text); setOk(true); setTimeout(()=>setOk(false),1500); }}
    className="text-xs text-vayancy-accent border border-vayancy-border px-2 py-1 rounded">{ok?"Copied!":"Copy"}</button>;
}
function Code({ children }: { children: React.ReactNode }) {
  return <div className="flex items-center justify-between bg-vayancy-bg border border-vayancy-border rounded-lg px-3 py-2 mt-1.5">
    <code className="text-xs text-vayancy-green font-mono break-all flex-1 mr-2">{children}</code>
    <CopyBtn text={children} />
  </div>;
}

const empty = (): PropertyDraft => ({ name:"", location:"", max_guests:4, bedrooms:2, base_rate:"", amenities:[], sources:{}, completeness:0 });

export default function OnboardingPage() {
  const { user, login, isLoading } = useAuth();
  const router = useRouter();
  const [step, setStep] = useState<Step>(1);
  const [err, setErr] = useState("");

  const [whKey,    setWhKey]    = useState("");
  const [whProp,   setWhProp]   = useState("");
  const [whOk,     setWhOk]     = useState<boolean|null>(null);
  const [whBusy,   setWhBusy]   = useState(false);
  const [waToken,  setWaToken]  = useState("");
  const [waPid,    setWaPid]    = useState("");
  const [waOk,     setWaOk]     = useState<boolean|null>(null);
  const [waBusy,   setWaBusy]   = useState(false);
  const [waPhone,  setWaPhone]  = useState("");

  const [siteUrl,  setSiteUrl]  = useState("");
  const [bdcUrl,   setBdcUrl]   = useState("");
  const [hasBdc,   setHasBdc]   = useState<boolean|null>(null);
  const [enriching, setEnriching] = useState(false);
  const [draft,    setDraft]    = useState<PropertyDraft>(empty());

  const [photoFiles, setPhotoFiles] = useState<File[]>([]);
  const [photos64,   setPhotos64]   = useState<string[]>([]);
  const [analyzing,  setAnalyzing]  = useState(false);
  const [saving,     setSaving]     = useState(false);

  const apiBase = typeof window !== "undefined" ? window.location.origin + "/api" : "https://owners.vayancy.gr/api";
  const tok = () => localStorage.getItem("vayancy_token") || "";

  useEffect(() => {
    if (!isLoading && !user) router.replace("/login");
    if (!isLoading && user?.onboarding_complete) router.replace("/dashboard");
  }, [user, isLoading, router]);

  async function post(path: string, body: unknown) {
    const r = await fetch(`${apiBase}${path}`, {
      method: "POST",
      headers: { "Content-Type":"application/json", "Authorization":`Bearer ${tok()}` },
      body: JSON.stringify(body),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || d.message || "Request failed");
    return d;
  }

  async function testWH() {
    setWhBusy(true); setWhOk(null); setErr("");
    try { const r = await api.onboarding.testWebHotelier(whKey, whProp); setWhOk(r.success); if (!r.success) setErr(r.error||"Failed"); }
    catch (e) { setWhOk(false); setErr(e instanceof Error ? e.message : "Failed"); }
    finally { setWhBusy(false); }
  }

  async function testWA() {
    setWaBusy(true); setWaOk(null); setErr("");
    try { const r = await api.onboarding.testWhatsApp(waToken, waPid); setWaOk(r.success); if (r.success) setWaPhone(r.phone_number||""); else setErr(r.error||"Failed"); }
    catch (e) { setWaOk(false); setErr(e instanceof Error ? e.message : "Failed"); }
    finally { setWaBusy(false); }
  }

  async function enrich() {
    if (!siteUrl) return;
    setEnriching(true); setErr("");
    try {
      const d = await post("/auth/onboarding/enrich", { website_url: siteUrl, wh_api_key: whKey||null, wh_property_id: whProp||null });
      setDraft(p => ({ ...p,
        name: d.name||p.name, location: d.location||p.location,
        max_guests: d.max_guests||p.max_guests, bedrooms: d.bedrooms||p.bedrooms,
        base_rate: d.base_rate ? String(d.base_rate) : p.base_rate,
        amenities: d.amenities?.length ? d.amenities : p.amenities,
        sources: { ...p.sources, ...(d.sources||{}) },
        completeness: d.completeness||p.completeness,
      }));
    } catch (e) { setErr(e instanceof Error ? e.message : "Failed"); }
    finally { setEnriching(false); }
  }

  async function enrichBdc() {
    if (!bdcUrl) return;
    setEnriching(true); setErr("");
    try {
      const d = await post("/auth/onboarding/enrich-bdc", { bdc_url: bdcUrl });
      setDraft(p => ({ ...p,
        name: d.name||p.name, location: d.location||p.location,
        max_guests: d.max_guests||p.max_guests,
        amenities: [...new Set([...p.amenities, ...(d.amenities||[])])],
        sources: { ...p.sources, ...(d.sources||{}) },
        completeness: Math.max(d.completeness||0, p.completeness),
      }));
    } catch (e) { setErr(e instanceof Error ? e.message : "Failed"); }
    finally { setEnriching(false); }
  }

  async function handleFiles(e: React.ChangeEvent<HTMLInputElement>) {
    const files = Array.from(e.target.files||[]).slice(0,4);
    setPhotoFiles(files);
    const b64s: string[] = [];
    for (const f of files) {
      const b64 = await new Promise<string>(res => { const r2 = new FileReader(); r2.onload = () => res((r2.result as string).split(",")[1]); r2.readAsDataURL(f); });
      b64s.push(b64);
    }
    setPhotos64(b64s);
  }

  async function analyzePhotos() {
    setAnalyzing(true); setErr("");
    try {
      const d = await post("/auth/onboarding/enrich-photos", { photos_b64: photos64 });
      setDraft(p => ({ ...p,
        bedrooms: d.bedrooms||p.bedrooms, max_guests: d.max_guests||p.max_guests,
        amenities: [...new Set([...p.amenities, ...(d.amenities||[])])],
        sources: { ...p.sources, ...(d.sources||{}) },
        completeness: Math.max(d.completeness||0, p.completeness),
      }));
      setStep(4);
    } catch (e) { setErr(e instanceof Error ? e.message : "Failed"); }
    finally { setAnalyzing(false); }
  }

  async function save() {
    if (!draft.name || !draft.location) { setErr("Name and location required"); return; }
    setSaving(true); setErr("");
    try {
      const r = await api.onboarding.complete({
        wh_api_key: whKey, wh_property_id: whProp,
        wa_access_token: waToken, wa_phone_number_id: waPid,
        property_name: draft.name, property_location: draft.location,
        max_guests: draft.max_guests||4, bedrooms: draft.bedrooms||1,
        base_rate: draft.base_rate ? parseFloat(draft.base_rate) : null,
        amenities: draft.amenities,
      });
      login(r.access_token);
      setStep(5);
    } catch (e) { setErr(e instanceof Error ? e.message : "Failed"); }
    finally { setSaving(false); }
  }

  if (isLoading) return <div className="min-h-screen bg-vayancy-bg flex items-center justify-center"><div className="w-5 h-5 border-2 border-vayancy-accent border-t-transparent rounded-full animate-spin"/></div>;

  const stepLabels = ["Connections","Property","Photos","Confirm"];

  return (
    <div className="min-h-screen bg-vayancy-bg flex flex-col items-center justify-center px-4 py-10">
      <div className="w-full max-w-lg">
        <div className="text-center mb-8"><p className="text-vayancy-accent font-semibold tracking-widest text-sm">VAYANCY</p></div>

        {step < 5 && (
          <div className="flex items-center justify-center mb-8 gap-1">
            {stepLabels.map((label, i) => {
              const n = (i+1) as Step;
              return <div key={n} className="flex items-center">
                <div className="flex flex-col items-center">
                  <div className={`w-7 h-7 rounded-full flex items-center justify-center text-xs font-medium border ${step>n?"bg-vayancy-green border-vayancy-green text-vayancy-bg":step===n?"bg-vayancy-accent border-vayancy-accent text-vayancy-bg":"bg-vayancy-surface border-vayancy-border text-vayancy-dim"}`}>
                    {step > n ? "✓" : n}
                  </div>
                  <span className={`text-xs mt-1 ${step===n?"text-vayancy-text":"text-vayancy-dim"}`}>{label}</span>
                </div>
                {i < 3 && <div className={`w-10 h-px mx-1 mb-4 ${step>n?"bg-vayancy-green":"bg-vayancy-border"}`}/>}
              </div>;
            })}
          </div>
        )}

        <div className="bg-vayancy-surface border border-vayancy-border rounded-xl p-7">
          {err && <div className="bg-red-950 border border-red-800 text-red-300 rounded-lg px-4 py-3 text-sm mb-5">{err}</div>}

          {step === 1 && <>
            <h2 className="text-base font-medium text-vayancy-text mb-1">Connect your tools</h2>
            <p className="text-sm text-vayancy-dim mb-5">PMS and WhatsApp. We collect property details automatically next.</p>

            <p className="text-xs font-medium text-vayancy-text mb-2">WebHotelier</p>
            <label className="block text-xs text-vayancy-dim mb-1.5">API key <span className="opacity-60">— WebHotelier → Settings → API Access</span></label>
            <input type="password" value={whKey} onChange={e=>{setWhKey(e.target.value);setWhOk(null);}} placeholder="wh_live_..."
              className="w-full bg-vayancy-bg border border-vayancy-border rounded-lg px-3 py-2.5 text-sm text-vayancy-text placeholder-vayancy-border focus:outline-none focus:border-vayancy-accent mb-2"/>
            <label className="block text-xs text-vayancy-dim mb-1.5">Property ID <span className="opacity-60">— in the URL when viewing your property</span></label>
            <input type="text" value={whProp} onChange={e=>{setWhProp(e.target.value);setWhOk(null);}} placeholder="12345"
              className="w-full bg-vayancy-bg border border-vayancy-border rounded-lg px-3 py-2.5 text-sm text-vayancy-text placeholder-vayancy-border focus:outline-none focus:border-vayancy-accent mb-3"/>
            <div className="flex items-center gap-3 mb-3">
              <button onClick={testWH} disabled={!whKey||!whProp||whBusy} className="border border-vayancy-border text-vayancy-dim text-xs px-3 py-2 rounded-lg hover:border-vayancy-accent disabled:opacity-40">{whBusy?"Testing…":"Test connection"}</button>
              {whOk===true && <span className="text-xs text-vayancy-green flex items-center gap-1.5"><span className="w-1.5 h-1.5 rounded-full bg-vayancy-green"/>Connected</span>}
              {whOk===false && <span className="text-xs text-red-400">Failed — check credentials</span>}
            </div>
            <div className="bg-vayancy-bg border border-vayancy-border rounded-lg p-3 mb-5">
              <p className="text-xs text-vayancy-dim mb-1">Webhook URL to set in WebHotelier → Settings → Webhooks:</p>
              <Code>{apiBase.replace("/api","")}/api/webhook/webhotelier</Code>
            </div>

            <div className="border-t border-vayancy-border pt-5 mb-5">
              <p className="text-xs font-medium text-vayancy-text mb-2">WhatsApp Business</p>
              <label className="block text-xs text-vayancy-dim mb-1.5">Access token <span className="opacity-60">— Meta Developer → App → WhatsApp → API Setup</span></label>
              <input type="password" value={waToken} onChange={e=>{setWaToken(e.target.value);setWaOk(null);}} placeholder="EAAxxxx..."
                className="w-full bg-vayancy-bg border border-vayancy-border rounded-lg px-3 py-2.5 text-sm text-vayancy-text placeholder-vayancy-border focus:outline-none focus:border-vayancy-accent mb-2"/>
              <label className="block text-xs text-vayancy-dim mb-1.5">Phone number ID</label>
              <input type="text" value={waPid} onChange={e=>{setWaPid(e.target.value);setWaOk(null);}} placeholder="123456789012345"
                className="w-full bg-vayancy-bg border border-vayancy-border rounded-lg px-3 py-2.5 text-sm text-vayancy-text placeholder-vayancy-border focus:outline-none focus:border-vayancy-accent mb-3"/>
              <div className="flex items-center gap-3 mb-3">
                <button onClick={testWA} disabled={!waToken||!waPid||waBusy} className="border border-vayancy-border text-vayancy-dim text-xs px-3 py-2 rounded-lg hover:border-vayancy-accent disabled:opacity-40">{waBusy?"Testing…":"Test connection"}</button>
                {waOk===true && <span className="text-xs text-vayancy-green flex items-center gap-1.5"><span className="w-1.5 h-1.5 rounded-full bg-vayancy-green"/>{waPhone||"Connected"}</span>}
                {waOk===false && <span className="text-xs text-red-400">Failed</span>}
              </div>
              <div className="bg-vayancy-bg border border-vayancy-border rounded-lg p-3 space-y-2">
                <p className="text-xs text-vayancy-dim">Meta → App → WhatsApp → Configuration:</p>
                <div><p className="text-xs text-vayancy-dim mb-0.5">Webhook URL:</p><Code>{apiBase.replace("/api","")}/api/webhook/whatsapp</Code></div>
                <div><p className="text-xs text-vayancy-dim mb-0.5">Verify token:</p><Code>vayancy-verify</Code></div>
              </div>
            </div>

            <button onClick={()=>{setErr("");setStep(2);}} disabled={!whOk||!waOk} className="w-full bg-vayancy-accent text-vayancy-bg font-medium text-sm rounded-lg py-2.5 disabled:opacity-40">Continue →</button>
            {(!whOk||!waOk)&&<p className="text-center text-xs text-vayancy-dim mt-2">Test both connections first</p>}
          </>}

          {step === 2 && <>
            <h2 className="text-base font-medium text-vayancy-text mb-1">Your property</h2>
            <p className="text-sm text-vayancy-dim mb-4">Paste your website URL and we fill everything automatically.</p>

            {draft.completeness > 0 && (
              <div className="mb-4">
                <div className="flex justify-between mb-1"><span className="text-xs text-vayancy-dim">Auto-filled</span><span className="text-xs text-vayancy-text">{draft.completeness}%</span></div>
                <div className="h-1 bg-vayancy-border rounded-full overflow-hidden"><div className="h-full bg-vayancy-accent rounded-full transition-all" style={{width:`${draft.completeness}%`}}/></div>
              </div>
            )}

            <div className="mb-4">
              <label className="block text-xs text-vayancy-dim mb-1.5">Website URL</label>
              <div className="flex gap-2">
                <input type="url" value={siteUrl} onChange={e=>setSiteUrl(e.target.value)} placeholder="https://villaazure.gr"
                  className="flex-1 bg-vayancy-bg border border-vayancy-border rounded-lg px-3 py-2.5 text-sm text-vayancy-text placeholder-vayancy-border focus:outline-none focus:border-vayancy-accent"/>
                <button onClick={enrich} disabled={!siteUrl||enriching} className="bg-vayancy-accent text-vayancy-bg text-xs font-medium px-4 rounded-lg disabled:opacity-40 whitespace-nowrap">
                  {enriching?"Fetching…":"Auto-fill"}
                </button>
              </div>
              {draft.name && <p className="text-xs text-vayancy-green mt-1.5 flex items-center gap-1.5"><span className="w-1.5 h-1.5 rounded-full bg-vayancy-green"/>Found: {draft.name}{draft.location?` · ${draft.location}`:""}{draft.amenities.length>0?` · ${draft.amenities.length} amenities`:""}</p>}
            </div>

            <div className="mb-4">
              <p className="text-xs text-vayancy-dim mb-2">On Booking.com?</p>
              <div className="flex gap-2 mb-2">
                {[true,false].map(v=><button key={String(v)} onClick={()=>setHasBdc(v)} className={`flex-1 text-xs py-2 rounded-lg border transition-colors ${hasBdc===v?"bg-vayancy-accent text-vayancy-bg border-vayancy-accent":"border-vayancy-border text-vayancy-dim hover:border-vayancy-accent"}`}>{v?"Yes":"No"}</button>)}
              </div>
              {hasBdc && <div className="flex gap-2">
                <input type="url" value={bdcUrl} onChange={e=>setBdcUrl(e.target.value)} placeholder="https://www.booking.com/hotel/gr/..."
                  className="flex-1 bg-vayancy-bg border border-vayancy-border rounded-lg px-3 py-2 text-sm text-vayancy-text placeholder-vayancy-border focus:outline-none focus:border-vayancy-accent"/>
                <button onClick={enrichBdc} disabled={!bdcUrl||enriching} className="bg-vayancy-surface border border-vayancy-border text-vayancy-dim text-xs px-3 rounded-lg hover:border-vayancy-accent disabled:opacity-40 whitespace-nowrap">{enriching?"…":"Fill gaps"}</button>
              </div>}
            </div>

            <div className="flex gap-3">
              <button onClick={()=>setStep(1)} className="flex-1 border border-vayancy-border text-vayancy-dim text-sm rounded-lg py-2.5 hover:border-vayancy-accent">← Back</button>
              <button onClick={()=>{setErr("");setStep(3);}} className="flex-1 bg-vayancy-accent text-vayancy-bg font-medium text-sm rounded-lg py-2.5">{draft.completeness>60?"Looks good →":"Continue →"}</button>
            </div>
          </>}

          {step === 3 && <>
            <h2 className="text-base font-medium text-vayancy-text mb-1">Upload photos</h2>
            <p className="text-sm text-vayancy-dim mb-2">AI analyses them to spot bedrooms, pool, sea views, and more.</p>
            <p className="text-xs text-vayancy-dim mb-5">Optional — skip if you prefer to fill manually.</p>

            {draft.completeness > 0 && (
              <div className="mb-4">
                <div className="flex justify-between mb-1"><span className="text-xs text-vayancy-dim">Auto-filled</span><span className="text-xs text-vayancy-text">{draft.completeness}%</span></div>
                <div className="h-1 bg-vayancy-border rounded-full overflow-hidden"><div className="h-full bg-vayancy-accent rounded-full transition-all" style={{width:`${draft.completeness}%`}}/></div>
              </div>
            )}

            <div className="border-2 border-dashed border-vayancy-border rounded-xl p-6 text-center mb-4">
              <input type="file" accept="image/*" multiple onChange={handleFiles} id="photos" className="hidden"/>
              <label htmlFor="photos" className="cursor-pointer">
                <p className="text-sm text-vayancy-dim">Click to upload up to 4 photos</p>
                <p className="text-xs text-vayancy-dim mt-1">JPG, PNG, WebP</p>
              </label>
            </div>
            {photoFiles.length > 0 && <p className="text-xs text-vayancy-green mb-4 flex items-center gap-1.5"><span className="w-1.5 h-1.5 rounded-full bg-vayancy-green"/>{photoFiles.length} photo{photoFiles.length>1?"s":""} ready to analyse</p>}

            <div className="flex gap-3">
              <button onClick={()=>setStep(2)} className="flex-1 border border-vayancy-border text-vayancy-dim text-sm rounded-lg py-2.5 hover:border-vayancy-accent">← Back</button>
              {photos64.length > 0
                ? <button onClick={analyzePhotos} disabled={analyzing} className="flex-1 bg-vayancy-accent text-vayancy-bg font-medium text-sm rounded-lg py-2.5 disabled:opacity-40">{analyzing?"Analysing…":"Analyse →"}</button>
                : <button onClick={()=>{setErr("");setStep(4);}} className="flex-1 bg-vayancy-surface border border-vayancy-border text-vayancy-dim text-sm rounded-lg py-2.5 hover:border-vayancy-accent">Skip →</button>
              }
            </div>
          </>}

          {step === 4 && <>
            <h2 className="text-base font-medium text-vayancy-text mb-1">Confirm your details</h2>
            <p className="text-sm text-vayancy-dim mb-4">We filled these automatically. Fix anything wrong.</p>

            {draft.completeness > 0 && (
              <div className="mb-4">
                <div className="flex justify-between mb-1"><span className="text-xs text-vayancy-dim">Auto-filled</span><span className="text-xs text-vayancy-text">{draft.completeness}%</span></div>
                <div className="h-1 bg-vayancy-border rounded-full overflow-hidden"><div className="h-full bg-vayancy-accent rounded-full transition-all" style={{width:`${draft.completeness}%`}}/></div>
              </div>
            )}

            <div className="space-y-3 mb-5">
              <div>
                <label className="block text-xs text-vayancy-dim mb-1">Name <SrcBadge src={draft.sources.name}/></label>
                <input type="text" value={draft.name} onChange={e=>setDraft(p=>({...p,name:e.target.value}))}
                  className="w-full bg-vayancy-bg border border-vayancy-border rounded-lg px-3 py-2.5 text-sm text-vayancy-text focus:outline-none focus:border-vayancy-accent"/>
              </div>
              <div>
                <label className="block text-xs text-vayancy-dim mb-1">Location <SrcBadge src={draft.sources.location}/></label>
                <input type="text" value={draft.location} onChange={e=>setDraft(p=>({...p,location:e.target.value}))} placeholder="Mykonos"
                  className="w-full bg-vayancy-bg border border-vayancy-border rounded-lg px-3 py-2.5 text-sm text-vayancy-text focus:outline-none focus:border-vayancy-accent"/>
              </div>
              <div className="grid grid-cols-3 gap-2">
                <div>
                  <label className="block text-xs text-vayancy-dim mb-1">Guests <SrcBadge src={draft.sources.max_guests}/></label>
                  <select value={draft.max_guests} onChange={e=>setDraft(p=>({...p,max_guests:parseInt(e.target.value)}))}
                    className="w-full bg-vayancy-bg border border-vayancy-border rounded-lg px-2 py-2.5 text-sm text-vayancy-text focus:outline-none focus:border-vayancy-accent">
                    {[2,3,4,5,6,7,8,10,12].map(n=><option key={n} value={n}>{n}</option>)}
                  </select>
                </div>
                <div>
                  <label className="block text-xs text-vayancy-dim mb-1">Beds <SrcBadge src={draft.sources.bedrooms}/></label>
                  <select value={draft.bedrooms} onChange={e=>setDraft(p=>({...p,bedrooms:parseInt(e.target.value)}))}
                    className="w-full bg-vayancy-bg border border-vayancy-border rounded-lg px-2 py-2.5 text-sm text-vayancy-text focus:outline-none focus:border-vayancy-accent">
                    {[1,2,3,4,5,6].map(n=><option key={n} value={n}>{n}</option>)}
                  </select>
                </div>
                <div>
                  <label className="block text-xs text-vayancy-dim mb-1">Rate €/night</label>
                  <input type="number" value={draft.base_rate} onChange={e=>setDraft(p=>({...p,base_rate:e.target.value}))} placeholder="350"
                    className="w-full bg-vayancy-bg border border-vayancy-border rounded-lg px-2 py-2.5 text-sm text-vayancy-text focus:outline-none focus:border-vayancy-accent"/>
                </div>
              </div>
              <div>
                <label className="block text-xs text-vayancy-dim mb-2">Amenities {draft.amenities.length>0&&<SrcBadge src={Object.values(draft.sources)[0]}/>}</label>
                <div className="flex flex-wrap gap-2">
                  {AMENITY_OPTIONS.map(a=>(
                    <button key={a} type="button"
                      onClick={()=>setDraft(p=>({...p,amenities:p.amenities.includes(a)?p.amenities.filter(x=>x!==a):[...p.amenities,a]}))}
                      className={`text-xs px-3 py-1.5 rounded-full border transition-colors ${draft.amenities.includes(a)?"bg-vayancy-accent text-vayancy-bg border-vayancy-accent":"bg-vayancy-bg border-vayancy-border text-vayancy-dim hover:border-vayancy-accent"}`}>
                      {AMENITY_LABELS[a]}
                    </button>
                  ))}
                </div>
              </div>
            </div>

            <div className="flex gap-3">
              <button onClick={()=>setStep(3)} className="flex-1 border border-vayancy-border text-vayancy-dim text-sm rounded-lg py-2.5 hover:border-vayancy-accent">← Back</button>
              <button onClick={save} disabled={!draft.name||!draft.location||saving} className="flex-1 bg-vayancy-accent text-vayancy-bg font-medium text-sm rounded-lg py-2.5 disabled:opacity-40">
                {saving?"Setting up…":"Go live →"}
              </button>
            </div>
          </>}

          {step === 5 && (
            <div className="text-center py-4">
              <div className="w-16 h-16 bg-green-950 border border-green-800 rounded-full flex items-center justify-center mx-auto mb-5">
                <svg width="28" height="28" viewBox="0 0 28 28" fill="none"><path d="M6 14l5.5 5.5L22 8" stroke="#4ade80" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/></svg>
              </div>
              <h2 className="text-xl font-medium text-vayancy-text mb-2">Your villa is live.</h2>
              <p className="text-sm text-vayancy-dim mb-6 leading-relaxed">Three AI agents are now active. Bookings are handled automatically.</p>
              <div className="bg-vayancy-bg border border-vayancy-border rounded-lg p-4 text-left mb-6 space-y-2">
                {["Guest agent answering WhatsApp messages","Revenue agent reviewing pricing every 4 hours","Operations agent dispatching on every checkout"].map((item,i)=>(
                  <div key={i} className="flex items-start gap-2"><span className="w-1.5 h-1.5 rounded-full bg-vayancy-green mt-1.5 flex-shrink-0"/><p className="text-xs text-vayancy-dim">{item}</p></div>
                ))}
              </div>
              <button onClick={()=>router.push("/dashboard")} className="w-full bg-vayancy-accent text-vayancy-bg font-medium text-sm rounded-lg py-2.5">Open dashboard →</button>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
