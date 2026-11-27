"""Run the pinned login API on a recovery host's private, loopback-only Docker guest."""

from pathlib import Path
import os
import re
import shlex
import stat
import subprocess
import tarfile
import tempfile
import time


class RecoveryAuthority:
    def __init__(self, stack, vm, transport, credentials, repository, image):
        self.stack = stack
        self.image = image
        for path in [vm, vm / "key", vm / "known_hosts", vm / "port"]:
            info = path.lstat()
            if (info.st_uid != os.getuid() or info.st_mode & 0o077
                    or not (stat.S_ISDIR(info.st_mode) if path == vm else stat.S_ISREG(info.st_mode))):
                raise ValueError("Expected the operator-owned, private recovery guest pins")
        port = int((vm / "port").read_text())
        if not 1024 <= port <= 65535 or port == 9011:
            raise ValueError("Unexpected recovery guest SSH port")
        self.ssh = ["ssh", "-F", "/dev/null", "-T", "-a", "-x", "-p", str(port), "-i", str(vm / "key"),
                    "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                    "-o", "UpdateHostKeys=no", "-o", "GlobalKnownHostsFile=/dev/null",
                    "-o", "UserKnownHostsFile=" + str(vm / "known_hosts"), "-o", "ConnectTimeout=10"]
        self.peer = "root@127.0.0.1"
        if self.command("docker", "image", "inspect", "--format", "{{.Id}}", image) != image:
            raise ValueError("Recovery authority image pin differs")
        self.guest = self.command("mktemp", "-d", "/run/nexpoly-m6-authority-XXXXXXXX")
        if not re.fullmatch(r"/run/nexpoly-m6-authority-[A-Za-z0-9]{8}", self.guest):
            raise RuntimeError("Unexpected temporary directory on recovery guest")
        self.stack.callback(self.command, "rm", "-rf", "--", self.guest)
        self.transport = transport
        self.sources = {repository / "backend": "backend", repository / "task_usage": "task_usage",
                        credentials / "a": "a", credentials / "b": "b", credentials / "router": "router",
                        transport / "members.json": "members",
                        repository / "ops/openscience-per-user-dev/m2_database.py": "bootstrap.py"}
        with tempfile.TemporaryFile() as stream:
            with tarfile.open(fileobj=stream, mode="w") as archive:
                for path, alias in self.sources.items():
                    selected = ("app", "migrations") if alias == "backend" else ("",)
                    for relative in selected:
                        archive.add(path / relative, arcname=str(Path(alias) / relative),
                                    filter=lambda entry: None if "__pycache__" in entry.name.split("/") else entry)
            stream.seek(0)
            self.command("tar", "-xf", "-", "-C", self.guest, stdin=stream)
        # The bind files carry fresh candidate credentials; the payload runs as the deployment UID.
        self.command("mkdir", self.guest + "/pg", self.guest + "/auth")
        self.command("chown", "-R", f"{os.getuid()}:{os.getgid()}", self.guest)
        pg = self.guest + "/pg/.s.PGSQL.5432"
        api = self.guest + "/auth/auth.sock"
        self.forward = subprocess.Popen([*self.ssh, "-N", "-o", "ExitOnForwardFailure=yes",
                                         "-R", pg + ":" + str(transport / "pg/.s.PGSQL.5432"),
                                         "-L", str(transport / "auth/auth.sock") + ":" + api, self.peer],
                                        stdin=subprocess.DEVNULL)
        stack.callback(self.stop_forward)
        for _ in range(80):
            if self.forward.poll() is not None:
                raise RuntimeError("Recovery authority forwarding exited")
            if (transport / "auth/auth.sock").is_socket():
                break
            time.sleep(.25)
        else:
            raise TimeoutError("Recovery authority forwarding did not create its local socket")
        self.command("chown", f"{os.getuid()}:{os.getgid()}", pg)
        self.sources.update({transport / "pg": "pg", transport / "auth": "auth"})

    def stop_forward(self):
        self.forward.terminate()
        try:
            self.forward.wait(timeout=8)
        except subprocess.TimeoutExpired:
            self.forward.kill()
            self.forward.wait()

    def command(self, *args, stdin=None):
        return subprocess.check_output([*self.ssh, self.peer, shlex.join(args)], stdin=stdin, text=True).strip()

    def docker_args(self, args):
        if (self.image not in args or "--network" not in args or args[args.index("--network") + 1] != "none"
                or "--pull=never" not in args):
            raise ValueError("Expected the offline pinned recovery authority")
        out = list(args)
        for index, arg in enumerate(args):
            if arg == "--mount":
                fields = args[index + 1].split(",")
                if fields[0] != "type=bind" or not fields[1].startswith("src="):
                    raise ValueError("Unexpected authority bind mount")
                fields[1] = "src=" + self.guest + "/" + self.sources[Path(fields[1][4:])]
                out[index + 1] = ",".join(fields)
            if arg == "--env-file":
                path = Path(args[index + 1])
                if path.parent != self.transport or path.name not in ("bootstrap.env", "app.env"):
                    raise ValueError("Unexpected authority environment file")
                dest = self.guest + "/" + path.name
                # No credentials in process arguments or logs.
                with path.open("rb") as stream:
                    self.command("dd", "of=" + dest, "status=none", stdin=stream)
                self.command("chmod", "600", dest)
                out[index + 1] = dest
        return out

    def bootstrap(self, args):
        self.command(*self.docker_args(args))

    def start_api(self, args):
        cid = self.command(*self.docker_args(["docker", "run", "-d", "--pull=never", "--network", "none", *args]))
        if not re.fullmatch(r"[0-9a-f]{64}", cid):
            raise RuntimeError("Expected a new recovery API container ID")
        self.stack.callback(self.command, "docker", "rm", "-f", cid)
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            if self.command("docker", "inspect", "--format", "{{.State.Running}}", cid) != "true":
                raise RuntimeError("The isolated recovery authority exited")
            test = subprocess.run([*self.ssh, self.peer, shlex.join(["test", "-S", self.guest + "/auth/auth.sock"])])
            if test.returncode == 0:
                return cid
            if test.returncode != 1:
                test.check_returncode()
            time.sleep(1)
        raise TimeoutError("Recovery authority did not listen on its private socket")
