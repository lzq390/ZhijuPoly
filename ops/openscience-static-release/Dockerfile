# The caller must supply the currently deployed immutable UI image, including
# its registry digest. This layer preserves that UI and its API/bridge routing.
ARG OPENSCIENCE_CURRENT_IMAGE
FROM ${OPENSCIENCE_CURRENT_IMAGE}

ARG OPENSCIENCE_CURRENT_IMAGE
ARG SOURCE_REVISION=unknown
LABEL org.opencontainers.image.revision="$SOURCE_REVISION" \
      com.nexpoly.openscience.current-image="$OPENSCIENCE_CURRENT_IMAGE" \
      com.nexpoly.openscience.static-release-policy="1"

# Build context is the reviewed preparation directory, never a live data tree.
COPY static/ /usr/share/nginx/html/
COPY nginx.conf /etc/nginx/nginx.conf
