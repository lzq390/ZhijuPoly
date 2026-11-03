# The development launcher must verify this local tag resolves to the exact image
# ID below before building. The source checksum independently fails closed.
ARG OPENSCIENCE_BASE_IMAGE=openscience-backend-poc:38ba323-demo-rc1
FROM ${OPENSCIENCE_BASE_IMAGE}
LABEL org.nexpoly.purpose="openscience-mcp-development" \
      org.nexpoly.expected-base-image="sha256:41a2b6929bc74a1bf3c0bac54df09f8e5f835954b3ba58dfba81f703b3ffe17a"
# The upstream application source belongs to root. Elevate only in this build layer.
USER root
COPY dev-boundary.mjs /app/backend/cli/src/server/nexpoly-dev-boundary.mjs
COPY patch.mjs /tmp/nexpoly-mcp-compat-patch.mjs
RUN bun /tmp/nexpoly-mcp-compat-patch.mjs /app/backend/cli/src/session/prompt.ts \
    && bun -e 'new Bun.Transpiler({ loader: "ts" }).transformSync(await Bun.file("/app/backend/cli/src/session/prompt.ts").text())' \
    && bun -e 'new Bun.Transpiler({ loader: "ts" }).transformSync(await Bun.file("/app/backend/cli/src/server/server.ts").text())' \
    && rm /tmp/nexpoly-mcp-compat-patch.mjs
USER openscience
