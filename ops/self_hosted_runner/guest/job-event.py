#!/usr/bin/env python3
"""Untrusted guest hints. The controller must verify them against GitHub."""
import json
import os
import sys

event = sys.argv[1]
if event not in ("started", "completed"):
    raise SystemExit(2)
payload = {"event": event}
for key, env_name in (("run_id", "GITHUB_RUN_ID"), ("run_attempt", "GITHUB_RUN_ATTEMPT")):
    value = os.environ.get(env_name, "")
    if value.isdecimal() and len(value) <= 24:
        payload[key] = int(value)
try:
    fd = os.open("/dev/virtio-ports/org.nexpoly.runner.events", os.O_WRONLY | os.O_NONBLOCK)
    try:
        os.write(fd, (json.dumps(payload) + "\n").encode())
    finally:
        os.close(fd)
except OSError:
    pass
