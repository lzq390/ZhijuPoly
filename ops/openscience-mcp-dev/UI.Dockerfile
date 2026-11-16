# Both local tags are checked against immutable image IDs by the development
# launcher before Compose builds. No registry access or application rebuild.
ARG OPENSCIENCE_UI_BASE_IMAGE=openscience-ui-poc:9e342b5-production-dual
ARG OPENSCIENCE_UI_PATCH_IMAGE=node:22-bookworm-slim
FROM ${OPENSCIENCE_UI_BASE_IMAGE} AS source

FROM ${OPENSCIENCE_UI_PATCH_IMAGE} AS patch
USER root
WORKDIR /work
COPY --from=source /usr/share/nginx/html/index.html /work/index.html
COPY ui-patch.mjs /work/ui-patch.mjs
COPY results-controller.mjs results-view.mjs results-view.css /work/assets/
RUN node /work/ui-patch.mjs /work/index.html /work/assets /work/overlay

FROM ${OPENSCIENCE_UI_BASE_IMAGE}
LABEL org.nexpoly.purpose="openscience-mcp-ui-development" \
      org.nexpoly.expected-base-image="sha256:fea8a3c294bb324a98041f8f8354448e6ca5c6502dedc565c4b884994c567bb0" \
      org.nexpoly.expected-base-index-sha256="98d73cc0ab6b6504ec0ee2f9bba49f5a98f78486f2ca16fa37aa97ce6eb5fe71" \
      org.nexpoly.ui-overlay-format="1"
# Copy only the guarded index and the new content-addressed assets. Keep the
# complete production bundle (including cache compatibility assets), nginx
# configuration, entrypoint and healthcheck intact. The private development
# network resolves openscience-backend to the independent development service.
USER root
COPY --from=patch /work/overlay/ /usr/share/nginx/html/
USER 1001:1001
