FROM reviewed-gateway-base
LABEL org.nexpoly.purpose="openscience-per-user-development-gateway" \
      org.nexpoly.expected-base-image="sha256:fb7fdd70e46d7544d0816f042bd0a4324cd7a6c1e86a4a4606031f5108199df0"
USER root
COPY delegation.ts /app/src/delegation.ts
COPY install_patch.mjs /tmp/install-m3.mjs
RUN bun /tmp/install-m3.mjs && rm /tmp/install-m3.mjs
USER bun
COPY gateway.test.ts /tmp/gateway.test.ts
RUN bun test /tmp/gateway.test.ts
