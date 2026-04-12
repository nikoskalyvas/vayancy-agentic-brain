interface Props {
  label:    string;
  value:    string | number;
  sub?:     string;
  accent?:  boolean;
}

export default function StatCard({ label, value, sub, accent }: Props) {
  return (
    <div className="bg-vayancy-surface border border-vayancy-border rounded-lg p-4">
      <p className="text-xs text-vayancy-dim font-mono uppercase tracking-wider mb-1">
        {label}
      </p>
      <p className={`text-2xl font-semibold ${accent ? "text-vayancy-accent" : "text-vayancy-text"}`}>
        {value}
      </p>
      {sub && (
        <p className="text-xs text-vayancy-dim mt-0.5">{sub}</p>
      )}
    </div>
  );
}
