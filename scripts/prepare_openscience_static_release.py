#!/usr/bin/env python3
"""Prepare a static UI release that keeps cached predecessor module graphs usable."""
from __future__ import annotations

import argparse
from collections import deque
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
from urllib.parse import unquote, urlsplit


IDENTIFIER = re.compile(r"[A-Za-z_$][\w$]*")


class ReleaseError(ValueError):
    pass


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def ordinary_directory(path: Path) -> Path:
    path = Path(os.path.abspath(path))
    if path.resolve() != path or not path.is_dir():
        raise ReleaseError("Static roots and output parents must be ordinary directories without symlinks")
    return path


def inventory(root: Path) -> dict[str, bytes]:
    root = ordinary_directory(root)
    files = {}
    for parent, directories, names in os.walk(root, followlinks=False):
        for name in [*directories, *names]:
            path = Path(parent) / name
            mode = path.lstat().st_mode
            if not stat.S_ISDIR(mode) and not stat.S_ISREG(mode):
                raise ReleaseError("Static trees cannot contain symlinks or nonregular files")
        for name in names:
            path = Path(parent) / name
            files[path.relative_to(root).as_posix()] = path.read_bytes()
    if "index.html" not in files:
        raise ReleaseError("Every release must contain index.html")
    return files


class IndexParser(HTMLParser):
    def __init__(self, payload: bytes):
        super().__init__(convert_charrefs=True)
        self.resources: list[tuple[str, bool]] = []
        self.module_entries: list[str] = []
        self.importmap: dict = {"imports": {}, "scopes": {}}
        self._map = None
        try:
            self.feed(payload.decode("utf-8"))
            self.close()
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReleaseError("Release HTML or importmap is invalid") from exc
        if self._map is not None:
            raise ReleaseError("Release importmap script is not closed")

    def handle_starttag(self, tag, attributes):
        values = dict(attributes)
        if tag == "script":
            if values.get("type") == "importmap":
                if values.get("src") or self._map is not None:
                    raise ReleaseError("Importmaps must be inline and unambiguous")
                self._map = []
            if values.get("src"):
                module = values.get("type") == "module"
                self.resources.append((values["src"], module))
                if module:
                    self.module_entries.append(values["src"])
        if tag == "link" and values.get("href"):
            relations = (values.get("rel") or "").split()
            if set(relations) & {"modulepreload", "stylesheet", "preload"}:
                self.resources.append((values["href"], "modulepreload" in relations))

    def handle_data(self, value):
        if self._map is not None:
            self._map.append(value)

    def handle_endtag(self, tag):
        if tag != "script" or self._map is None:
            return
        document = json.loads("".join(self._map))
        self._map = None
        if not isinstance(document, dict):
            raise ReleaseError("Importmap must be an object")
        imports, scopes = document.get("imports", {}), document.get("scopes", {})
        if not isinstance(imports, dict) or not isinstance(scopes, dict):
            raise ReleaseError("Importmap imports and scopes must be objects")
        mappings = [imports, *scopes.values()]
        if any(not isinstance(mapping, dict) or any(not isinstance(key, str) or not isinstance(value, str)
                                                   for key, value in mapping.items()) for mapping in mappings):
            raise ReleaseError("Importmap mappings must contain string URLs")
        if set(self.importmap["imports"]) & set(imports) or set(self.importmap["scopes"]) & set(scopes):
            raise ReleaseError("Duplicate importmap definitions are ambiguous")
        self.importmap["imports"].update(imports)
        self.importmap["scopes"].update(scopes)


def js_tokens(source: str):
    """Tokenize enough module syntax to avoid interpreting strings/comments as imports."""
    cursor = 0
    previous = None
    regex_after = {None, "(", "[", "{", "=", ":", ",", ";", "!", "?", "return", "=>", "||", "&&"}
    while cursor < len(source):
        character = source[cursor]
        if character.isspace():
            cursor += 1
            continue
        if source.startswith("//", cursor):
            newline = source.find("\n", cursor + 2)
            cursor = len(source) if newline < 0 else newline + 1
            continue
        if source.startswith("/*", cursor):
            end = source.find("*/", cursor + 2)
            if end < 0:
                raise ReleaseError("Unterminated JavaScript comment")
            cursor = end + 2
            continue
        if character in "\"'`":
            quote, start = character, cursor + 1
            cursor += 1
            while cursor < len(source):
                if source[cursor] == "\\":
                    cursor += 2
                    continue
                if source[cursor] == quote:
                    break
                cursor += 1
            if cursor >= len(source):
                raise ReleaseError("Unterminated JavaScript string")
            value = source[start:cursor]
            cursor += 1
            if quote != "`":
                value = re.sub(r"\\([\\/'\"])", r"\1", value)
                yield "string", value
            previous = "string"
            continue
        if character == "/" and previous in regex_after:
            cursor += 1
            in_class = False
            while cursor < len(source):
                if source[cursor] == "\\":
                    cursor += 2
                    continue
                if source[cursor] == "[":
                    in_class = True
                elif source[cursor] == "]":
                    in_class = False
                elif source[cursor] == "/" and not in_class:
                    cursor += 1
                    while cursor < len(source) and source[cursor].isalpha():
                        cursor += 1
                    break
                cursor += 1
            previous = "regex"
            continue
        identifier = IDENTIFIER.match(source, cursor)
        if identifier:
            value = identifier.group()
            cursor += len(value)
            yield "identifier", value
        else:
            value = source[cursor:cursor + 2] if source[cursor:cursor + 2] in {"=>", "||", "&&"} else character
            cursor += len(value)
            yield "punctuation", value
        previous = value


def module_imports(source: str) -> list[str]:
    tokens = list(js_tokens(source))
    imports = []
    for offset, token in enumerate(tokens):
        if token not in {("identifier", "import"), ("identifier", "export")}:
            continue
        if offset and tokens[offset - 1][1] == ".":
            continue
        following = tokens[offset + 1:]
        if not following:
            continue
        if token[1] == "import" and following[0][0] == "string":
            imports.append(following[0][1])
            continue
        if following[0][1] == "(":
            if token[1] == "import" and len(following) > 1 and following[1][0] == "string":
                imports.append(following[1][1])
            continue
        for index, item in enumerate(following):
            if item[1] == ";":
                break
            if item == ("identifier", "from") and index + 1 < len(following) and following[index + 1][0] == "string":
                imports.append(following[index + 1][1])
                break
    return imports


def local_url(specifier: str, importer: str = "index.html") -> str | None:
    parsed = urlsplit(specifier)
    if parsed.scheme or parsed.netloc or specifier.startswith("//"):
        return None
    decoded = unquote(parsed.path)
    if "\\" in decoded or "\0" in decoded:
        raise ReleaseError("Asset URL contains an invalid path")
    parts = [] if decoded.startswith("/") else list(PurePosixPath(importer).parent.parts)
    for part in decoded.split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            if not parts:
                raise ReleaseError("Asset URL escapes the static root")
            parts.pop()
        else:
            parts.append(part)
    return "/".join(parts)


def mapped_import(specifier: str, importer: str, importmap: dict) -> str | None:
    bare = not specifier.startswith(("/", "./", "../")) and not urlsplit(specifier).scheme
    normalized = specifier if bare else "/" + (local_url(specifier, importer) or "")
    scopes = [(scope, mapping) for scope, mapping in importmap["scopes"].items()
              if ("/" + importer).startswith("/" + (local_url(scope) or "") + ("/" if scope.endswith("/") else ""))]
    mappings = [mapping for _, mapping in sorted(scopes, key=lambda pair: len(pair[0]), reverse=True)]
    mappings.append(importmap["imports"])
    for mapping in mappings:
        for key in sorted(mapping, key=len, reverse=True):
            normalized_key = key if not key.startswith(("/", "./", "../")) else "/" + (local_url(key) or "")
            if key.endswith("/") and not normalized_key.endswith("/"):
                normalized_key += "/"
            if normalized == normalized_key or normalized_key.endswith("/") and normalized.startswith(normalized_key):
                suffix = normalized[len(normalized_key):]
                return local_url(mapping[key] + suffix)
    if bare:
        raise ReleaseError("Unmapped bare JavaScript import: " + specifier)
    return local_url(specifier, importer)


def validate_graph(files: dict[str, bytes], html: bytes, label: str) -> dict:
    index = IndexParser(html)
    queue = deque()
    for resource, module in index.resources:
        path = local_url(resource)
        if path is not None:
            queue.append((path, module))
    visited = set()
    while queue:
        path, module = queue.popleft()
        if path not in files:
            raise ReleaseError(f"Missing release dependency ({label}): {path}")
        if (path, module) in visited:
            continue
        visited.add((path, module))
        if module and PurePosixPath(path).suffix in {".js", ".mjs"}:
            try:
                imports = module_imports(files[path].decode("utf-8"))
            except UnicodeDecodeError as exc:
                raise ReleaseError("JavaScript module is not UTF-8: " + path) from exc
            for specifier in imports:
                dependency = mapped_import(specifier, path, index.importmap)
                if dependency is not None:
                    queue.append((dependency, True))
        if PurePosixPath(path).suffix == ".css":
            css = re.sub(r"/\*.*?\*/", "", files[path].decode("utf-8"), flags=re.S)
            urls = re.findall(r"url\(\s*['\"]?([^)'\"\s]+)", css)
            urls += re.findall(r"@import\s+['\"]([^'\"]+)['\"]", css)
            for resource in urls:
                dependency = local_url(resource, path)
                if dependency is not None:
                    queue.append((dependency, False))
    return {"release": label, "index_sha256": sha256(html), "module_entries": index.module_entries,
            "importmap": index.importmap, "validated_dependencies": sorted({path for path, _ in visited})}


def prepare(current_root: Path, previous_roots: list[Path], output_root: Path,
            *, manifest: Path | None = None, required_assets: list[str] = ()) -> dict:
    roots = [ordinary_directory(current_root), *(ordinary_directory(root) for root in previous_roots)]
    output = Path(os.path.abspath(output_root))
    ordinary_directory(output.parent)
    if os.path.lexists(output) or any(output == root or output.is_relative_to(root) or root.is_relative_to(output) for root in roots):
        raise ReleaseError("Output must be new and outside all input trees")
    if manifest is not None:
        manifest = Path(os.path.abspath(manifest))
        if (os.path.lexists(manifest) or manifest == output or manifest.is_relative_to(output)
                or any(manifest == root or manifest.is_relative_to(root) for root in roots)):
            raise ReleaseError("Manifest must be a new file outside all static trees")
        ordinary_directory(manifest.parent)
    inventories = [inventory(root) for root in roots]
    files = dict(inventories[0])
    origins = {path: ["current"] for path in files}
    for number, previous in enumerate(inventories[1:], 1):
        for path, payload in previous.items():
            if not path.startswith("assets/"):
                continue
            if path in files and files[path] != payload:
                raise ReleaseError("Conflicting bytes for retained asset: " + path)
            files[path] = payload
            origins.setdefault(path, []).append("previous-" + str(number))
    for asset in required_assets:
        try:
            valid = asset.startswith("assets/") and local_url(asset) == asset and asset in files
        except ReleaseError:
            valid = False
        if not valid:
            raise ReleaseError("Required retained asset is missing or invalid: " + asset)
    releases = [validate_graph(files, contents["index.html"], "current" if number == 0 else "previous-" + str(number))
                for number, contents in enumerate(inventories)]
    result = {"schema_version": 1, "releases": releases, "required_assets": sorted(set(required_assets)),
              "files": [{"path": path, "sha256": sha256(payload), "bytes": len(payload), "releases": origins[path]}
                        for path, payload in sorted(files.items())]}
    staging = Path(tempfile.mkdtemp(prefix="." + output.name + "-", dir=output.parent))
    written_manifest = False
    try:
        for path, payload in files.items():
            target = staging / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
            target.chmod(0o644)
        # Operations use a private umask. Every directory copied into the image
        # must still be traversable by its nonroot static server.
        for directory in staging.rglob("*"):
            if directory.is_dir():
                directory.chmod(0o755)
        if os.path.lexists(output):
            raise ReleaseError("Output was created by another operation")
        if manifest is not None:
            with manifest.open("xb") as stream:
                written_manifest = True
                stream.write((json.dumps(result, sort_keys=True, indent=2) + "\n").encode())
                stream.flush()
                os.fsync(stream.fileno())
        staging.chmod(0o755)
        staging.rename(output)
    except Exception:
        if written_manifest:
            manifest.unlink()
        raise
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--current-root", required=True, type=Path)
    parser.add_argument("--previous-root", action="append", default=[], type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--required-asset", action="append", default=[])
    args = parser.parse_args()
    try:
        result = prepare(args.current_root, args.previous_root, args.output_root,
                         manifest=args.manifest, required_assets=args.required_asset)
    except (ReleaseError, OSError) as exc:
        parser.exit(1, str(exc) + "\n")
    print(json.dumps({"status": "prepared", "files": len(result["files"]), "releases": len(result["releases"])}))


if __name__ == "__main__":
    main()
