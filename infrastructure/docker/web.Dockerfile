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

# next.config.mjs reads API_INTERNAL_URL while building, because rewrites are
# baked into the output. NEXT_PUBLIC_WS_URL is inlined into the client bundle
# for the same reason. Both target the API Container App's FQDN, which only
# exists after the environment is provisioned, so the build is split into phases
# and these are passed in as build args. The defaults keep a local image working
# unchanged.
ARG API_INTERNAL_URL=http://127.0.0.1:8000
ARG NEXT_PUBLIC_WS_URL=

# NEXT_PUBLIC_API_URL is deliberately left unset so the browser keeps using
# same-origin /api and the rewrite above; only the socket goes direct.
ENV NEXT_TELEMETRY_DISABLED=1 \
    NODE_ENV=production \
    API_INTERNAL_URL=${API_INTERNAL_URL} \
    NEXT_PUBLIC_WS_URL=${NEXT_PUBLIC_WS_URL}
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
