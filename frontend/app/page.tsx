"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/lib/auth";
import Link from "next/link";

export default function Root() {
  const { user, isLoading } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (isLoading) return;
    if (user?.onboarding_complete) {
      router.replace("/dashboard");
    }
  }, [user, isLoading, router]);

  if (isLoading) {
    return (
      <div style={{ minHeight: "100vh", background: "#0c0c0e", display: "flex", alignItems: "center", justifyContent: "center" }}>
        <div style={{ width: 20, height: 20, border: "1.5px solid #c8a96e", borderTopColor: "transparent", borderRadius: "50%", animation: "spin 0.8s linear infinite" }} />
        <style>{`@keyframes spin { to { transform: rotate(360deg); } }`}</style>
      </div>
    );
  }

  return (
    <>
      <style>{`
        @import url('https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,300;0,400;1,300;1,400&family=Jost:wght@300;400&display=swap');
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { background: #0c0c0e; }
        @keyframes spin { to { transform: rotate(360deg); } }
        @keyframes fadeUp {
          from { opacity: 0; transform: translateY(24px); }
          to   { opacity: 1; transform: translateY(0); }
        }
        .land-hero-title { animation: fadeUp 1s ease both; }
        .land-hero-sub   { animation: fadeUp 1s 0.2s ease both; }
        .land-cta        { animation: fadeUp 1s 0.4s ease both; }
        .land-features   { animation: fadeUp 1s 0.6s ease both; }
        .land-btn {
          display: inline-block;
          padding: 14px 44px;
          background: transparent;
          border: 1px solid #c8a96e;
          color: #c8a96e;
          font-family: 'Jost', sans-serif;
          font-size: 13px;
          font-weight: 300;
          letter-spacing: 0.18em;
          text-transform: uppercase;
          text-decoration: none;
          cursor: pointer;
          transition: background 0.3s, color 0.3s;
        }
        .land-btn:hover { background: #c8a96e; color: #0c0c0e; }
        .land-divider { width: 40px; height: 1px; background: #c8a96e; opacity: 0.5; margin: 0 auto 32px; }
        .land-feature-card {
          padding: 40px 32px;
          border: 1px solid rgba(200,169,110,0.15);
          flex: 1;
          min-width: 200px;
          transition: border-color 0.3s;
        }
        .land-feature-card:hover { border-color: rgba(200,169,110,0.4); }
        .land-feature-num {
          font-family: 'Cormorant Garamond', serif;
          font-size: 48px;
          font-weight: 300;
          color: rgba(200,169,110,0.25);
          line-height: 1;
          margin-bottom: 20px;
        }
        .land-feature-title {
          font-family: 'Jost', sans-serif;
          font-size: 12px;
          font-weight: 400;
          letter-spacing: 0.14em;
          text-transform: uppercase;
          color: #c8a96e;
          margin-bottom: 12px;
        }
        .land-feature-body {
          font-family: 'Jost', sans-serif;
          font-size: 14px;
          font-weight: 300;
          color: rgba(255,255,255,0.45);
          line-height: 1.7;
        }
      `}</style>

      <div style={{ minHeight: "100vh", background: "#0c0c0e", display: "flex", flexDirection: "column" }}>

        <nav style={{ padding: "28px 48px", display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <span style={{ fontFamily: "'Cormorant Garamond', serif", fontSize: 22, fontWeight: 300, letterSpacing: "0.12em", color: "#e8dcc8" }}>
            VAYANCY
          </span>
          <Link href="/login" className="land-btn" style={{ padding: "10px 28px", fontSize: 11 }}>
            Sign In
          </Link>
        </nav>

        <main style={{ flex: 1, display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", textAlign: "center", padding: "80px 24px 60px" }}>
          <div style={{ maxWidth: 720 }}>
            <p className="land-hero-sub" style={{ fontFamily: "'Jost', sans-serif", fontSize: 11, fontWeight: 300, letterSpacing: "0.22em", textTransform: "uppercase", color: "#c8a96e", marginBottom: 32 }}>
              Property Intelligence Platform
            </p>

            <h1 className="land-hero-title" style={{ fontFamily: "'Cormorant Garamond', serif", fontSize: "clamp(52px, 8vw, 96px)", fontWeight: 300, lineHeight: 1.05, color: "#e8dcc8", marginBottom: 32, letterSpacing: "-0.01em" }}>
              Your properties,<br />
              <em style={{ fontStyle: "italic", color: "#c8a96e" }}>fully in command.</em>
            </h1>

            <div className="land-divider" />

            <p className="land-hero-sub" style={{ fontFamily: "'Jost', sans-serif", fontSize: 16, fontWeight: 300, color: "rgba(255,255,255,0.45)", lineHeight: 1.8, marginBottom: 52, maxWidth: 480, margin: "0 auto 52px" }}>
              AI-powered revenue, guest communication, and operations — built for Mediterranean property owners.
            </p>

            <div className="land-cta">
              <Link href="/login" className="land-btn">
                Access Dashboard
              </Link>
            </div>
          </div>
        </main>

        <section className="land-features" style={{ padding: "60px 48px 80px", maxWidth: 1100, margin: "0 auto", width: "100%" }}>
          <div style={{ display: "flex", gap: 1, flexWrap: "wrap" }}>
            {[
              { n: "01", title: "Revenue Intelligence", body: "Dynamic pricing, channel analysis, and yield optimisation informed by real-time market data." },
              { n: "02", title: "Guest Automation", body: "AI handles inquiries, check-in instructions, and upsells in your guests' language — 24/7." },
              { n: "03", title: "Unified Operations", body: "Bookings, maintenance, and financial reporting across all your properties in one place." },
            ].map(f => (
              <div key={f.n} className="land-feature-card">
                <div className="land-feature-num">{f.n}</div>
                <div className="land-feature-title">{f.title}</div>
                <div className="land-feature-body">{f.body}</div>
              </div>
            ))}
          </div>
        </section>

        <footer style={{ padding: "24px 48px", borderTop: "1px solid rgba(200,169,110,0.1)", display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <span style={{ fontFamily: "'Jost', sans-serif", fontSize: 12, fontWeight: 300, color: "rgba(255,255,255,0.25)", letterSpacing: "0.08em" }}>
            © 2026 Vayancy
          </span>
          <span style={{ fontFamily: "'Jost', sans-serif", fontSize: 12, fontWeight: 300, color: "rgba(255,255,255,0.25)", letterSpacing: "0.08em" }}>
            vayancy.gr
          </span>
        </footer>

      </div>
    </>
  );
}
