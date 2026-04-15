"use client";

/**
 * Auth context — frontend/lib/auth.ts
 *
 * Provides AuthProvider and useAuth hook to all client components.
 * Stores JWT in localStorage. Decodes it client-side for fast reads.
 *
 * Usage:
 *   const { user, login, logout, isLoading } = useAuth();
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
} from "react";

const TOKEN_KEY = "vayancy_token";

export interface User {
  sub:                 string;
  email:               string;
  name:                string;
  tenant_id:           string | null;
  tenant_api_key:      string | null;
  onboarding_complete: boolean;
}

interface AuthContextValue {
  user:            User | null;
  isLoading:       boolean;
  isAuthenticated: boolean;
  login:           (token: string) => void;
  logout:          () => void;
}

const AuthContext = createContext<AuthContextValue>({
  user:            null,
  isLoading:       true,
  isAuthenticated: false,
  login:           () => {},
  logout:          () => {},
});

function decodeJwt(token: string): User | null {
  try {
    const payload = token.split(".")[1];
    const decoded = JSON.parse(atob(payload.replace(/-/g, "+").replace(/_/g, "/")));
    // Check expiry
    if (decoded.exp && decoded.exp * 1000 < Date.now()) return null;
    return {
      sub:                 decoded.sub,
      email:               decoded.email,
      name:                decoded.name || "",
      tenant_id:           decoded.tenant_id || null,
      tenant_api_key:      decoded.tenant_api_key || null,
      onboarding_complete: Boolean(decoded.tenant_id),
    };
  } catch {
    return null;
  }
}

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user,      setUser]      = useState<User | null>(null);
  const [isLoading, setIsLoading] = useState(true);

  useEffect(() => {
    const token = localStorage.getItem(TOKEN_KEY);
    if (token) {
      const decoded = decodeJwt(token);
      if (decoded) {
        setUser(decoded);
      } else {
        localStorage.removeItem(TOKEN_KEY);
      }
    }
    setIsLoading(false);
  }, []);

  const login = useCallback((token: string) => {
    localStorage.setItem(TOKEN_KEY, token);
    const decoded = decodeJwt(token);
    setUser(decoded);
  }, []);

  const logout = useCallback(() => {
    localStorage.removeItem(TOKEN_KEY);
    setUser(null);
    window.location.href = "/login";
  }, []);

  return (
    <AuthContext.Provider
      value={{
        user,
        isLoading,
        isAuthenticated: Boolean(user),
        login,
        logout,
      }}
    >
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  return useContext(AuthContext);
}
