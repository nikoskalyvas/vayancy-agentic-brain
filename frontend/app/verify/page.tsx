"use client";

import { useEffect, useState, Suspense } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { useAuth } from "@/lib/auth";
import { api } from "@/lib/api";

function VerifyContent() {
  const searchParams = useSearchParams();
  const router       = useRouter();
  const { login }    = useAuth();
  const [status, setStatus] = useState<"loading" | "success" | "error">("loading");
  const [message, setMessage] = useState("");

  useEffect(() => {
    const token = searchParams.get("token");
    if (!token) {
      setStatus("error");
      setMessage("Invalid verification link — no token found.");
      return;
    }

    api.auth.verify(token)
      .then(res => {
        login(res.access_token);
        setStatus("success");
        setTimeout(() => {
          router.push(res.onboarding_complete ? "/dashboard" : "/onboarding");
        }, 1500);
      })
      .catch(err => {
        setStatus("error");
        setMessage(err instanceof Error ? err.message : "Verification failed");
      });
  }, [searchParams, login, router]);

  return (
    <div className="min-h-screen bg-vayancy-bg flex flex-col items-center justify-center px-4">
      <div className="w-full max-w-sm text-center">
        <p className="text-vayancy-accent font-semibold tracking-widest text-sm mb-8">
          VAYANCY
        </p>

        {status === "loading" && (
          <>
            <div className="w-8 h-8 border-2 border-vayancy-accent border-t-transparent rounded-full animate-spin mx-auto mb-4" />
            <p className="text-vayancy-dim text-sm">Verifying your email…</p>
          </>
        )}

        {status === "success" && (
          <>
            <div className="w-14 h-14 bg-green-950 border border-green-800 rounded-full flex items-center justify-center mx-auto mb-4">
              <svg width="24" height="24" viewBox="0 0 24 24" fill="none">
                <path d="M5 12l5 5L19 7" stroke="#4ade80" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/>
              </svg>
            </div>
            <h1 className="text-lg font-medium text-vayancy-text mb-2">Email verified</h1>
            <p className="text-vayancy-dim text-sm">Taking you to setup…</p>
          </>
        )}

        {status === "error" && (
          <>
            <div className="w-14 h-14 bg-red-950 border border-red-800 rounded-full flex items-center justify-center mx-auto mb-4">
              <svg width="24" height="24" viewBox="0 0 24 24" fill="none">
                <path d="M18 6L6 18M6 6l12 12" stroke="#f87171" strokeWidth="2" strokeLinecap="round"/>
              </svg>
            </div>
            <h1 className="text-lg font-medium text-vayancy-text mb-2">Verification failed</h1>
            <p className="text-vayancy-dim text-sm mb-5">{message}</p>
            <a href="/signup" className="text-vayancy-accent text-sm hover:underline">
              Back to sign up
            </a>
          </>
        )}
      </div>
    </div>
  );
}
