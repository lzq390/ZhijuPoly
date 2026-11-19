# build_image.py supplies this named context from the verified local OCI export.
FROM reviewed-openscience-base
LABEL org.nexpoly.purpose="openscience-per-user-development" \
      org.nexpoly.template-version="53293fc-m1-v1" \
      org.nexpoly.expected-base-image="sha256:a828b3723cdf0ebe067339b88bad1ef9384ad596f50f1d448b7dfa45f28ec853"
ENV OPENSCIENCE_DISABLE_MODELS_FETCH=1 OPENSCIENCE_DISABLE_AUTOUPDATE=1

USER root
COPY --chmod=644 dev-boundary.mjs /app/backend/cli/src/server/nexpoly-dev-boundary.mjs
COPY --chmod=644 capability-form-bridge.mjs polytao-form-bridge.mjs retrosynthesis-form-bridge.mjs /app/backend/cli/src/server/
COPY --chmod=644 instance-auth.mjs /app/backend/cli/src/server/nexpoly-m1-auth.mjs
COPY --chmod=644 activity.mjs /app/backend/cli/src/server/nexpoly-m1-activity.mjs
RUN --mount=type=bind,source=patch.mjs,target=/tmp/patch.mjs \
    --mount=type=bind,source=install_patch.mjs,target=/tmp/install_patch.mjs \
    --mount=type=bind,source=patch_activity_instance.mjs,target=/tmp/patch_activity_instance.mjs \
    bun /tmp/patch_activity_instance.mjs /app/backend/cli/src/project/instance.ts && bun /tmp/install_patch.mjs

USER openscience
RUN bun --conditions=browser -e 'await import("/app/backend/cli/src/server/server.ts")'
