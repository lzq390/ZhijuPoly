ARG OPENSCIENCE_BASE_IMAGE=ghcr.io/lzq390/nexpoly-web@sha256:e7d25a1b6d515daec641c8de9c98265f275991eee2396dc578ce9c2fcfdeb197

FROM ${OPENSCIENCE_BASE_IMAGE} AS openscience-base

FROM node:22-bookworm-slim@sha256:d9f850096136edbc402debdd8729579a288aac64574ada0ff4db26b6ae58b0b2 AS patcher

WORKDIR /work
COPY --from=openscience-base /usr/share/nginx/html /work/base-html
COPY ops/openscience-ui-overlay/patch.mjs /work/patch.mjs
RUN node /work/patch.mjs /work/base-html /work/patched

FROM ${OPENSCIENCE_BASE_IMAGE}

ARG SOURCE_REVISION="unknown"
ARG SOURCE_URL="https://github.com/lzq390/ZhijuPoly"
ARG VERSION="dev"
ARG OPENSCIENCE_BASE_MANIFEST="sha256:e7d25a1b6d515daec641c8de9c98265f275991eee2396dc578ce9c2fcfdeb197"

LABEL org.opencontainers.image.source="$SOURCE_URL" \
      org.opencontainers.image.revision="$SOURCE_REVISION" \
      org.opencontainers.image.version="$VERSION" \
      com.nexpoly.openscience.base-image-id="sha256:e7d25a1b6d515daec641c8de9c98265f275991eee2396dc578ce9c2fcfdeb197" \
      com.nexpoly.openscience.base-manifest="$OPENSCIENCE_BASE_MANIFEST" \
      com.nexpoly.openscience.base-static-tree="sha256:b942ea5aac61ed7568327e40a3193b3c35c12380f05234775f122b6a2b98bc9d" \
      com.nexpoly.openscience.derived-static-tree="sha256:32f45b16e585ef348b4a83a9763412476568ec1781aecb5be69ebd7d7f3c54fd" \
      com.nexpoly.openscience.parent-origins="http://114.214.255.154:9000,http://114.214.255.154:9001" \
      com.nexpoly.openscience.parent-policy-sha256="sha256:955ae6f5f3d0710dcaacc0906f6326a4ba99321a0e47fc928c198c8967dd0042"

USER root
COPY --from=patcher /work/patched/index.html /usr/share/nginx/html/index.html
COPY --from=patcher /work/patched/assets/ /usr/share/nginx/html/assets/
RUN rm -f /usr/share/nginx/html/assets/index-B2eNxQLj.js \
    && test "$(find /usr/share/nginx/html/assets -maxdepth 1 -type f -name 'index-nexpoly-*.js' | wc -l)" -eq 1
USER nginx
