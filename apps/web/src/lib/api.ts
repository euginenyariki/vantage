'use client';

/**
 * Browser API client.
 *
 * The access token lives in memory only (never localStorage, so an XSS bug
 * cannot read it from disk). Persistence across reloads comes from the
 * HttpOnly refresh cookie: on 401 we silently refresh once and replay.
 */

import type {
  AdminBot, AdminBroker, AdminUser, AdminUserList, AnalyticsReport, AuthResponse,
  BacktestDetail, BacktestJob, BacktestOptions, BacktestRequest, Bot,
  BrokerConnection, BrokerConnectionInput, ConnectionTestResult, EquityPoint,
  Health, Order, PerformanceMetrics, PerformanceSummary, PlatformStats, Position,
  Quote, SymbolInfo, Trade, User,
} from './types';

const BASE = process.env.NEXT_PUBLIC_API_URL || '/api';

let accessToken: string | null = null;
let refreshing: Promise<boolean> | null = null;

export function setAccessToken(token: string | null): void {
  accessToken = token;
}

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

async function parseError(res: Response): Promise<string> {
  try {
    const body = await res.json();
    const detail = body?.detail;
    if (typeof detail === 'string') return detail;
    if (Array.isArray(detail) && detail[0]?.msg) return detail[0].msg;
  } catch {
    /* fall through to the status text */
  }
  return res.statusText || `Request failed (${res.status})`;
}

async function refreshSession(): Promise<boolean> {
  if (!refreshing) {
    refreshing = (async () => {
      try {
        const res = await fetch(`${BASE}/auth/refresh`, {
          method: 'POST',
          credentials: 'include',
          headers: { 'Content-Type': 'application/json' },
          body: '{}',
        });
        if (!res.ok) return false;
        const data: AuthResponse = await res.json();
        accessToken = data.tokens.access_token;
        return true;
      } catch {
        return false;
      } finally {
        refreshing = null;
      }
    })();
  }
  return refreshing;
}

interface RequestOptions extends Omit<RequestInit, 'body'> {
  body?: unknown;
  auth?: boolean;
  retry?: boolean;
}

export async function api<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { body, auth = true, retry = true, headers, ...rest } = options;

  const finalHeaders = new Headers(headers);
  if (body !== undefined) finalHeaders.set('Content-Type', 'application/json');
  if (auth && accessToken) finalHeaders.set('Authorization', `Bearer ${accessToken}`);

  const res = await fetch(`${BASE}${path}`, {
    ...rest,
    headers: finalHeaders,
    credentials: 'include',
    body: body === undefined ? undefined : JSON.stringify(body),
  });

  if (res.status === 401 && auth && retry) {
    if (await refreshSession()) {
      return api<T>(path, { ...options, retry: false });
    }
    accessToken = null;
  }

  if (!res.ok) throw new ApiError(await parseError(res), res.status);
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

// --- auth ---------------------------------------------------------------- //
export const authApi = {
  register: (p: {
    email: string;
    name: string;
    password: string;
    country: string;
    accepted_terms: boolean;
  }) => api<AuthResponse>('/auth/register', { method: 'POST', body: p, auth: false }),
  login: (p: { email: string; password: string }) =>
    api<AuthResponse>('/auth/login', { method: 'POST', body: p, auth: false }),
  logout: () => api<void>('/auth/logout', { method: 'POST' }),
  me: () => api<User>('/auth/me'),
  /**
   * Restore a session on page load. Uses /auth/refresh directly (it returns
   * the user too) so a cold load costs one request instead of a guaranteed
   * 401 from /auth/me followed by a refresh.
   */
  bootstrap: async (): Promise<AuthResponse | null> => {
    const res = await fetch(`${BASE}/auth/refresh`, {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: '{}',
    });
    if (!res.ok) return null;
    const data: AuthResponse = await res.json();
    setAccessToken(data.tokens.access_token);
    return data;
  },
};

// --- dashboard ----------------------------------------------------------- //
export const dashboardApi = {
  summary: () => api<PerformanceSummary>('/dashboard/summary'),
  equity: (days = 30) => api<EquityPoint[]>(`/dashboard/equity?days=${days}`),
};

// --- bots ---------------------------------------------------------------- //
export const botsApi = {
  list: () => api<Bot[]>('/bots'),
  get: (id: string) => api<Bot>(`/bots/${id}`),
  create: (p: Partial<Bot> & { name: string; symbol: string }) =>
    api<Bot>('/bots', { method: 'POST', body: p }),
  update: (id: string, p: Partial<Bot>) =>
    api<Bot>(`/bots/${id}`, { method: 'PATCH', body: p }),
  remove: (id: string) => api<void>(`/bots/${id}`, { method: 'DELETE' }),
  start: (id: string) => api<{ ok: boolean; message: string }>(`/bots/${id}/start`, { method: 'POST' }),
  stop: (id: string) => api<{ ok: boolean; message: string }>(`/bots/${id}/stop`, { method: 'POST' }),
  pause: (id: string) => api<{ ok: boolean; message: string }>(`/bots/${id}/pause`, { method: 'POST' }),
  resume: (id: string) => api<{ ok: boolean; message: string }>(`/bots/${id}/resume`, { method: 'POST' }),
  killSwitch: (id: string) => api<{ ok: boolean; message: string }>(`/bots/${id}/kill-switch`, { method: 'POST' }),
  closePosition: (id: string) => api<{ ok: boolean; message: string }>(`/positions/${id}/close`, { method: 'POST' }),
};

// --- brokers ------------------------------------------------------------- //
export const brokersApi = {
  list: () => api<BrokerConnection[]>('/brokers'),
  get: (id: string) => api<BrokerConnection>(`/brokers/${id}`),
  create: (p: BrokerConnectionInput) =>
    api<BrokerConnection>('/brokers', { method: 'POST', body: p }),
  remove: (id: string) => api<void>(`/brokers/${id}`, { method: 'DELETE' }),
  test: (id: string) =>
    api<ConnectionTestResult>(`/brokers/${id}/test`, { method: 'POST' }),
  disconnect: (id: string) =>
    api<BrokerConnection>(`/brokers/${id}/disconnect`, { method: 'POST' }),
};

// --- trading data -------------------------------------------------------- //
export const tradingApi = {
  positions: (openOnly = false) => api<Position[]>(`/positions?open_only=${openOnly}`),
  orders: () => api<Order[]>('/orders'),
  trades: () => api<Trade[]>('/trades'),
};

// --- analytics ----------------------------------------------------------- //
export const analyticsApi = {
  report: (days = 90) => api<AnalyticsReport>(`/analytics/report?days=${days}`),
  performance: (days = 90) =>
    api<PerformanceMetrics>(`/analytics/performance?days=${days}`),
};

// --- administration ------------------------------------------------------ //
export const adminApi = {
  stats: () => api<PlatformStats>('/admin/stats'),
  users: (q?: string) =>
    api<AdminUserList>(`/admin/users${q ? `?q=${encodeURIComponent(q)}` : ''}`),
  setUserActive: (id: string, active: boolean) =>
    api<AdminUser>(`/admin/users/${id}/active?active=${active}`, { method: 'POST' }),
  brokers: () => api<AdminBroker[]>('/admin/brokers'),
  bots: () => api<AdminBot[]>('/admin/bots'),
};

// --- market -------------------------------------------------------------- //
export const marketApi = {
  symbols: () => api<SymbolInfo[]>('/market/symbols'),
  quotes: () => api<Quote[]>('/market/quotes'),
  health: () => api<Health>('/health', { auth: false, retry: false }),
};

// --- backtesting --------------------------------------------------------- //
export const backtestApi = {
  options: () => api<BacktestOptions>('/backtests/options'),
  list: () => api<BacktestJob[]>('/backtests'),
  get: (id: string) => api<BacktestDetail>(`/backtests/${id}`),
  create: (p: BacktestRequest) =>
    api<BacktestJob>('/backtests', { method: 'POST', body: p }),
  remove: (id: string) => api<void>(`/backtests/${id}`, { method: 'DELETE' }),
  requeue: (id: string) =>
    api<BacktestJob>(`/backtests/${id}/requeue`, { method: 'POST' }),
};

/** Resolve the realtime endpoint.
 *
 * REST stays same-origin (`/api` is rewritten to the API container by
 * next.config), but Next rewrites cannot proxy a WebSocket upgrade, so the
 * socket has to address the API directly. NEXT_PUBLIC_WS_URL therefore points
 * at the API's own origin; if it is unset we fall back to the REST origin.
 */
function wsUrl(): string {
  const proto = window.location.protocol === 'https:' ? 'wss' : 'ws';
  const configured = process.env.NEXT_PUBLIC_WS_URL || process.env.NEXT_PUBLIC_API_URL;
  if (configured) return `${configured.replace(/^https?:/, proto)}/ws`;
  return `${proto}://${window.location.host}/ws`;
}

/** Open a realtime event stream. Returns a disposer. */
export function connectEvents(
  onEvent: (e: Record<string, unknown>) => void,
  onStatus?: (connected: boolean) => void,
): () => void {
  if (typeof window === 'undefined') return () => {};
  const token = accessToken;
  if (!token) return () => {};

  const socket = new WebSocket(`${wsUrl()}?token=${encodeURIComponent(token)}`);
  let retryTimer: ReturnType<typeof setTimeout> | null = null;
  let closed = false;

  socket.onopen = () => onStatus?.(true);
  socket.onmessage = (msg) => {
    try {
      onEvent(JSON.parse(msg.data));
    } catch {
      /* ignore malformed frames */
    }
  };
  socket.onclose = () => {
    onStatus?.(false);
    // Reconnect with a fixed delay: the server sends pings, so a close here
    // means the connection actually dropped.
    if (!closed) retryTimer = setTimeout(() => connectEvents(onEvent, onStatus), 5000);
  };

  return () => {
    closed = true;
    if (retryTimer) clearTimeout(retryTimer);
    socket.close();
  };
}
