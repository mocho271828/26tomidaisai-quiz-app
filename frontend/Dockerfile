ARG SOURCE_COMMIT=unversioned
ARG DEPLOYMENT_ENVIRONMENT=local-demo

FROM --platform=$BUILDPLATFORM node:22.19.0-alpine AS build

ARG VITE_EVENT_START_AT=""
ARG VITE_SURVEY_URL=""
ARG VITE_HOME_URL=""

ENV VITE_EVENT_START_AT=${VITE_EVENT_START_AT} \
    VITE_SURVEY_URL=${VITE_SURVEY_URL} \
    VITE_HOME_URL=${VITE_HOME_URL}

RUN corepack enable
WORKDIR /app

COPY frontend/package.json frontend/pnpm-lock.yaml frontend/pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile

COPY frontend/ ./
RUN pnpm build

FROM caddy:2.10.2-alpine AS runtime

# BusyBox wgetにないCA・接続先名検証付きTLS healthcheckを使う。
RUN apk add --no-cache curl

ARG SOURCE_COMMIT
ARG DEPLOYMENT_ENVIRONMENT

LABEL org.opencontainers.image.revision="${SOURCE_COMMIT}" \
    io.tomidaisai.deployment-environment="${DEPLOYMENT_ENVIRONMENT}"

COPY infra/Caddyfile /etc/caddy/Caddyfile
COPY infra/Caddyfile.local-multi-api /etc/caddy/Caddyfile.multi-api
COPY infra/Caddyfile.load-test /etc/caddy/Caddyfile.load-test
COPY --from=build /app/dist /srv

EXPOSE 8080
