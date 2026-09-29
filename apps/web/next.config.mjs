/** @type {import('next').NextConfig} */
const API = process.env.API_INTERNAL_URL || 'http://127.0.0.1:8000';

const nextConfig = {
  reactStrictMode: true,
  // Emit a self-contained server bundle so the runtime image needs no node_modules.
  output: 'standalone',
  // Server-side fetches go to the API directly; the browser talks to
  // /api/* on this origin so there is no CORS preflight in production.
  async rewrites() {
    return [{ source: '/api/:path*', destination: `${API}/api/:path*` }];
  },
};

export default nextConfig;
