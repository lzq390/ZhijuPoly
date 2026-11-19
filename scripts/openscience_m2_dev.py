"""Run the private dev-m1 browser stack on loopback; Ctrl-C stops it and preserves its data."""

import argparse
from contextlib import ExitStack
import fcntl
import hashlib
import runpy
import re
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from uuid import UUID, uuid4

from openscience_per_user_dev import local_engine


ROOT = Path(__file__).resolve().parents[1]
OPS = ROOT / "ops/openscience-per-user-dev"
DEFAULT = Path.home() / ".local/state"
REGISTRY = DEFAULT / "nexpoly-openscience-m1-dev"
PARENT = "https://app.m2.test:18760"
AGENT = "https://agent.m2.test:18761"
API_IMAGE = "sha256:886fc49f0ad7a4a4922e9f820886a9b0f7e76829f6dfbc84a41b02fbd9a68961"
PG_IMAGE = "sha256:eb4759788a2182f08257135e61a34f2cfc3c2914079f3465d64ee62350f4d081"
UI_IMAGE = "sha256:fea8a3c294bb324a98041f8f8354448e6ca5c6502dedc565c4b884994c567bb0"
M3_MODEL_SHA = "f8a207538832b1d1ecdf80cc380194dcf8f92d13d2381042022170c217559df2"
MANAGER = ROOT / "scripts/openscience_per_user_dev.py"


def run(*args, **kw):
    return subprocess.check_output(args, text=True, **kw).strip()


def file(path, content, mode=0o600):
    path.write_text(content)
    path.chmod(mode)


def secret(path, *, pg=False):
    if not path.exists():
        file(path, secrets.token_urlsafe(40) + "\n", 0o444 if pg else 0o600)
    return path.read_text().strip()


def mount(src, dst, writable=False):
    return ["--mount", f"type=bind,src={src},dst={dst}" + ("" if writable else ",readonly")]


def wait(check, subject, timeout=45):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            if check():
                return
        except (OSError, subprocess.CalledProcessError):
            pass
        time.sleep(0.25)
    raise RuntimeError(f"{subject} did not become ready")


def launch(stack, directory, name, args, **kw):
    log = stack.enter_context((directory / (name + ".log")).open("w"))
    p = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT, **kw)

    def stop():
        if p.poll() is None:
            p.terminate()
            try:
                p.wait(18)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()

    stack.callback(stop)
    return p


def container(stack, *args):
    cid = run("docker", "run", "-d", "--pull=never", "--network", "none", *args)

    def stop():
        subprocess.run(["docker", "stop", "-t", "8", cid], check=False, stdout=subprocess.DEVNULL)
        subprocess.run(["docker", "rm", cid], check=False, stdout=subprocess.DEVNULL)

    stack.callback(stop)
    return cid


ADAPTER_IMAGES = {
    "polytao": "sha256:72eca12668d3ee9d03969d2711c750067431c26c31f95811eb9ec5c3ec7d862d",
    "retrosynthesis": "sha256:afc71c6ba92bb8595d177f04c33fb01dcf26ad9d40f9f6c19dc67c536e0fa5ed",
}


def start_adapters(stack, state, transport, api_socket, cred, deploy, verify):
    endpoints, evidence = {}, {}
    for name, image in ADAPTER_IMAGES.items():
        ipc = transport / ("m3-" + name)
        ipc.mkdir(mode=0o700)
        journal = state / "m3-adapters" / name
        journal.mkdir(mode=0o700, parents=True, exist_ok=True)
        ingress = cred / ("m3-" + name + "-ingress")
        secret(ingress)
        verification = []
        if verify:
            evidence[name] = str(ipc / "retry.json")
            verification = [
                "-e",
                "M3_ADAPTER_VERIFY_DROP_RECEIPT=1",
                "-e",
                "M3_ADAPTER_VERIFY_EVIDENCE=/ipc/retry.json",
                "-e",
                "M3_ADAPTER_SUBMITS_PER_MINUTE=1",
            ]
        cid = container(
            stack,
            "--label",
            "org.nexpoly.m2.deployment=" + deploy,
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--tmpfs",
            "/tmp:rw,nosuid,size=32m",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--memory",
            "384m",
            "--cpus",
            "1",
            *mount(ipc, "/ipc", True),
            *mount(journal, "/journal", True),
            *mount(api_socket, "/authority"),
            *mount(ROOT / "backend", "/app/backend"),
            *mount(ROOT / "task_usage", "/app/task_usage"),
            *mount(ingress, "/ingress"),
            *mount(cred / ("m3-" + name + "-service"), "/service"),
            "-e",
            "M3_ADAPTER=" + name,
            "-e",
            "M3_ADAPTER_INGRESS_FILE=/ingress",
            "-e",
            "M3_ADAPTER_AUTHORITY_FILE=/service",
            "-e",
            "M3_AUTHORITY_SOCKET=/authority/auth.sock",
            "-e",
            "M3_ADAPTER_STATE_DIR=/journal",
            "-e",
            "PYTHONPATH=/app/backend:/app",
            "-e",
            "PYTHONDONTWRITEBYTECODE=1",
            *verification,
            "--entrypoint",
            "python",
            image,
            "-m",
            "uvicorn",
            "app.auth.openscience_m3_adapters:create_app",
            "--factory",
            "--uds",
            "/ipc/adapter.sock",
            "--no-access-log",
            "--no-proxy-headers",
        )

        def ready():
            if run("docker", "inspect", "--format", "{{.State.Running}}", cid) != "true":
                raise RuntimeError(f"Private {name} adapter exited during startup")
            return (ipc / "adapter.sock").is_socket()

        wait(ready, name + " scoped adapter")
        endpoints[name] = {"socket": str(ipc / "adapter.sock"), "ingress_file": str(ingress)}
    return endpoints, evidence


def run_stack(args, stack):
    local_engine()
    if args.m3_adapters and not args.m3_gateway_image:
        raise ValueError("--m3-adapters requires the reviewed --m3-gateway-image")
    if args.verify:
        if args.verify.exists():
            raise FileExistsError(f"Choose a new verification output: {args.verify}")
        args.verify.parent.mkdir(parents=True, exist_ok=True)
    state = args.state_dir.resolve()
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    if state.stat().st_uid != os.getuid() or state.stat().st_mode & 0o077:
        raise ValueError("Use an owner-private M2 state directory (0700)")
    lock = stack.enter_context((state / "stack.lock").open("a"))
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    identifier = state / "deployment"
    if not identifier.exists():
        if set(state.iterdir()) != {state / "stack.lock"}:
            raise ValueError("A new deployment requires an empty M2 state directory")
        file(identifier, uuid4().hex)
    deploy = UUID(identifier.read_text()).hex
    # Refuse port collisions before starting dependencies or claiming workspace lifecycles.
    for port in (18760, 18761, 18762, 18763):
        with socket.socket() as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", port))
    transport = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="np-m2-")))
    logs = state / "logs"
    logs.mkdir(exist_ok=True)
    pg_socket, api_socket = transport / "pg", transport / "auth"
    pg_socket.mkdir(mode=0o700)
    pg_socket.chmod(0o1777)
    api_socket.mkdir(mode=0o700)
    web, cred = state / "ui", state / "credentials"
    cred.mkdir(mode=0o700, exist_ok=True)
    for name in ("admin", "api", "auth", "service", "router", "a", "b"):
        secret(cred / name, pg=name == "admin")
    cert, key = state / "tls.crt", state / "tls.key"
    if (
        not cert.exists()
        or not key.exists()
        or subprocess.run(
            ["openssl", "x509", "-in", cert, "-checkend", "86400", "-noout"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
        != 0
    ):
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-days",
                "30",
                "-subj",
                "/CN=OpenScience M2 development",
                "-addext",
                "subjectAltName=DNS:app.m2.test,DNS:agent.m2.test",
                "-keyout",
                key,
                "-out",
                cert,
            ],
            check=True,
            stderr=subprocess.DEVNULL,
        )
    if not web.exists():
        copy = transport / "ui"
        copy.mkdir()
        cid = run(
            "docker", "create", "--pull=never", "--network", "none", "--entrypoint", "/bin/true", UI_IMAGE
        )
        try:
            subprocess.run(["docker", "cp", cid + ":/usr/share/nginx/html/.", str(copy)], check=True)
            shutil.copytree(copy, state / "ui.partial", dirs_exist_ok=True)
            (state / "ui.partial").rename(web)
        finally:
            run("docker", "rm", cid)
    pg_volume = "nexpoly-m2-auth-" + deploy
    label = "org.nexpoly.m2.deployment"
    run("docker", "volume", "create", "--label", label + "=" + deploy, pg_volume)
    observed = run(
        "docker",
        "volume",
        "inspect",
        "--format",
        '{{index .Labels "' + label + '"}} {{.Driver}} {{json .Options}}',
        pg_volume,
    )
    if observed not in (deploy + " local null", deploy + " local {}"):
        raise RuntimeError("Unrelated PostgreSQL volume")
    hba = transport / "pg_hba.conf"
    file(hba, "local all all scram-sha-256\n", 0o444)
    pg = container(
        stack,
        "--label",
        label + "=" + deploy,
        "--memory",
        "512m",
        "--cpus",
        "1",
        *mount(pg_socket, "/m2pg", True),
        *mount(cred / "admin", "/admin-pw"),
        *mount(hba, "/m2_hba.conf"),
        "--mount",
        f"type=volume,src={pg_volume},dst=/var/lib/postgresql/data",
        "-e",
        "POSTGRES_USER=m2_admin",
        "-e",
        "POSTGRES_DB=nexpoly_m2_dev",
        "-e",
        "POSTGRES_PASSWORD_FILE=/admin-pw",
        "-e",
        "PGHOST=/m2pg",
        "-e",
        "POSTGRES_INITDB_ARGS=--auth-local=scram-sha-256",
        PG_IMAGE,
        "postgres",
        "-c",
        "listen_addresses=",
        "-c",
        "unix_socket_directories=/var/run/postgresql,/m2pg",
        "-c",
        "hba_file=/m2_hba.conf",
    )

    def pg_ready():
        # The official entrypoint uses a temporary postmaster during init; wait for PID 1.
        if run("docker", "inspect", "--format", "{{.State.Running}}", pg) != "true":
            raise RuntimeError("Private PostgreSQL container exited during startup")
        return (
            run("docker", "exec", pg, "cat", "/proc/1/comm") == "postgres"
            and (pg_socket / ".s.PGSQL.5432").exists()
        )

    wait(pg_ready, "dedicated PostgreSQL", 90)
    pg_env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("PG")},
        "PGPASSWORD": secret(cred / "admin"),
        "PGHOST": str(pg_socket),
        "PGUSER": "m2_admin",
    }
    if not run(
        "psql",
        "-XAt",
        "-d",
        "postgres",
        "-c",
        "SELECT 1 FROM pg_database WHERE datname='nexpoly_m2_dev'",
        env=pg_env,
    ):
        subprocess.run(["createdb", "nexpoly_m2_dev"], env=pg_env, check=True)

    def dsn(role):
        return "host=/m2pg dbname=nexpoly_m2_dev user=m2_" + role + " password=" + secret(cred / role)

    env = {
        "APP_POSTGRES_DSN": dsn("api"),
        "AUTH_POSTGRES_DSN": dsn("auth"),
        "APP_SERVICE_POSTGRES_DSN": dsn("service"),
        "PYTHONPATH": "/app/backend:/app",
        "PYTHONDONTWRITEBYTECODE": "1",
        "OPENSCIENCE_M2_ROUTER_TOKEN_FILE": "/m2key",
        "OPENSCIENCE_M2_PARENT_ORIGIN": PARENT,
        "OPENSCIENCE_M2_AGENT_ORIGIN": AGENT,
    }
    common = [
        "--label",
        label + "=" + deploy,
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--tmpfs",
        "/tmp:rw,nosuid,size=128m",
        "--memory",
        "6g" if args.m3_adapters else "2g",
        "--cpus",
        "2",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        *mount(ROOT / "backend", "/app/backend"),
        *mount(ROOT / "task_usage", "/app/task_usage"),
        *mount(pg_socket, "/m2pg"),
    ]

    m3_api, manager_env, tool_config = [], dict(os.environ), {}
    retry_evidence = None
    gateway = None
    if args.m3_gateway_image:
        base = "sha256:fb7fdd70e46d7544d0816f042bd0a4324cd7a6c1e86a4a4606031f5108199df0"
        image_id = args.m3_gateway_image
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
            raise ValueError("Pass the immutable image ID from the development gateway builder")

        def layers(image):
            return json.loads(
                run("docker", "image", "inspect", "--format", "{{json .RootFS.Layers}}", image)
            )

        expected = layers(base)
        if layers(image_id)[: len(expected)] != expected or image_id == base:
            raise ValueError("Expected a development gateway derived from the reviewed November image")
        if (
            run(
                "docker",
                "image",
                "inspect",
                "--format",
                '{{index .Config.Labels "org.nexpoly.purpose"}} {{index .Config.Labels "org.nexpoly.expected-base-image"}}',
                image_id,
            )
            != "openscience-per-user-development-gateway " + base
        ):
            raise ValueError("Expected the labeled M3 development gateway")
        model = args.m3_prediction_model.resolve(strict=True)
        if (
            model.name != "rf_Glass transition temperature_exp.pkl"
            or hashlib.sha256(model.read_bytes()).hexdigest() != M3_MODEL_SHA
        ):
            raise ValueError("Unexpected M3 development prediction model")
        for name in ("m3-ingress", "m3-gateway"):
            secret(cred / name)
        env.update(
            OPENSCIENCE_M3_GATEWAY_TOKEN_FILE="/m3key",
            USAGE_ENTITLEMENTS_ENABLED="true",
            USAGE_ENTITLEMENTS_ALLOW_EXAMPLE="true",
            USAGE_ENTITLEMENTS_POLICY_FILE="/m3policy.json",
            USAGE_METERING_ENABLED="true",
        )
        policy = ROOT / "config/examples/entitlements-example-v1.json"
        if args.m3_adapters:
            policy_data = json.loads(policy.read_bytes())
            policy_data["version"] = "dev-m1-adapters-v1"
            policy_data["tiers"]["member"]["pools"].update(generation=50, chemistry_small=50)
            policy = transport / "adapter-policy.json"
            file(policy, json.dumps(policy_data), 0o444)
        common += mount(policy, "/m3policy.json")
        artifacts = state / "m3-artifacts"
        artifacts.mkdir(mode=0o700, exist_ok=True)
        m3_api = [
            *mount(cred / "m3-gateway", "/m3key"),
            *mount(model, "/m3model/" + model.name),
            *mount(ROOT / "workers/monomer_dft_worker", "/app/workers/monomer_dft_worker"),
            *mount(artifacts, "/m3artifacts", True),
        ]
        if args.m3_adapters:
            env.update(
                OPENSCIENCE_M3_ADAPTERS_ENABLED="1",
                OMP_NUM_THREADS="1",
                MKL_NUM_THREADS="1",
                OPENBLAS_NUM_THREADS="1",
                HF_HUB_OFFLINE="1",
                CUDA_VISIBLE_DEVICES="",
            )
            for name in ADAPTER_IMAGES:
                adapter_key = cred / ("m3-" + name + "-service")
                secret(adapter_key)
                dest = "/m3-" + name + "-key"
                m3_api.extend(mount(adapter_key, dest))
                env["M3_" + name.upper() + "_AUTHORITY_FILE"] = dest
            for name in ("polytao", "reactiont5-retrosynthesis"):
                m3_api.extend(mount(model.parent / name, "/m3adapter-models/" + name))
            m3_api.extend(
                mount(
                    model.parent.parent / "ASSET-MANIFEST.json", "/m3adapter-models/ASSET-MANIFEST.json"
                )
            )
        overlay = transport / "m3"
        overlay.mkdir()
        generations = overlay / "generations"
        generations.mkdir(mode=0o755)
        generations.chmod(0o755)
        m3_api += mount(generations, "/m3generations")
        runpy.run_path(str(ROOT / "ops/openscience-m3-dev/workspace.py"))["prepare"](
            overlay / "code", args.registry, args.m3_adapters
        )
        manager_env["OPENSCIENCE_M3_OVERLAY"] = str(overlay)
        peer_dir = transport / "gateway"
        peer_dir.mkdir()
        tool_config = {
            "ingress_file": str(cred / "m3-ingress"),
            "gateway_socket": str(peer_dir / "gateway.sock"),
            "peer_sockets": {},
        }

    def env_file(name, values):
        dest = transport / name
        file(dest, "".join(k + "=" + v + "\n" for k, v in values.items()))
        return ["--env-file", str(dest)]

    admin = {
        **env,
        "OPENSCIENCE_M2_ADMIN_DSN": dsn("admin"),
        "OPENSCIENCE_M2_PASSWORD_A_FILE": "/m2a",
        "OPENSCIENCE_M2_PASSWORD_B_FILE": "/m2b",
    }
    bootstrap = [
        "docker",
        "run",
        "--rm",
        "--pull=never",
        "--network",
        "none",
        *common,
        *mount(OPS / "m2_database.py", "/bootstrap-m2.py"),
        *mount(cred / "a", "/m2a"),
        *mount(cred / "b", "/m2b"),
        *env_file("bootstrap.env", admin),
        API_IMAGE,
        "python",
    ]
    subprocess.run([*bootstrap, "/bootstrap-m2.py"], check=True)
    subprocess.run([*bootstrap, "-m", "app.auth.openscience_seed"], check=True)
    if args.m3_gateway_image:
        subprocess.run(
            [
                *bootstrap,
                "-m",
                "app.entitlement_cli",
                "install-policy",
                "--file",
                "/m3policy.json",
                "--allow-example",
                "--apply",
            ],
            check=True,
        )
        gateway = container(
            stack,
            "--label",
            label + "=" + deploy,
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--memory",
            "384m",
            "--cpus",
            "1",
            *mount(peer_dir, "/ipc", True),
            *mount(api_socket, "/nexpoly"),
            *mount(cred / "m3-ingress", "/ingress"),
            *mount(cred / "m3-gateway", "/gw"),
            "-e",
            "OPENSCIENCE_M3_INGRESS_KEY_FILE=/ingress",
            "-e",
            "OPENSCIENCE_M3_GATEWAY_KEY_FILE=/gw",
            "-e",
            "OPENSCIENCE_M3_PARENT_ORIGIN=" + PARENT,
            args.m3_gateway_image,
        )
        wait(lambda: (peer_dir / "gateway.sock").is_socket(), "isolated capability gateway")
        if args.m3_adapters:
            tool_config["adapters"], adapter_evidence = start_adapters(
                stack, state, transport, api_socket, cred, deploy, args.verify
            )
        if args.verify and not args.m3_adapters:
            retry_evidence = transport / "retry.json"
            replay = transport / "retry.sock"
            launch(
                stack,
                logs,
                "m3-retry",
                [
                    shutil.which("node"),
                    str(ROOT / "scripts/ci/openscience_m3_retry.mjs"),
                    "--listen",
                    str(replay),
                    "--upstream",
                    tool_config["gateway_socket"],
                    "--evidence",
                    str(retry_evidence),
                ],
            )
            wait(replay.is_socket, "private M3 retry verifier")
            tool_config["gateway_socket"] = str(replay)
    api = container(
        stack,
        *common,
        *m3_api,
        *mount(cred / "router", "/m2key"),
        *mount(api_socket, "/ipc", True),
        *env_file("app.env", env),
        API_IMAGE,
        "python",
        "-m",
        "uvicorn",
        "app.auth.openscience_dev:app",
        "--uds",
        "/ipc/auth.sock",
        "--no-access-log",
        "--no-proxy-headers",
    )
    wait(lambda: (api_socket / "auth.sock").is_socket(), "M2 authority")
    before = json.loads(run(sys.executable, str(MANAGER), "--state-dir", str(args.registry), "list"))[
        "workspaces"
    ]
    retained = {
        b["user_id"]: b
        for b in before
        if b["environment"] == "dev-m1" and b["user_id"] in ("test-user-a", "test-user-b")
    }
    if len(retained) != 2:
        raise RuntimeError("Both retained dev-m1 bindings are required")

    if args.m3_gateway_image:
        if (
            any(v["state"] != "recycled" for v in retained.values())
            or (args.registry / "supervisor.sock").exists()
        ):
            raise RuntimeError(
                "M3 needs the retained development workspaces offline to attach its scoped transports"
            )
        for user, row in retained.items():
            directory = overlay / row["workspace"]
            directory.mkdir(mode=0o755)
            directory.chmod(0o755)
            tool_config["peer_sockets"][user] = str(directory / "tools.sock")

    def recycle():
        for u, original in retained.items():
            if original["state"] == "recycled":
                result = json.loads(
                    run(
                        sys.executable,
                        str(MANAGER),
                        "--state-dir",
                        str(args.registry),
                        "recycle",
                        "--environment",
                        "dev-m1",
                        "--user-id",
                        u,
                        env=manager_env,
                    )
                )
                if result["state"] != "recycled" or result["volume"] != original["volume"]:
                    raise RuntimeError(
                        "Workspace did not finish draining with its original private volume"
                    )

    stack.callback(recycle)
    supervisor_socket = args.registry / "supervisor.sock"
    if not supervisor_socket.exists():
        launch(
            stack,
            logs,
            "supervisor",
            [
                sys.executable,
                str(ROOT / "scripts/openscience_per_user_service.py"),
                "--state-dir",
                str(args.registry),
            ],
            env=manager_env,
        )
        wait(supervisor_socket.is_socket, "M1 supervisor")
    config = {
        "listen_port": 18761,
        "public_origin": AGENT,
        "parent_origins": [PARENT],
        "authority_url": "http://127.0.0.1:18762",
        "service_token_file": str(cred / "router"),
        "supervisor_socket": str(supervisor_socket),
        "ui_root": str(web),
        "environment": "dev-m1",
        "tls_key_file": str(key),
        "tls_cert_file": str(cert),
    }
    if args.m3_gateway_image:
        config["m3"] = tool_config
    config_file = transport / "router.json"
    file(config_file, json.dumps(config))
    node = shutil.which("node")
    launch(stack, logs, "router", [node, str(OPS / "m2-router.mjs"), "--config", str(config_file)])
    frontend = ROOT / "frontend"
    launch(
        stack,
        logs,
        "frontend",
        [
            node,
            str(frontend / "node_modules/vite/bin/vite.js"),
            "--host",
            "127.0.0.1",
            "--port",
            "18763",
            "--strictPort",
            "--mode",
            "m2",
        ],
        cwd=frontend,
        env={
            **os.environ,
            "VITE_OPENSCIENCE_PER_USER": "true",
            "VITE_SERVER_MONITORING_ENABLED": "false",
            "VITE_OPENSCIENCE_ROUTER_URL": AGENT + "/",
            "DEV_PROXY_TARGET": "http://127.0.0.1:18762",
            "VITE_API_BASE_URL": "/api/v1",
        },
    )
    conf = transport / "nginx.conf"
    file(
        conf,
        f"""daemon off;
pid {transport}/nginx.pid;
error_log {logs}/nginx-error.log warn;
events {{ worker_connections 256; }}
http {{
  access_log off;
  client_body_temp_path {transport}/body;
  proxy_temp_path {transport}/proxy;
  fastcgi_temp_path {transport}/fastcgi;
  uwsgi_temp_path {transport}/uwsgi;
  scgi_temp_path {transport}/scgi;
  server {{
    listen 127.0.0.1:18762;
    location / {{ proxy_pass http://unix:{api_socket}/auth.sock:; }}
  }}
  server {{
    listen 127.0.0.1:18760 ssl;
    server_name app.m2.test;
    ssl_certificate {cert};
    ssl_certificate_key {key};
    location /api/ {{ proxy_pass http://unix:{api_socket}/auth.sock:; proxy_set_header Host $http_host; }}
    location /internal/ {{ return 404; }}
    location / {{ proxy_pass http://127.0.0.1:18763; proxy_set_header Host 127.0.0.1; }}
  }}
}}
""",
    )
    launch(stack, logs, "nginx", ["nginx", "-p", str(transport), "-c", str(conf)])
    loopback_http = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    wait(
        lambda: loopback_http.open("http://127.0.0.1:18762/api/v1/auth/session", timeout=2).status == 200,
        "login API via Unix socket",
    )
    wait(
        lambda: loopback_http.open("http://127.0.0.1:18763", timeout=2).status == 200, "NexPoly frontend"
    )

    def listening():
        with socket.create_connection(("127.0.0.1", 18761), timeout=1):
            return True

    wait(listening, "HTTPS agent router")
    ready = {
        "state_dir": str(state),
        "registry": str(args.registry),
        "pg": pg,
        "api": api,
        "deployment": deploy,
        "parent": PARENT,
        "agent": AGENT,
        "user_volumes": {k: v["volume"] for k, v in retained.items()},
    }
    if args.m3_gateway_image:
        ready.update(gateway=gateway, gateway_image=args.m3_gateway_image)
        if args.m3_adapters:
            ready.update(
                adapter_images=ADAPTER_IMAGES,
                model_manifest_sha256=hashlib.sha256(
                    (model.parent.parent / "ASSET-MANIFEST.json").read_bytes()
                ).hexdigest(),
                adapter_retry_evidence=adapter_evidence,
            )
        if retry_evidence:
            ready["retry_evidence"] = str(retry_evidence)
    info = state / "running.json"
    file(info, json.dumps(ready))
    stack.callback(info.unlink, missing_ok=True)
    phase = "M3" if args.m3_gateway_image else "M2"
    print(f"{phase} ready. Browser origins:", PARENT, AGENT, flush=True)
    if args.verify:
        subprocess.run(
            [
                node,
                str(
                    ROOT
                    / "scripts/ci"
                    / (
                        "verify_openscience_m3_adapters.mjs"
                        if args.m3_adapters
                        else "verify_openscience_m3.mjs"
                        if args.m3_gateway_image
                        else "verify_openscience_m2_browser.mjs"
                    )
                ),
                "--state",
                str(state),
                "--output",
                str(args.verify),
            ],
            check=True,
        )
    else:
        while True:
            signal.pause()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT / "nexpoly-openscience-m2-dev")
    parser.add_argument("--registry", type=Path, default=REGISTRY)
    parser.add_argument(
        "--verify", type=Path, help="Run browser acceptance and write a new JSON report, then stop."
    )
    parser.add_argument(
        "--m3-gateway-image", help="Enable the six-tool M3 pilot with this reviewed derived image ID"
    )
    parser.add_argument(
        "--m3-prediction-model",
        type=Path,
        default=Path(
            "/data/lzq/nexpoly-assets/releases/ad19a4f1cb954b3ee6999b7157c798fd887ecd3fd7ae12e40ac20a97637575e2/model/rf_Glass transition temperature_exp.pkl"
        ),
    )
    parser.add_argument(
        "--m3-adapters",
        action="store_true",
        help="Enable private PolyTAO and retrosynthesis MCP adapters",
    )
    args = parser.parse_args()
    os.umask(0o077)

    def stop(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    with ExitStack() as stack:
        try:
            run_stack(args, stack)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
