"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { useAuth } from "@/lib/auth";
import { api } from "@/lib/api";

export default function LoginPage() {
  const { login } = useAuth();
  const router    = useRouter();

  const [email,    setEmail]    = useState("");
  const [password, setPassword] = useState("");
  const [error,    setError]    = useState("");
  const [loading,  setLoading]  = useState(false);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    setLoading(true);
    try {
      const res = await api.auth.login(email, password);
      login(res.access_token);
      router.push(res.onboarding_complete ? "/dashboard" : "/onboarding");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Login failed");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="min-h-screen bg-vayancy-bg flex flex-col items-center justify-center px-4">
      <div className="w-full max-w-sm">

        {/* Logo */}
        <div className="text-center mb-10">
          <p className="text-vayancy-accent font-semibold tracking-widest text-sm mb-1">
            VAYANCY
          </p>
          <p className="text-vayancy-dim text-sm">Owner portal</p>
        </div>

        {/* Card */}
        <div className="bg-vayancy-surface border border-vayancy-border rounded-xl p-7">
          <h1 className="text-lg font-medium text-vayancy-text mb-6">Sign in</h1>

          {error && (
            <div className="bg-red-950 border border-red-800 text-red-300 rounded-lg px-4 py-3 text-sm mb-4">
              {error}
            </div>
          )}

          <form onSubmit={handleSubmit} className="space-y-4">
            <div>
              <label className="block text-xs text-vayancy-dim mb-1.5">Email</label>
              <input
                type="email"
                value={email}
                onChange={e => setEmail(e.target.value)}
                placeholder="you@villa.gr"
                required
                className="w-full bg-vayancy-bg border border-vayancy-border rounded-lg px-3 py-2.5 text-sm text-vayancy-text placeholder-vayancy-border focus:outline-none focus:border-vayancy-accent transition-colors"
              />
            </div>

            <div>
              <label className="block text-xs text-vayancy-dim mb-1.5">Password</label>
              <input
                type="password"
                value={password}
                onChange={e => setPassword(e.target.value)}
                placeholder="••••••••"
                required
                className="w-full bg-vayancy-bg border border-vayancy-border rounded-lg px-3 py-2.5 text-sm text-vayancy-text placeholder-vayancy-border focus:outline-none focus:border-vayancy-accent transition-colors"
              />
            </div>

            <button
              type="submit"
              disabled={loading}
              className="w-full bg-vayancy-accent text-vayancy-bg font-medium text-sm rounded-lg py-2.5 mt-2 disabled:opacity-50 transition-opacity"
            >
              {loading ? "Signing in…" : "Sign in"}
            </button>
          </form>
        </div>

        <p className="text-center text-sm text-vayancy-dim mt-5">
          No account?{" "}
          <Link href="/signup" className="text-vayancy-accent hover:underline">
            Get started
          </Link>
        </p>
      </div>
    </div>
  );
}
