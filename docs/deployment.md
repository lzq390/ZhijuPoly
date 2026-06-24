# nexpoly Deployment

This deployment runs nexpoly as a Docker Compose project named `nexpoly`.
It is designed to run in parallel with the existing `polyprop` deployment and
must not stop, remove, or overwrite any `polyprop` containers, images, volumes,
or deployment directories.

Default server deployment path:

```text
/data/lzq/gith/nexpoly
```

Default public URL:

```text
http://114.214.255.154:9000
```

## Services

- `backend`: FastAPI service on port `8000` inside the Docker network.
  By default it is limited to host GPU `2` through `NEXPOLY_GPU_DEVICE`.
- `online-retrieval`: Flask/Gunicorn online literature retrieval service on port `5002` inside the Docker network.
- `lab-postgres`: internal PostgreSQL service for lab data.
- `nginx`: Nginx static site on host port `${NEXPOLY_WEB_PORT:-9000}`, with `/api` proxied to the backend.

Local build images are named explicitly:

```text
nexpoly-backend:latest
nexpoly-nginx:latest
nexpoly-online-retrieval:latest
```

## Required Runtime Files

Runtime databases are mounted from `backend/data` and are not baked into the
image. Prepare these files before starting Docker Compose:

```text
backend/data/polyprop.db
backend/data/fumol.db
backend/data/pi_reverse_design.db
```

Required model artifacts:

```text
model/rf_*.pkl
model/ocsr/swin_base_char_aux_1m.pth
model/conditional_generation/generator_best_40.pth
model/conditional_generation/best_chemberta_tg.pth
model/conditional_generation/top10_desc_names.pkl
model/conditional_generation/tg_scaler.pkl
model/conditional_generation/ChemBerta/
model/reactiont5-retrosynthesis/
```

For the server deployment, copy runtime assets from the currently active
`polyprop` deployment directory into `/data/lzq/gith/nexpoly`:

```text
backend/data
model
online_retrieval/data
.env.ai, if present
```

Treat the existing `polyprop` deployment as read-only during this copy.

## Start

Create `/data/lzq/gith/nexpoly/.env`:

```bash
NEXPOLY_WEB_PORT=9000
NEXPOLY_GPU_DEVICE=2
GEN_MODEL_ENABLED=true
RETRO_MODEL_ENABLED=true
RETRO_MODEL_ID=/app/model/reactiont5-retrosynthesis
RETRO_DEVICE=auto
```

Build and start nexpoly:

```bash
cd /data/lzq/gith/nexpoly
docker compose build
docker compose up -d --remove-orphans
```

If another service occupies port `9000`, choose a different host port:

```bash
NEXPOLY_WEB_PORT=9100 docker compose up -d --remove-orphans
```

## Health Checks

Check the new nexpoly deployment:

```bash
docker compose ps
docker compose config --images
curl http://localhost:9000/health
```

Confirm the old polyprop deployment is still running separately:

```bash
docker compose ls
curl http://localhost:10000/health
```

## Package

Create a release bundle:

```bash
scripts/package_release.sh
```

By default, database files are excluded to keep the package small. To bundle the
prepared databases too:

```bash
INCLUDE_DATA=1 scripts/package_release.sh
```

The package is written to `release/nexpoly-release-*.tar.gz`.
Model artifacts are included by default. Database files are included only when
`INCLUDE_DATA=1`.

## Configuration

Runtime environment variables can be adjusted in `.env` or `docker-compose.yml`:

| Variable | Purpose |
| --- | --- |
| `SQLITE_DB_PATH` | Main polymer and knowledge database path inside the container. |
| `FUMOL_DB_PATH` | DFT conformation database path inside the container. |
| `MODEL_DIR` | Prediction model directory inside the container. |
| `MODEL_ENABLED` | Enables or disables prediction endpoints. |
| `GEN_MODEL_ENABLED` | Enables or disables conditional generation. |
| `RETRO_MODEL_ENABLED` | Enables or disables monomer retrosynthesis. |
| `ALLOWED_ORIGINS` | Browser origins accepted by the API. |
| `NEXPOLY_WEB_PORT` | Host port used by Nginx. Defaults to `9000`. |
| `NEXPOLY_GPU_DEVICE` | Host GPU exposed to the backend. Defaults to `2`. |

Online retrieval is exposed under the same origin at:

```text
/online-retrieval/
```

This path is used by the knowledge search module's Online tab and is proxied by
Nginx to the `online-retrieval` service.
