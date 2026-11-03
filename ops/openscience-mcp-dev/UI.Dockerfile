# Both local tags are checked against immutable image IDs by the development
# launcher before Compose builds. No registry access or application rebuild.
ARG OPENSCIENCE_UI_BASE_IMAGE=openscience-ui-poc:4522be0-production-dual
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
      org.nexpoly.expected-base-image="sha256:7dfa854a2a934cd8a6f7f62ec6bda97c6d9ab6faaa6a67c12c24cebf95d6809a" \
      org.nexpoly.expected-base-index-sha256="5647ece9a54520ad8df4b04b2424293a54f3537f21293649729522144ddd3157" \
      org.nexpoly.ui-overlay-format="1"
# Copy only the guarded index and the new content-addressed assets. Keep the
# upstream bundle, nginx configuration, entrypoint and healthcheck intact.
USER root
COPY --from=patch /work/overlay/ /usr/share/nginx/html/
USER 1001:1001
