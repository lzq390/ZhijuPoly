# PolyProp Deployment

This deployment runs the application as three containers:

- `backend`: FastAPI service on port `8000` inside the Docker network.
- `online-retrieval`: Flask/Gunicorn online literature retrieval service on port `5002` inside the Docker network.
- `nginx`: Nginx static site on host port `9000`, with `/api` proxied to the backend.

Runtime databases are mounted from `backend/data` and are not baked into the image.
Online retrieval history is mounted from `online_retrieval/data`.
The Compose project name is fixed as `polyprop`, so this deployment replaces the existing `polyprop-nginx-1` container that serves `9000:80`. It does not bind the server's port `80`.

## Required Runtime Files

Prepare these files on the deployment host before starting Docker Compose:

```text
backend/data/polyprop.db
backend/data/fumol.db
```

The model files under `model/` are copied into the backend image during build.

## Start

```bash
docker compose build
docker compose up -d --remove-orphans
```

Open:

```text
http://localhost:9000
```

If another local service already occupies port `9000`, choose a different host port:

```bash
POLYPROP_WEB_PORT=9100 docker compose up -d --remove-orphans
```

Health check:

```bash
docker compose ps
curl http://localhost:9000/health
```

## Package

Create a release bundle:

```bash
scripts/package_release.sh
```

By default, database files are excluded to keep the package small. To bundle the prepared databases too:

```bash
INCLUDE_DATA=1 scripts/package_release.sh
```

The package is written to `release/polyprop-release-*.tar.gz`.

## Configuration

Runtime environment variables can be adjusted in `docker-compose.yml`:

| Variable | Purpose |
| --- | --- |
| `SQLITE_DB_PATH` | Main polymer and knowledge database path inside the container. |
| `FUMOL_DB_PATH` | DFT conformation database path inside the container. |
| `MODEL_DIR` | Prediction model directory inside the container. |
| `MODEL_ENABLED` | Enables or disables prediction endpoints. |
| `ALLOWED_ORIGINS` | Browser origins accepted by the API. |
| `POLYPROP_WEB_PORT` | Host port used by Nginx. Defaults to `9000`. |

Online retrieval is exposed under the same origin at:

```text
/online-retrieval/
```

This path is used by the knowledge search module's Online tab and is proxied by Nginx to the `online-retrieval` service.
