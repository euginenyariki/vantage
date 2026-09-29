'use client';

import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { useEffect, useState, type FormEvent } from 'react';

import { ErrorNote } from '@/components/ui';
import { useAuth } from '@/lib/auth';

const COUNTRIES = [
  ['US', 'United States'], ['GB', 'United Kingdom'], ['DE', 'Germany'], ['FR', 'France'],
  ['ES', 'Spain'], ['IT', 'Italy'], ['NL', 'Netherlands'], ['CA', 'Canada'],
  ['AU', 'Australia'], ['NZ', 'New Zealand'], ['JP', 'Japan'], ['SG', 'Singapore'],
  ['AE', 'United Arab Emirates'], ['ZA', 'South Africa'],
  // East Africa and the wider African market, where a large share of traders
  // on this platform are based.
  ['KE', 'Kenya'], ['UG', 'Uganda'], ['TZ', 'Tanzania'], ['RW', 'Rwanda'],
  ['NG', 'Nigeria'], ['GH', 'Ghana'], ['ET', 'Ethiopia'], ['EG', 'Egypt'],
  ['MA', 'Morocco'], ['ZW', 'Zimbabwe'], ['ZM', 'Zambia'], ['BW', 'Botswana'],
] as const;

export default function RegisterPage() {
  const { register, user, ready } = useAuth();
  const router = useRouter();
  const [form, setForm] = useState({
    name: '', email: '', password: '', country: 'US', terms: false,
  });
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (ready && user) router.replace('/dashboard');
  }, [ready, user, router]);

  const set = <K extends keyof typeof form>(k: K, v: (typeof form)[K]) =>
    setForm((f) => ({ ...f, [k]: v }));

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setError('');
    if (!form.terms) {
      setError('You must accept the terms to create an account');
      return;
    }
    setBusy(true);
    try {
      await register({
        email: form.email, name: form.name,
        password: form.password, country: form.country,
        // The API requires this field; the checkbox is enforced above too.
        accepted_terms: form.terms,
      });
      router.replace('/dashboard');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Registration failed');
      setBusy(false);
    }
  }

  return (
    <div className="card">
      <h1 className="text-xl font-semibold text-slate-50">Create your account</h1>
      <p className="mt-1 text-sm text-slate-400">Paper trade to start. No card required.</p>

      <form onSubmit={onSubmit} className="mt-6 space-y-4">
        {error ? <ErrorNote message={error} /> : null}
        <div>
          <label className="label" htmlFor="name">Full name</label>
          <input id="name" required maxLength={120} autoComplete="name"
            value={form.name} onChange={(e) => set('name', e.target.value)} />
        </div>
        <div>
          <label className="label" htmlFor="email">Email</label>
          <input id="email" type="email" required autoComplete="email"
            value={form.email} onChange={(e) => set('email', e.target.value)}
            placeholder="you@example.com" />
        </div>
        <div>
          <label className="label" htmlFor="password">Password</label>
          <input id="password" type="password" required minLength={10} autoComplete="new-password"
            value={form.password} onChange={(e) => set('password', e.target.value)} />
          <p className="mt-1.5 text-xs text-slate-500">
            At least 10 characters, with upper and lower case and a digit.
          </p>
        </div>
        <div>
          <label className="label" htmlFor="country">Country</label>
          <select id="country" value={form.country}
            onChange={(e) => set('country', e.target.value)}>
            {COUNTRIES.map(([code, name]) => (
              <option key={code} value={code}>{name}</option>
            ))}
          </select>
        </div>
        <label className="flex items-start gap-2.5 text-sm text-slate-400">
          <input
            type="checkbox" checked={form.terms} required
            onChange={(e) => set('terms', e.target.checked)}
            className="mt-0.5 h-4 w-4 w-auto flex-none rounded border-edge bg-base-900"
          />
          <span>
            I understand that automated trading carries a risk of loss, and I accept the
            terms of service.
          </span>
        </label>
        <button type="submit" className="btn-primary w-full" disabled={busy}>
          {busy ? 'Creating account…' : 'Create account'}
        </button>
      </form>

      <p className="mt-6 text-center text-sm text-slate-400">
        Already registered?{' '}
        <Link href="/login" className="font-medium text-brand-400 hover:text-brand-500">
          Sign in
        </Link>
      </p>
    </div>
  );
}
