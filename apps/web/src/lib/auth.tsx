'use client';

import { useRouter } from 'next/navigation';
import {
  createContext, useCallback, useContext, useEffect, useMemo, useState,
} from 'react';

import { authApi, setAccessToken } from './api';
import type { User } from './types';

interface AuthState {
  user: User | null;
  ready: boolean;
  login: (email: string, password: string) => Promise<void>;
  register: (p: {
    email: string;
    name: string;
    password: string;
    country: string;
    accepted_terms: boolean;
  }) => Promise<void>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [ready, setReady] = useState(false);
  const router = useRouter();

  // On mount, resume the session from the HttpOnly refresh cookie.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const session = await authApi.bootstrap();
        if (!cancelled && session) setUser(session.user);
      } catch {
        // No valid refresh cookie; stay signed out.
      } finally {
        if (!cancelled) setReady(true);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const login = useCallback(async (email: string, password: string) => {
    const res = await authApi.login({ email, password });
    setAccessToken(res.tokens.access_token);
    setUser(res.user);
  }, []);

  const register = useCallback(
    async (p: {
      email: string;
      name: string;
      password: string;
      country: string;
      accepted_terms: boolean;
    }) => {
      const res = await authApi.register(p);
      setAccessToken(res.tokens.access_token);
      setUser(res.user);
    },
    [],
  );

  const logout = useCallback(async () => {
    try {
      await authApi.logout();
    } finally {
      setAccessToken(null);
      setUser(null);
      router.push('/login');
    }
  }, [router]);

  const value = useMemo(
    () => ({ user, ready, login, register, logout }),
    [user, ready, login, register, logout],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error('useAuth must be used inside AuthProvider');
  return ctx;
}
