# Phase 4.1: frontend image — builds the agent-ui SPA workspaces (protocol /
# client / catalog / react / embedded-host-example) and serves the example Vite
# bundle from nginx (the root-level API reverse proxy ships in the image and is
# overridden by the Helm frontend-configmap when deployed in-cluster).
#
# Build (from repo root):
#   docker build -f deploy/images/frontend.Dockerfile -t your-registry/.../frontend .
# Optional build args:
#   NPM_REGISTRY_URL=https://registry.npmmirror.com   (China mirror override)
ARG NODE_IMAGE=node:22-alpine
ARG NGINX_IMAGE=nginx:1.27-alpine

# ---------------------------------------------------------------------------
# Stage 1: builder — install workspace deps and build all packages + example.
# ---------------------------------------------------------------------------
FROM ${NODE_IMAGE} AS builder
ARG NPM_REGISTRY_URL=
ENV npm_config_registry=${NPM_REGISTRY_URL:-https://registry.npmjs.org/}
# npm 11 warns loudly on --no-audit/--no-fund by default; keep install quiet.
ENV npm_config_audit=false npm_config_fund=false
WORKDIR /app/frontend
# .dockerignore keeps node_modules / dist / tsbuildinfo out of the context.
COPY frontend/ /app/frontend/
RUN npm ci && npm run build

# ---------------------------------------------------------------------------
# Stage 2: runtime — static nginx serving the built SPA. Runs as the nginx
# user (101:101) so the pod can satisfy runAsNonRoot; port 80 binding works
# because Kubernetes keeps CAP_NET_BIND_SERVICE on containers by default. The
# full nginx.conf below relocates pid/error-log/temp dirs under /tmp so the
# non-root master can start without touching root-owned paths.
# ---------------------------------------------------------------------------
FROM ${NGINX_IMAGE}
ENV NGINX_ENTRYPOINT_QUIET_LOGS=1
COPY deploy/images/frontend-nginx.conf /etc/nginx/nginx.conf
COPY deploy/images/frontend-default.conf /etc/nginx/conf.d/default.conf
COPY --from=builder /app/frontend/examples/embedded-host-example/dist /usr/share/nginx/html
# Pre-create the nginx scratch dirs (nginx does not mkdir parent chains) and
# hand /tmp to the runtime user so the non-root master can start.
RUN mkdir -p /tmp/nginx/client_body /tmp/nginx/proxy /tmp/nginx/fastcgi \
        /tmp/nginx/uwsgi /tmp/nginx/scgi \
    && chown -R 101:101 /usr/share/nginx/html /tmp/nginx
USER 101:101
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]