# The development launcher must verify this local tag resolves to the exact image
# ID below before building. The source checksum independently fails closed.
ARG OPENSCIENCE_BASE_IMAGE=openscience-backend-poc:release-20261029
FROM ${OPENSCIENCE_BASE_IMAGE}
LABEL org.nexpoly.purpose="openscience-mcp-development" \
      org.nexpoly.expected-base-image="sha256:7e4c055b4641bfcc0d329cbb2b61ef473b89e8435b3505dabc7b2895d11f0435"
# The upstream application source belongs to root. Elevate only in this build layer.
USER root
COPY --chmod=644 dev-boundary.mjs /app/backend/cli/src/server/nexpoly-dev-boundary.mjs
COPY --chmod=644 capability-form-bridge.mjs /app/backend/cli/src/server/capability-form-bridge.mjs
COPY --chmod=644 retrosynthesis-form-bridge.mjs /app/backend/cli/src/server/retrosynthesis-form-bridge.mjs
COPY --chmod=644 polytao-form-bridge.mjs /app/backend/cli/src/server/polytao-form-bridge.mjs
COPY patch.mjs /tmp/nexpoly-mcp-compat-patch.mjs
RUN bun /tmp/nexpoly-mcp-compat-patch.mjs /app/backend/cli/src/session/prompt.ts \
    && bun -e 'new Bun.Transpiler({ loader: "ts" }).transformSync(await Bun.file("/app/backend/cli/src/session/prompt.ts").text())' \
    && bun -e 'new Bun.Transpiler({ loader: "ts" }).transformSync(await Bun.file("/app/backend/cli/src/server/server.ts").text())' \
    && rm /tmp/nexpoly-mcp-compat-patch.mjs
USER openscience
# Build-time regression gate: verify imports under the actual runtime UID.
RUN bun -e 'await import("/app/backend/cli/src/server/nexpoly-dev-boundary.mjs"); await import("/app/backend/cli/src/server/polytao-form-bridge.mjs"); await import("/app/backend/cli/src/server/retrosynthesis-form-bridge.mjs")'
