# Derive only from the reviewed local gateway; never replace the 9011 image.
ARG CAPABILITY_GATEWAY_BASE_IMAGE=openscience-capability-gateway:91f5d81-release-20261111
FROM ${CAPABILITY_GATEWAY_BASE_IMAGE}
LABEL org.nexpoly.purpose="capability-gateway-development" \
      org.nexpoly.expected-base-image="sha256:fb7fdd70e46d7544d0816f042bd0a4324cd7a6c1e86a4a4606031f5108199df0"
USER root
COPY property-filter.ts /app/src/property-filter.ts
COPY polytao.ts /app/src/polytao.ts
COPY retrosynthesis.ts /app/src/retrosynthesis.ts
COPY patch.mjs /tmp/nexpoly-gateway-patch.mjs
RUN bun /tmp/nexpoly-gateway-patch.mjs /app/src/mcp.ts /app/src/server.ts \
    && bun -e 'for (const path of ["mcp", "server", "property-filter", "polytao", "retrosynthesis"]) new Bun.Transpiler({loader:"ts"}).transformSync(await Bun.file(`/app/src/${path}.ts`).text())' \
    && rm /tmp/nexpoly-gateway-patch.mjs
USER bun
