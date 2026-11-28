# Build context: repo root. The prod web server: the built console + the Caddyfile, in one image.
# (Dev serves apps/web/dist and the Caddyfile through bind mounts instead; see docker-compose.yml.)
FROM node:22-slim AS web
WORKDIR /web
COPY apps/web/package.json apps/web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY apps/web ./
# Build, then refuse to ship a bundle that contains any PageCite key (full pc_ or ask-only pca_):
# the demo key is served at runtime from /config.json, never compiled in.
RUN npm run build \
    && if grep -rEq 'pca?_[A-Za-z0-9_-]{20,}' dist; then echo 'a pc_/pca_ key is in the web bundle' >&2; exit 1; fi

FROM caddy:2
COPY Caddyfile /etc/caddy/Caddyfile
COPY --from=web /web/dist /srv/web
ARG GIT_SHA=dev
LABEL org.opencontainers.image.revision=$GIT_SHA
