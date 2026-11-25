"""Prepare a read-only tooling overlay for the reviewed November workspace runtime."""

import hashlib
import json
from pathlib import Path
import re
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


def reviewed_image(image):
    if image == IMAGE:
        return
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
        raise ValueError("Expected immutable development image ID")
    # A release may have a new digest for its build metadata alone. Retain
    # the original scoped-runtime pin for changes to executable code/config.
    original, candidate = json.loads(subprocess.check_output(["docker", "image", "inspect", IMAGE, image]))

    def runtime(data):
        config = dict(data["Config"])
        config.pop("Labels", None)
        return data["RootFS"], data["Architecture"], data["Os"], data.get("Variant"), config

    if runtime(original) != runtime(candidate):
        raise ValueError("The scoped workspace requires an explicitly reviewed runtime image")


def model_settings(fixture=None):
    protocols = {"m4local": "@ai-sdk/openai-compatible", "m4local-responses": "@ai-sdk/openai"}
    providers = {
        name: {
            "npm": sdk,
            "name": "Qwen2.5-7B · " + ("Responses" if name.endswith("-responses") else "Chat Completions"),
            "options": {"baseURL": "http://m4-model.invalid/v1", "apiKey": "local-transport", "timeout": 300000},
            "models": {"qwen2.5-7b": {"name": "Qwen2.5-7B", "limit": {"context": 16384, "output": 12}}},
        } for name, sdk in protocols.items()
    }
    if fixture is not None:
        providers["m0fixture"] = fixture
    # The November runtime titles each turn via the global small_model, including
    # deterministic M3 tool calls. Only the explicit verification stack uses a local title model.
    main = "m4local/qwen2.5-7b"
    return dict(model=main, small_model="m0fixture/m0" if fixture is not None else main,
                enabled_providers=list(providers), provider=providers)


def prepare(root, registry, adapters=False, m4=False, verify_retries=False):
    image = json.loads((registry / "registry.json").read_text())["image"]
    reviewed_image(image)
    root.mkdir()
    cid = subprocess.check_output(
        [
            "docker",
            "create",
            "--pull=never",
            "--network",
            "none",
            "--entrypoint",
            "/bin/true",
            image,
        ],
        text=True,
    ).strip()
    try:
        sources = dict(SOURCES)
        if m4:
            sources.update(
                llm=(
                    "session/llm.ts",
                    "0c6478cbf5e4efbc87dade8c75105de339955f25a01a0023593ee8af8b2e8a62",
                ),
                provider=(
                    "provider/provider.ts",
                    "9cd397849a731c6c10166e05f1656eeb40f7ada89a794579db453c92b948644c",
                ),
            )
        for name, (src, digest) in sources.items():
            file = root / (name + ".ts")
            subprocess.run(
                ["docker", "cp", cid + ":/app/backend/cli/src/" + src, str(file)],
                check=True,
            )
            if hashlib.sha256(file.read_bytes()).hexdigest() != digest:
                raise ValueError("Unrecognized M1 " + name)
            source = file.read_text()
            if name == "server":
                source = 'import { withToolTurn } from "/m3-code/instance.mjs"\n' + anchor(
                    source,
                    "        .use(devBoundary)",
                    "        .use(devBoundary)\n        .use(withToolTurn)",
                )
                if adapters:
                    source = 'import { draftMessages, draftProof, latestUser } from "/m3-code/draft-bridge.ts"\n' + source
                    source = anchor(source, "        .use(withToolTurn)",
                                    '        .use(withToolTurn)\n        .post("/nexpoly/private/draft-evidence/:adapter", draftProof)')
                    # Both adapters use the same canonical lookup and receipt semantics.
                    get_messages = """getMessages: async (sessionID) => {
            await Session.get(sessionID)
            return Session.messages({ sessionID, limit: 1000 })
          }"""
                    latest_user = """const messages = await Session.messages({ sessionID, limit: 1000 })
            const user = messages.findLast(message => message.info.role === "user")?.info"""
                    if source.count(get_messages) != 2 or source.count(latest_user) != 4:
                        raise ValueError("Unexpected November draft history hooks")
                    source = source.replace(get_messages, "getMessages: draftMessages")
                    source = source.replace(latest_user, "const user = await latestUser(sessionID)")
            elif name == "llm":
                source = 'import { modelHeaders } from "/m3-code/instance.mjs"\n' + anchor(
                    source,
                    "        ...input.model.headers,\n        ...headers,",
                    '        ...input.model.headers,\n        ...headers,\n        ...(["m4local", "m4local-responses"].includes(input.model.providerID) ? modelHeaders(input.sessionID, input.user.id) : {}),',
                )
            elif name == "provider":
                source = 'import { modelFetch } from "/m3-code/instance.mjs"\n' + anchor(
                    source,
                    '      const customFetch = options["fetch"]',
                    '      const customFetch = ["m4local", "m4local-responses"].includes(model.providerID) ? modelFetch : options["fetch"]',
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
        if adapters:
            bridge = root / "capability-form-bridge.mjs"
            subprocess.run(
                [
                    "docker",
                    "cp",
                    cid + ":/app/backend/cli/src/server/capability-form-bridge.mjs",
                    str(bridge),
                ],
                check=True,
            )
            if (
                hashlib.sha256(bridge.read_bytes()).hexdigest()
                != "1a4f06aa1cf13e56f409db4bd9aa11c04cb9f9c6df9f70931e4e8ee8be883889"
            ):
                raise ValueError("Unexpected November confirmation bridge")
            source = 'import { formTool } from "/m3-code/instance.mjs"\n' + bridge.read_text()
            source = anchor(
                source,
                "callMcp = createCapabilityUiMcpCaller(adapter)",
                """callMcp = async (name,args) => {
    const result = await formTool(name,args)
    const output = result?.structuredContent
    if (result?.isError || !output?.ok) {
      const error = output?.error
      throw new BridgeError(error?.code || "DRAFT_UNAVAILABLE",error?.message || "Draft unavailable",
        [400,401,403,404,409,422,429,503].includes(error?.http_status) ? error.http_status : 409)
    }
    return output
  }""",
            )
            # Two tabs and the parameter card may read the same draft concurrently.
            # The adapter serializes local reconciliation. Only mutating form actions
            # need the bridge lock that protects persistence of the conversation receipt.
            source = anchor(
                source,
                '      if (active.has(lock)) throw new BridgeError("DRAFT_BUSY", "A draft action is already in progress for this conversation.", 409)\n      active.add(lock)\n      acquired = true',
                """      if (body.action !== "get") {
        if (active.has(lock)) throw new BridgeError("DRAFT_BUSY", "A draft action is already in progress for this conversation.", 409)
        active.add(lock)
        acquired = true
      }""",
            )
            source = anchor(source, "getMessages(body.session_id)", "getMessages(adapter, body)")
            bridge.write_text(source + "\nexport { draftEvidence, matchingReceipt }\n")
            shutil.copyfile(HERE / "draft-bridge.ts", root / "draft-bridge.ts")
    finally:
        subprocess.run(["docker", "rm", cid], check=True, stdout=subprocess.DEVNULL)
    for name in ["model.py", "instance.mjs", "draft-history.mjs"]:
        shutil.copyfile(HERE / name, root / name)
    config = json.loads((registry / "config.json").read_text())
    config["permission"].update(
        {
            "capability_gateway_*": "allow",
            "mcp": {
                "*": "deny",
                "capability_gateway_*": "allow",
                "capability_gateway_dft_job_submit": "ask",
            },
        }
    )
    if m4:
        # Qwen always uses its delegated Unix socket. The isolated retry drill
        # also keeps the deterministic, loopback-only M3 tool-selection model.
        if not verify_retries:
            (root / "model.py").write_text(
                "import asyncio, runpy\nasyncio.run(runpy.run_path('/m2-loopback.py')['main']())\n"
            )
        fixture = config["provider"]["m0fixture"] if verify_retries else None
        config.update(model_settings(fixture))
        for tool in [
            "knowledge_service_kb_search",
            "knowledge_service_kb_read_citation",
        ]:
            config["permission"][tool] = "allow"
            config["permission"]["mcp"][tool] = "allow"
    (root / "config.json").write_text(json.dumps(config))
    if image != IMAGE:
        (root / "workspace-image").write_text(image + "\n")
    root.chmod(0o755)
    for f in root.iterdir():
        f.chmod(0o444)
