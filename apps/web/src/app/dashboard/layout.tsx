'use client';

import Link from 'next/link';
import { usePathname, useRouter } from 'next/navigation';
import { useEffect, useState, type ReactNode } from 'react';

import { ModeBadge } from '@/components/ui';
import { useAuth } from '@/lib/auth';

const NAV = [
  { href: '/dashboard', label: 'Overview' },
  { href: '/dashboard/analytics', label: 'Performance' },
  { href: '/dashboard/bots', label: 'Bots' },
  { href: '/dashboard/brokers', label: 'Brokers' },
  { href: '/dashboard/positions', label: 'Positions' },
  { href: '/dashboard/trades', label: 'Trades' },
  { href: '/dashboard/backtests', label: 'Backtests' },
];

/** Entries that only administrators should see in the navigation. */
const ADMIN_NAV = [{ href: '/dashboard/admin', label: 'Admin' }];

/** Responsive shell: sidebar on desktop, bottom tab bar on mobile. */
export default function DashboardLayout({ children }: { children: ReactNode }) {
  const { user, ready, logout } = useAuth();
  const router = useRouter();
  const pathname = usePathname();
  const [menuOpen, setMenuOpen] = useState(false);

  useEffect(() => {
    if (ready && !user) router.replace('/login');
  }, [ready, user, router]);

  if (!ready || !user) {
    return (
      <div className="flex min-h-dvh items-center justify-center">
        <span className="h-6 w-6 animate-spin rounded-full border-2 border-edge border-t-brand-400" />
      </div>
    );
  }

  const isActive = (href: string) =>
    href === '/dashboard' ? pathname === href : pathname.startsWith(href);

  // The API enforces the admin role independently; hiding the link is only so
  // ordinary users are not offered a page that will refuse to load.
  const isAdmin = user.role === 'ADMIN' || user.role === 'SUPER_ADMIN';
  const nav = isAdmin ? [...NAV, ...ADMIN_NAV] : NAV;

  return (
    <div className="min-h-dvh pb-16 lg:pb-0">
      {/* Top bar */}
      <header className="sticky top-0 z-40 border-b border-edge bg-base-950/90 backdrop-blur">
        <div className="mx-auto flex h-16 max-w-7xl items-center justify-between gap-3 px-4 lg:px-6">
          <Link href="/dashboard" className="text-lg font-semibold tracking-tight text-slate-50">
            vantage<span className="text-brand-400">.</span>
          </Link>

          <div className="flex items-center gap-2">
            <ModeBadge mode="PAPER" />
            <div className="relative">
              <button
                onClick={() => setMenuOpen((o) => !o)}
                className="flex h-9 items-center gap-2 rounded-lg border border-edge bg-base-850 px-3 text-sm text-slate-200 hover:bg-base-800"
                aria-expanded={menuOpen}
                aria-haspopup="menu"
              >
                <span className="hidden max-w-[10rem] truncate sm:inline">{user.name}</span>
                <span className="sm:hidden" aria-hidden>
                  {user.name.charAt(0).toUpperCase()}
                </span>
                <span className="text-slate-500" aria-hidden>▾</span>
              </button>
              {menuOpen && (
                <>
                  <button
                    className="fixed inset-0 z-10 cursor-default"
                    onClick={() => setMenuOpen(false)}
                    aria-label="Close menu"
                  />
                  <div className="absolute right-0 z-20 mt-2 w-56 overflow-hidden rounded-lg border border-edge bg-base-850 shadow-xl">
                    <div className="border-b border-edge px-3 py-2.5">
                      <p className="truncate text-sm font-medium text-slate-100">{user.name}</p>
                      <p className="truncate text-xs text-slate-500">{user.email}</p>
                    </div>
                    <button
                      onClick={async () => { setMenuOpen(false); await logout(); }}
                      className="w-full px-3 py-2.5 text-left text-sm text-slate-300 hover:bg-base-800"
                    >
                      Sign out
                    </button>
                  </div>
                </>
              )}
            </div>
          </div>
        </div>
      </header>

      <div className="mx-auto flex max-w-7xl gap-6 px-4 py-6 lg:px-6">
        {/* Desktop sidebar */}
        <nav className="hidden w-52 flex-none lg:block">
          <ul className="sticky top-22 space-y-1">
            {nav.map((item) => (
              <li key={item.href}>
                <Link
                  href={item.href}
                  className={`block rounded-lg px-3 py-2 text-sm font-medium transition ${
                    isActive(item.href)
                      ? 'bg-brand-500/10 text-brand-400'
                      : 'text-slate-400 hover:bg-base-850 hover:text-slate-200'
                  }`}
                >
                  {item.label}
                </Link>
              </li>
            ))}
          </ul>
        </nav>

        <main className="min-w-0 flex-1">{children}</main>
      </div>

      {/* Mobile bottom tab bar */}
      <nav className="fixed inset-x-0 bottom-0 z-40 border-t border-edge bg-base-950/95 backdrop-blur lg:hidden">
        <ul className="mx-auto flex max-w-lg">
          {nav.map((item) => (
            <li key={item.href} className="flex flex-1">
              <Link
                href={item.href}
                className={`flex w-full flex-col items-center justify-center gap-0.5 px-1 py-2.5 text-[11px] font-medium ${
                  isActive(item.href) ? 'text-brand-400' : 'text-slate-500'
                }`}
              >
                <span className="text-base leading-none" aria-hidden>
                  {NAV_ICON[item.label]}
                </span>
                {item.label}
              </Link>
            </li>
          ))}
        </ul>
      </nav>
    </div>
  );
}

const NAV_ICON: Record<string, string> = {
  Overview: '◧',
  Performance: '◔',
  Bots: '⚙',
  Brokers: '⇄',
  Positions: '◈',
  Trades: '▤',
  Admin: '⛨',
};
