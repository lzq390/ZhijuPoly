"""Prepare a read-only M3 overlay from the exact retained M1 image."""

import hashlib
import json
from pathlib import Path
import shutil
import subprocess

HERE = Path(__file__).resolve().parent
IMAGE = "sha256:91c29f44773ef3e82ba7cdbd1a457cc5c0d7065975d4918ae47e21a3e6332859"
SOURCES = {
    "prompt": ("session/prompt.ts", "0b8c811b1872384f2ca22a576eb655fca8eb468a5d31aae307cb1b82f66704a1"),
    "server": ("server/server.ts", "8391babd70cd487e430851c9df7364d1e526462bc498cebf62f23a0ccf2171e5"),
}


def anchor(source, old, new):
    if source.count(old) != 1:
        raise ValueError("Unexpected OpenScience source anchor")
    return source.replace(old, new)


def prepare(root, registry):
    if json.loads((registry / "registry.json").read_text())["image"] != IMAGE:
        raise ValueError("The M3 overlay requires the verified M1 image")
    root.mkdir()
    cid = subprocess.check_output(
        ["docker", "create", "--pull=never", "--network", "none", "--entrypoint", "/bin/true", IMAGE],
        text=True,
    ).strip()
    try:
        for name, (src, digest) in SOURCES.items():
            file = root / (name + ".ts")
            subprocess.run(["docker", "cp", cid + ":/app/backend/cli/src/" + src, str(file)], check=True)
            if hashlib.sha256(file.read_bytes()).hexdigest() != digest:
                raise ValueError("Unrecognized M1 " + name)
            source = file.read_text()
            if name == "server":
                source = 'import { withToolTurn } from "/m3-code/instance.mjs"\n' + anchor(
                    source,
                    "        .use(devBoundary)",
                    "        .use(devBoundary)\n        .use(withToolTurn)",
                )
            else:
                source = 'import { rememberTurn, toolsForTurn } from "/m3-code/instance.mjs"\n' + source
                source = anchor(
                    source,
                    "    await Session.touch(input.sessionID)\n\n    // this is backwards",
                    "    rememberTurn(input.sessionID, message.info.id)\n    await Session.touch(input.sessionID)\n\n    // this is backwards",
                )
                source = anchor(
                    source,
                    "const mcp = await MCP.tools(input.session.id)",
                    "const mcp = { ...await MCP.tools(input.session.id), ...await toolsForTurn(input, jsonSchema) }",
                )
                source = anchor(
                    source,
                    '        await ctx.ask({\n          permission: "mcp",\n          metadata: {},',
                    """        const approval = key === "capability_gateway_dft_job_submit" && item.prepare ? await item.prepare(args, opts) : null
        await ctx.ask({
          permission: "mcp",
          metadata: approval ? { operation_id: approval.operation_id, arguments: args } : {},""",
                )
            file.write_text(source)
    finally:
        subprocess.run(["docker", "rm", cid], check=True, stdout=subprocess.DEVNULL)
    for name in ["model.py", "instance.mjs"]:
        shutil.copyfile(HERE / name, root / name)
    config = json.loads((registry / "config.json").read_text())
    config["permission"].update(
        {
            "capability_gateway_*": "allow",
            "mcp": {"*": "deny", "capability_gateway_*": "allow", "capability_gateway_dft_job_submit": "ask"},
        }
    )
    (root / "config.json").write_text(json.dumps(config))
    root.chmod(0o755)
    for f in root.iterdir():
        f.chmod(0o444)
