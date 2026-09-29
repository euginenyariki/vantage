# syntax=docker/dockerfile:1
# Edge reverse proxy: TLS termination, static/app routing and WS upgrade.
FROM nginx:1.27-alpine

COPY infrastructure/docker/nginx/default.conf /etc/nginx/conf.d/default.conf

EXPOSE 80 443
