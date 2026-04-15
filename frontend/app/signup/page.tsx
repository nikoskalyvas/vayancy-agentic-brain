"use client";

import { useState } from "react";
import Link from "next/link";
import { api } from "@/lib/api";

export default function SignupPage() {
  const [name,     setName]     = useState("");
  const [email,    setEmail]    = useState("");
  const [password, setPassword] = useState("");
  const [error,    setError]    = useState("");
  const [success,  setSuccess]  = useState(false);
  const [loading,  setLoading]  = useState(false);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    if (password.length < 8) {
      setError("Password must be at least 8 characters");
      return;
    }
    setLoading(true);
    try {
      await api.auth.signup(name.trim(), email.toLowerCase(), password);
      setSuccess(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Signup failed");
    } finally {
      setLoading(false);
    }
  }

  if (success) {
    return (
      <div className="min-h-screen bg-vayancy-bg flex flex-col items-center justify-center px-4">
        <div className="w-full max-w-sm text-center">
          <div className="w-14 h-14 bg-green-950 border border-green-800 rounded-full flex items-center justify-center mx-auto mb-5">
            <svg width="24" height="24" viewBox="0 0 24 24" fill="none">
              <path d="M5 12l5 5L19 7" stroke="#4ade80" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/>
            </svg>
          </div>
          <h1 className="text-xl font-medium text-vayancy-text mb-3">Check your inbox</h1>
          <p className="text-vayancy-dim text-sm leading-relaxed mb-6">
            We sent a verification link to <strong className="text-vayancy-text">{email}</strong>.
            Click it to activate your account and start setup.
          </p>
          <p className="text-xs text-vayancy-border">
            No email? Check your spam folder or{" "}
            <button
              onClick={() => { setSuccess(false); }}
              className="text-vayancy-accent hover:underline"
            >
              try again
            </button>
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-vayancy-bg flex flex-col items-center justify-center px-4">
      <div className="w-full max-w-sm">

        <div className="text-center mb-10">
          <p className="text-vayancy-accent font-semibold tracking-widest text-sm mb-1">
            VAYANCY
          </p>
          <p className="text-vayancy-dim text-sm">Owner portal</p>
        </div>

        <div className="bg-vayancy-surface border border-vayancy-border rounded-xl p-7">
          <h1 className="text-lg font-medium text-vayancy-text mb-2">Create your account</h1>
          <p className="text-sm text-vayancy-dim mb-6">
            Connect your villa and let the AI handle the rest.
          </p>

          {error && (
            <div className="bg-red-950 border border-red-800 text-red-300 rounded-lg px-4 py-3 text-sm mb-4">
              {error}
            </div>
          )}

          <form onSubmit={handleSubmit} className="space-y-4">
            <div>
              <label className="block text-xs text-vayancy-dim mb-1.5">Your name</label>
              <input
                type="text"
                value={name}
                onChange={e => setName(e.target.value)}
                placeholder="Nikos Papadopoulos"
                required
                className="w-full bg-vayancy-bg border border-vayancy-border rounded-lg px-3 py-2.5 text-sm text-vayancy-text placeholder-vayancy-border focus:outline-none focus:border-vayancy-accent transition-colors"
              />
            </div>

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
                placeholder="8+ characters"
                required
                className="w-full bg-vayancy-bg border border-vayancy-border rounded-lg px-3 py-2.5 text-sm text-vayancy-text placeholder-vayancy-border focus:outline-none focus:border-vayancy-accent transition-colors"
              />
            </div>

            <button
              type="submit"
              disabled={loading}
              className="w-full bg-vayancy-accent text-vayancy-bg font-medium text-sm rounded-lg py-2.5 mt-2 disabled:opacity-50 transition-opacity"
            >
              {loading ? "Creating account…" : "Create account"}
            </button>
          </form>
        </div>

        <p className="text-center text-sm text-vayancy-dim mt-5">
          Already have an account?{" "}
          <Link href="/login" className="text-vayancy-accent hover:underline">
            Sign in
          </Link>
        </p>
      </div>
    </div>
  );
}
