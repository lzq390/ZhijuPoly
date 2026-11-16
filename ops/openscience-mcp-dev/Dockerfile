# The development launcher must verify this local tag resolves to the exact image
# ID below before building. The source checksum independently fails closed.
ARG OPENSCIENCE_BASE_IMAGE=openscience-backend-poc:release-20261029
FROM ${OPENSCIENCE_BASE_IMAGE}
LABEL org.nexpoly.purpose="openscience-mcp-development" \
      org.nexpoly.expected-base-image="sha256:7e4c055b4641bfcc0d329cbb2b61ef473b89e8435b3505dabc7b2895d11f0435"
# The upstream application source belongs to root. Elevate only in this build layer.
USER root
COPY dev-boundary.mjs /app/backend/cli/src/server/nexpoly-dev-boundary.mjs
COPY patch.mjs /tmp/nexpoly-mcp-compat-patch.mjs
RUN bun /tmp/nexpoly-mcp-compat-patch.mjs /app/backend/cli/src/session/prompt.ts \
    && bun -e 'new Bun.Transpiler({ loader: "ts" }).transformSync(await Bun.file("/app/backend/cli/src/session/prompt.ts").text())' \
    && bun -e 'new Bun.Transpiler({ loader: "ts" }).transformSync(await Bun.file("/app/backend/cli/src/server/server.ts").text())' \
    && rm /tmp/nexpoly-mcp-compat-patch.mjs
USER openscience
