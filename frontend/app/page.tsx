import WorkflowMatrix from "@/components/WorkflowMatrix";
import StatCard from "@/components/StatCard";

const PROPERTY_ID = process.env.NEXT_PUBLIC_PROPERTY_ID ?? "default";

export default function Dashboard() {
  return (
    <div className="min-h-screen bg-vayancy-bg">

      {/* Top nav */}
      <header className="border-b border-vayancy-border px-6 py-4 flex items-center justify-between">
        <div className="flex items-center gap-3">
          <span className="text-vayancy-accent font-semibold tracking-wide">
            VAYANCY
          </span>
          <span className="text-vayancy-border">│</span>
          <span className="text-vayancy-dim text-sm">Owner Dashboard</span>
        </div>
        <div className="flex items-center gap-2">
          <span className="text-xs text-vayancy-dim font-mono">
            {PROPERTY_ID}
          </span>
        </div>
      </header>

      <main className="max-w-6xl mx-auto px-6 py-8 space-y-8">

        {/* Hero */}
        <section>
          <h1 className="text-2xl font-light text-vayancy-text mb-1">
            Agentic Brain
          </h1>
          <p className="text-vayancy-dim text-sm">
            Autonomous processing in real-time
          </p>
        </section>

        {/* KPIs */}
        <section className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <StatCard
            label="Response time"
            value="< 2 min"
            sub="24/7 multilingual"
            accent
          />
          <StatCard
            label="Automation rate"
            value="94%"
            sub="Of daily inquiries"
          />
          <StatCard
            label="Active agents"
            value="3"
            sub="Guest · Revenue · Ops"
          />
          <StatCard
            label="MCP servers"
            value="4"
            sub="WebHotelier · WA · PL · EN"
          />
        </section>

        {/* Live workflow matrix */}
        <section>
          <div className="flex items-center gap-2 mb-4">
            <h2 className="text-sm font-medium text-vayancy-text">
              Live Workflow Matrix
            </h2>
            <span className="text-vayancy-dim text-xs">
              — autonomous processing in real-time
            </span>
          </div>
          <WorkflowMatrix propertyId={PROPERTY_ID} />
        </section>

        {/* Integration status */}
        <section>
          <h2 className="text-sm font-medium text-vayancy-text mb-4">
            Integration Status
          </h2>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            {[
              { name: "WebHotelier",  desc: "PMS & Booking Source", port: 3001 },
              { name: "WhatsApp",     desc: "Guest Communications",  port: 3002 },
              { name: "PriceLabs",    desc: "Revenue Management",    port: 3003 },
              { name: "Epsilon Net",  desc: "ERP & Accounting",      port: 3004 },
            ].map((s) => (
              <div
                key={s.name}
                className="bg-vayancy-surface border border-vayancy-border
                           rounded-lg p-3 flex items-start gap-2.5"
              >
                <span className="mt-0.5 w-1.5 h-1.5 rounded-full bg-vayancy-green
                                 flex-shrink-0 animate-pulse-dot" />
                <div>
                  <p className="text-xs font-medium text-vayancy-text">{s.name}</p>
                  <p className="text-xs text-vayancy-dim">{s.desc}</p>
                  <p className="text-xs text-vayancy-border font-mono mt-0.5">
                    :{s.port}
                  </p>
                </div>
              </div>
            ))}
          </div>
        </section>

      </main>
    </div>
  );
}
