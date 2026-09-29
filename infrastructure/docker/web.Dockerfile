# syntax=docker/dockerfile:1
# Vantage web: Next.js built to a standalone bundle, run by node.
FROM node:22-alpine AS deps

WORKDIR /app
COPY apps/web/package.json apps/web/package-lock.json* ./
RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi

FROM node:22-alpine AS builder

WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY apps/web/ ./

# NEXT_PUBLIC_* values are inlined at build time, so the browser must use
# same-origin relative URLs (the app proxies /api to the API container).
ENV NEXT_TELEMETRY_DISABLED=1 \
    NODE_ENV=production
RUN npm run build

FROM nginx:1.27-alpine AS runtime

COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static
COPY --from=builder /app/public ./public

# The standalone bundle reads PORT and HOSTNAME at runtime.
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0

EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD wget -qO- http://127.0.0.1:3000/ >/dev/null || exit 1

CMD ["node", "server.js"]
