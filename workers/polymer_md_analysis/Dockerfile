# Frozen Linux/amd64 CPython 3.11.17; scientific dependencies are installed in
# this container rather than loading host glibc/venv binaries across ABIs.
ARG PYTHON_BASE=python:3.11.17-slim-bookworm@sha256:8fb0b110b022c70cbe09d3ff1fb313654f31f9c814abb0d48180af56d906a199
FROM ${PYTHON_BASE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 \
    OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 \
    CUDA_VISIBLE_DEVICES="" NVIDIA_VISIBLE_DEVICES=none
COPY requirements-runtime.lock /opt/nexpoly-analysis/requirements.lock
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
RUN python -m pip install --no-cache-dir --require-hashes --only-binary=:all: -r /opt/nexpoly-analysis/requirements.lock \
    && python -I -B -c 'import importlib.metadata,json,platform,sys; import mdtraj,numpy,pandas,scipy,netCDF4; names=("fastapi","pydantic","httpx","uvicorn","numpy","pandas","scipy","mdtraj"); value={"python":sys.version.split()[0],"libc":platform.libc_ver(),"dependencies":{n:importlib.metadata.version(n) for n in names}}; open("/opt/nexpoly-analysis/runtime.json","w").write(json.dumps(value,sort_keys=True)+"\n")'
USER 1001:1001
WORKDIR /tmp
ENTRYPOINT ["/usr/local/bin/python3.11", "-I", "-B"]
