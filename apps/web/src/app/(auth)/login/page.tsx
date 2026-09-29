'use client';

import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { useEffect, useState, type FormEvent } from 'react';

import { ErrorNote } from '@/components/ui';
import { useAuth } from '@/lib/auth';

export default function LoginPage() {
  const { login, user, ready } = useAuth();
  const router = useRouter();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (ready && user) router.replace('/dashboard');
  }, [ready, user, router]);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setError('');
    setBusy(true);
    try {
      await login(email, password);
      router.replace('/dashboard');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Sign in failed');
      setBusy(false);
    }
  }

  return (
    <div className="card">
      <h1 className="text-xl font-semibold text-slate-50">Sign in</h1>
      <p className="mt-1 text-sm text-slate-400">Welcome back.</p>

      <form onSubmit={onSubmit} className="mt-6 space-y-4">
        {error ? <ErrorNote message={error} /> : null}
        <div>
          <label className="label" htmlFor="email">Email</label>
          <input
            id="email" type="email" autoComplete="email" required
            value={email} onChange={(e) => setEmail(e.target.value)}
            placeholder="you@example.com"
          />
        </div>
        <div>
          <label className="label" htmlFor="password">Password</label>
          <input
            id="password" type="password" autoComplete="current-password" required
            value={password} onChange={(e) => setPassword(e.target.value)}
            placeholder="••••••••••"
          />
        </div>
        <button type="submit" className="btn-primary w-full" disabled={busy}>
          {busy ? 'Signing in…' : 'Sign in'}
        </button>
      </form>

      <p className="mt-6 text-center text-sm text-slate-400">
        No account yet?{' '}
        <Link href="/register" className="font-medium text-brand-400 hover:text-brand-500">
          Create one
        </Link>
      </p>
    </div>
  );
}
