import type { Config } from 'tailwindcss';

const config: Config = {
  content: ['./src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        base: { 950: '#070b14', 900: '#0b1220', 850: '#0f172a', 800: '#151e33' },
        edge: '#1e293b',
        brand: { 400: '#38bdf8', 500: '#0ea5e9', 600: '#0284c7' },
        up: { DEFAULT: '#22c55e', dim: '#14532d' },
        down: { DEFAULT: '#ef4444', dim: '#7f1d1d' },
      },
      fontFamily: {
        sans: ['ui-sans-serif', 'system-ui', '-apple-system', 'Segoe UI', 'Roboto', 'sans-serif'],
        mono: ['ui-monospace', 'SFMono-Regular', 'Menlo', 'monospace'],
      },
    },
  },
  plugins: [],
};
export default config;
