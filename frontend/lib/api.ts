/**
 * API client — frontend/lib/api.ts
 *
 * Typed wrappers around every backend endpoint the frontend needs.
 * All calls go through /api/* which the Next.js proxy forwards to the backend.
 *
 * Token is read from localStorage on every call so it's always fresh.
 * On 401, clears token and redirects to /login.
 */

const BASE = "/api";

function getToken(): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem("vayancy_token");
}

function clearAuth() {
  if (typeof window !== "undefined") {
    localStorage.removeItem("vayancy_token");
    localStorage.removeItem("vayancy_user");
  }
}

async function request<T>(
  path: string,
  options: RequestInit = {}
): Promise<T> {
  const token = getToken();
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(options.headers as Record<string, string>),
  };
  if (token) headers["Authorization"] = `Bearer ${token}`;

  const res = await fetch(`${BASE}${path}`, { ...options, headers });

  if (res.status === 401) {
    clearAuth();
    if (typeof window !== "undefined") window.location.href = "/login";
    throw new Error("Session expired");
  }

  const data = await res.json();
  if (!res.ok) throw new Error(data.detail || data.message || "Request failed");
  return data as T;
}

// ── Types ─────────────────────────────────────────────────────────────────────

export interface AuthUser {
  sub:             string;
  email:           string;
  name:            string;
  tenant_id:       string | null;
  tenant_api_key:  string | null;
  onboarding_complete: boolean;
}

export interface DashboardData {
  stats: {
    confirmed_bookings:    number;
    this_month:            number;
    unique_guests:         number;
    direct_bookings:       number;
    commission_saved_eur:  number;
  };
  recent_bookings: Booking[];
  pending_hitl:    HITLDecision[];
  recent_activity: ActivityEvent[];
}

export interface Booking {
  booking_id:    string;
  guest_name:    string;
  property_name: string;
  check_in:      string;
  check_out:     string;
  guests:        number;
  status:        string;
  channel:       string;
  booked_at:     string | null;
}

export interface HITLDecision {
  id:             string;
  action_type:    string;
  impact_summary: string;
  proposed:       Record<string, unknown>;
  created_at:     string;
  status?:        string;
  decision_note?: string;
  decided_at?:    string | null;
}

export interface ActivityEvent {
  agent:     string;
  action:    string;
  status:    string;
  timestamp: string;
}

export interface IntegrationStatus {
  webhotelier: { connected: boolean; property_id: string; status: string };
  whatsapp:    { connected: boolean; phone_number: string; status: string };
  ai_agents:   { connected: boolean; model: string; status: string };
  voyage_ai:   { connected: boolean; status: string };
}

// ── Auth ──────────────────────────────────────────────────────────────────────

export const api = {
  auth: {
    signup: (name: string, email: string, password: string) =>
      request<{ message: string; email: string }>("/auth/signup", {
        method: "POST",
        body: JSON.stringify({ name, email, password }),
      }),

    verify: (token: string) =>
      request<{ message: string; access_token: string; onboarding_complete: boolean }>(
        `/auth/verify?token=${token}`
      ),

    login: (email: string, password: string) =>
      request<{
        access_token: string;
        name: string;
        email: string;
        onboarding_complete: boolean;
      }>("/auth/login", {
        method: "POST",
        body: JSON.stringify({ email, password }),
      }),

    me: () => request<AuthUser>("/auth/me"),
  },

  onboarding: {
    testWebHotelier: (api_key: string, property_id: string) =>
      request<{ success: boolean; message?: string; error?: string }>(
        "/auth/onboarding/test-webhotelier",
        {
          method: "POST",
          body: JSON.stringify({ api_key, property_id }),
        }
      ),

    testWhatsApp: (access_token: string, phone_number_id: string) =>
      request<{
        success: boolean;
        phone_number?: string;
        name?: string;
        message?: string;
        error?: string;
      }>("/auth/onboarding/test-whatsapp", {
        method: "POST",
        body: JSON.stringify({ access_token, phone_number_id }),
      }),

    complete: (data: {
      wh_api_key:        string;
      wh_property_id:    string;
      wa_access_token:   string;
      wa_phone_number_id: string;
      property_name:     string;
      property_location: string;
      max_guests:        number;
      bedrooms:          number;
      base_rate:         number | null;
      amenities:         string[];
    }) =>
      request<{ message: string; access_token: string; tenant_id: string }>(
        "/auth/onboarding/complete",
        { method: "POST", body: JSON.stringify(data) }
      ),
  },

  owner: {
    dashboard: () => request<DashboardData>("/owner/dashboard"),

    bookings: (limit = 20, status = "confirmed") =>
      request<{ bookings: Booking[]; total: number }>(
        `/owner/bookings?limit=${limit}&status=${status}`
      ),

    hitl: () =>
      request<{ decisions: HITLDecision[]; pending: number }>("/owner/hitl"),

    approveHitl: (id: string, note = "") =>
      request<{ status: string }>(`/owner/hitl/${id}/approve`, {
        method: "POST",
        body: JSON.stringify({ note }),
      }),

    rejectHitl: (id: string, note = "") =>
      request<{ status: string }>(`/owner/hitl/${id}/reject`, {
        method: "POST",
        body: JSON.stringify({ note }),
      }),

    activity: (limit = 30) =>
      request<{ events: ActivityEvent[] }>(`/owner/activity?limit=${limit}`),

    integrations: () => request<IntegrationStatus>("/owner/integrations"),

    updateSettings: (data: {
      hitl_threshold_pct?:   number;
      pricing_floor_eur?:    number;
      pricing_ceiling_eur?:  number;
      owner_whatsapp_phone?: string;
    }) =>
      request<{ message: string; config: Record<string, unknown> }>(
        "/owner/settings",
        { method: "PATCH", body: JSON.stringify(data) }
      ),
  },
};
