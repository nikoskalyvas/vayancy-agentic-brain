"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/lib/auth";

export default function Root() {
  const { user, isLoading } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (isLoading) return;
    if (!user) {
      router.replace("/login");
    } else if (!user.onboarding_complete) {
      router.replace("/onboarding");
    } else {
      router.replace("/dashboard");
    }
  }, [user, isLoading, router]);

  return (
    <div className="min-h-screen bg-vayancy-bg flex items-center justify-center">
      <div className="w-5 h-5 border-2 border-vayancy-accent border-t-transparent rounded-full animate-spin" />
    </div>
  );
}
