import type { Metadata, Viewport } from 'next';

import { AuthProvider } from '@/lib/auth';

import './globals.css';

export const metadata: Metadata = {
  title: 'Vantage — Automated Forex Trading',
  description:
    'Deploy risk-managed trading bots across currency pairs and metals. Paper trade first, go live when you are ready.',
};

export const viewport: Viewport = {
  width: 'device-width',
  initialScale: 1,
  themeColor: '#070b14',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-dvh">
        <AuthProvider>{children}</AuthProvider>
      </body>
    </html>
  );
}
