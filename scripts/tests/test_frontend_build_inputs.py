from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
import posixpath
import re
import shlex
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def docker_build_copies(root: Path) -> list[tuple[Path, str]]:
    """Resolve local COPY inputs in the frontend's first build stage."""
    text = (root / "frontend/Dockerfile").read_text(encoding="utf-8")
    text = re.sub(r"\\\n\s*", " ", text)
    stage = 0
    working_directory = "/"
    copies: list[tuple[Path, str]] = []
    for line in text.splitlines():
        instruction, _, value = line.strip().partition(" ")
        instruction = instruction.upper()
        if instruction == "FROM":
            stage += 1
            if stage > 1:
                break
        elif stage == 1 and instruction == "WORKDIR":
            working_directory = posixpath.normpath(posixpath.join(working_directory, value.strip()))
        elif stage == 1 and instruction == "COPY":
            arguments = json.loads(value) if value.startswith("[") else shlex.split(value)
            if any(argument.startswith("--from=") for argument in arguments):
                continue
            arguments = [argument for argument in arguments if not argument.startswith("--")]
            destination = arguments[-1]
            sources = [path for pattern in arguments[:-1] for path in root.glob(pattern)]
            for source in sources:
                target = posixpath.normpath(posixpath.join(working_directory, destination))
                if source.is_file() and (destination.endswith("/") or len(sources) > 1):
                    target = posixpath.join(target, source.name)
                copies.append((source, target))
    return copies


def stage_location(path: Path, copies: list[tuple[Path, str]]) -> str | None:
    for source, destination in reversed(copies):
        if path == source:
            return destination
        if source.is_dir() and path.is_relative_to(source):
            return str(PurePosixPath(destination) / path.relative_to(source).as_posix())
    return None


class FrontendDockerBuildInputTests(unittest.TestCase):
    def test_relative_json_imports_are_present_at_their_build_stage_locations(self) -> None:
        root = REPOSITORY_ROOT
        frontend = root / "frontend"
        copies = docker_build_copies(root)
        imports = re.compile(r"(?:from\s*|import\s*\(\s*|import\s*)[\"']([^\"']+\.json)[\"']")
        external_imports = 0
        missing = []
        for source in (frontend / "src").rglob("*.ts*"):
            for specification in imports.findall(source.read_text(encoding="utf-8")):
                if not specification.startswith("."):
                    continue
                dependency = (source.parent / specification).resolve()
                if dependency.is_relative_to(frontend):
                    continue
                external_imports += 1
                self.assertTrue(dependency.is_file(), str(dependency.relative_to(root)))
                source_in_stage = stage_location(source, copies)
                self.assertIsNotNone(source_in_stage, str(source.relative_to(root)))
                expected = posixpath.normpath(posixpath.join(posixpath.dirname(source_in_stage), specification))
                if stage_location(dependency, copies) != expected:
                    missing.append(str(dependency.relative_to(root)) + " -> " + expected)
        self.assertGreater(external_imports, 0, "No external JSON imports were checked")
        self.assertEqual(missing, [], "Docker build stage is missing imported JSON inputs")


if __name__ == "__main__":
    unittest.main()
