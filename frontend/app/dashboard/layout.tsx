"use client";

import { useEffect } from "react";
import { useRouter, usePathname } from "next/navigation";
import Link from "next/link";
import { useAuth } from "@/lib/auth";

function NavLink({
  href, label, icon,
}: {
  href: string; label: string; icon: React.ReactNode;
}) {
  const pathname = usePathname();
  const active   = pathname === href;
  return (
    <Link
      href={href}
      className={`flex items-center gap-2.5 px-3 py-2 rounded-lg text-sm transition-colors ${
        active
          ? "bg-vayancy-surface text-vayancy-accent"
          : "text-vayancy-dim hover:text-vayancy-text hover:bg-vayancy-surface"
      }`}
    >
      {icon}
      {label}
    </Link>
  );
}

export default function DashboardLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const { user, isLoading, logout } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (!isLoading && !user) router.replace("/login");
    if (!isLoading && user && !user.onboarding_complete) router.replace("/onboarding");
  }, [user, isLoading, router]);

  if (isLoading || !user) return (
    <div className="min-h-screen bg-vayancy-bg flex items-center justify-center">
      <div className="w-5 h-5 border-2 border-vayancy-accent border-t-transparent rounded-full animate-spin" />
    </div>
  );

  return (
    <div className="min-h-screen bg-vayancy-bg flex flex-col">

      {/* Top nav */}
      <header className="border-b border-vayancy-border px-4 sm:px-6 py-3 flex items-center justify-between flex-shrink-0">
        <div className="flex items-center gap-3">
          <span className="text-vayancy-accent font-semibold tracking-widest text-xs">
            VAYANCY
          </span>
          <span className="text-vayancy-border hidden sm:inline">|</span>
          <span className="text-vayancy-dim text-xs hidden sm:inline">
            {user.name}
          </span>
        </div>

        <nav className="flex items-center gap-1">
          <NavLink
            href="/dashboard"
            label="Overview"
            icon={
              <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
                <rect x="1" y="1" width="5" height="5" rx="1" stroke="currentColor" strokeWidth="1.2"/>
                <rect x="8" y="1" width="5" height="5" rx="1" stroke="currentColor" strokeWidth="1.2"/>
                <rect x="1" y="8" width="5" height="5" rx="1" stroke="currentColor" strokeWidth="1.2"/>
                <rect x="8" y="8" width="5" height="5" rx="1" stroke="currentColor" strokeWidth="1.2"/>
              </svg>
            }
          />
          <NavLink
            href="/dashboard/hitl"
            label="Decisions"
            icon={
              <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
                <circle cx="7" cy="7" r="5.5" stroke="currentColor" strokeWidth="1.2"/>
                <path d="M7 4v3.5l2 1" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round"/>
              </svg>
            }
          />
          <NavLink
            href="/dashboard/settings"
            label="Settings"
            icon={
              <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
                <circle cx="7" cy="7" r="2" stroke="currentColor" strokeWidth="1.2"/>
                <path d="M7 1v1.5M7 11.5V13M1 7h1.5M11.5 7H13" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round"/>
              </svg>
            }
          />
        </nav>

        <button
          onClick={logout}
          className="text-xs text-vayancy-dim hover:text-vayancy-text transition-colors"
        >
          Sign out
        </button>
      </header>

      {/* Page content */}
      <main className="flex-1 overflow-auto">
        {children}
      </main>
    </div>
  );
}
