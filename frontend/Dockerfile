FROM node:22-bookworm-slim@sha256:d9f850096136edbc402debdd8729579a288aac64574ada0ff4db26b6ae58b0b2 AS build

WORKDIR /app/frontend

ARG VITE_AGENT_WORKSPACE_URL=""
ENV VITE_AGENT_WORKSPACE_URL="${VITE_AGENT_WORKSPACE_URL}"

COPY frontend/package*.json ./
RUN npm ci

COPY contracts/monomer_dft_api_contract_v1.json /app/contracts/monomer_dft_api_contract_v1.json
COPY backend/tests/fixtures/monomer_dft_scientific_result_v1_legacy.json /app/backend/tests/fixtures/monomer_dft_scientific_result_v1_legacy.json
COPY frontend ./
RUN npm run build

FROM nginx:1.27-alpine@sha256:65645c7bb6a0661892a8b03b89d0743208a18dd2f3f17a54ef4b76fb8e2f2a10

ARG SOURCE_REVISION="unknown"
ARG SOURCE_URL="https://github.com/lzq390/ZhijuPoly"
ARG VERSION="dev"

LABEL org.opencontainers.image.source="$SOURCE_URL" \
      org.opencontainers.image.revision="$SOURCE_REVISION" \
      org.opencontainers.image.version="$VERSION"

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/frontend/dist /usr/share/nginx/html
RUN chmod 0644 /etc/nginx/conf.d/default.conf \
    && find /usr/share/nginx/html -type d -exec chmod 0755 {} + \
    && find /usr/share/nginx/html -type f -exec chmod 0644 {} +

EXPOSE 80
