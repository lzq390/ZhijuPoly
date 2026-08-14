from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import time
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "provision_mutable_data_audit_role.py"
SPEC = importlib.util.spec_from_file_location("mutable_role_provision_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
ROLE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ROLE)


def _write_private(path: Path, value: object | bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = value if isinstance(value, bytes) else ROLE._canonical_bytes(value) + b"\n"
    path.write_bytes(payload)
    path.chmod(mode)


def _delivery_gate(source_sha: str) -> dict[str, object]:
    return {
        "remote_main": source_sha,
        "ci": {
            "workflow_run_id": 1234,
            "run_attempt": 1,
            "head_sha": source_sha,
            "head_branch": "main",
            "event": "push",
            "path": ".github/workflows/ci.yml",
            "conclusion": "success",
            "required_jobs": ["exact-B"],
        },
    }


class MutableAuditRolePrimitiveTests(unittest.TestCase):
    def test_pgpass_parser_supports_libpq_escapes_without_text_secret(self) -> None:
        fields = ROLE._split_pgpass_bytes(
            br"127.0.0.1:55432:nexpoly:nexpoly_mutable_audit:secr\:et\\value"
        )
        self.assertTrue(all(isinstance(value, bytearray) for value in fields))
        self.assertEqual(
            [bytes(value) for value in fields],
            [
                b"127.0.0.1",
                b"55432",
                b"nexpoly",
                b"nexpoly_mutable_audit",
                b"secr:et\\value",
            ],
        )

    def test_pgpass_parser_rejects_trailing_escape(self) -> None:
        with self.assertRaisesRegex(ROLE.RoleProvisionError, "trailing escape"):
            ROLE._split_pgpass_bytes(b"a:b:c:d:secret\\")

    def test_scram_verifier_matches_postgresql_algorithm(self) -> None:
        password = bytearray(b"correct horse battery staple")
        salt = bytes(range(16))
        verifier = ROLE._scram_verifier(password, salt=salt)
        salted = hashlib.pbkdf2_hmac("sha256", bytes(password), salt, 4096)
        stored = hashlib.sha256(
            hmac.new(salted, b"Client Key", hashlib.sha256).digest()
        ).digest()
        server = hmac.new(salted, b"Server Key", hashlib.sha256).digest()
        expected = (
            "SCRAM-SHA-256$4096:"
            + base64.b64encode(salt).decode()
            + "$"
            + base64.b64encode(stored).decode()
            + ":"
            + base64.b64encode(server).decode()
        )
        self.assertEqual(verifier, expected)
        self.assertRegex(verifier, ROLE.SCRAM_RE)

    def test_transaction_contains_verifier_but_never_plaintext(self) -> None:
        password = bytearray(b"do-not-send-this-password")
        verifier = ROLE._scram_verifier(password, salt=b"0" * 16)
        role_sql = (
            b"\\set ON_ERROR_STOP on\nBEGIN;\n"
            b"__IN_TRANSACTION_SEALED_CAS__\nALTER ROLE x PASSWORD "
            b"__SCRAM_VERIFIER_LITERAL__;\n"
            b"__IN_TRANSACTION_DESIRED_ASSERT__\nCOMMIT;\n"
        )
        payload = ROLE._transaction_sql(
            role_sql,
            verifier,
            before_database={"state": "before"},
            desired_database={"state": "desired"},
        )
        self.assertIn(verifier.encode(), payload)
        self.assertNotIn(bytes(password), payload)
        self.assertNotIn(b"__IN_TRANSACTION_", payload)
        self.assertLess(payload.index(b"LOCK TABLE"), payload.index(b"ALTER ROLE x"))
        self.assertLess(
            payload.index(b"sealed mutable-audit before/desired CAS differs"),
            payload.index(b"ALTER ROLE x"),
        )

    def test_deploy_lock_uses_one_open_fd_through_flock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary) / "runtime"
            (runtime / "state").mkdir(mode=0o700, parents=True)
            runtime.chmod(0o700)
            lock = runtime / "state/deploy.lock"
            lock.write_bytes(b"")
            lock.chmod(0o600)
            with mock.patch.object(ROLE.os, "open", wraps=os.open) as opened:
                with ROLE._deploy_lock(runtime):
                    self.assertEqual(opened.call_count, 1)
            flags = opened.call_args.args[1]
            self.assertTrue(flags & getattr(os, "O_NOFOLLOW", 0))

    def test_git_disables_optional_locks(self) -> None:
        completed = mock.Mock(stdout=b"ok")
        with mock.patch.object(
            ROLE, "_assert_pre_git_source_safety"
        ) as preflight, mock.patch.object(
            ROLE, "_trusted_root_binary"
        ) as trusted_binary, mock.patch.object(
            ROLE, "_git_ssh_command", return_value="/usr/bin/ssh -F /dev/null"
        ), mock.patch.object(ROLE, "_run", return_value=completed) as run:
            self.assertEqual(ROLE._git("status"), b"ok")
        preflight.assert_called_once_with(ROLE.SOURCE_ROOT)
        trusted_binary.assert_called_once_with(Path("/usr/bin/git"))
        command = run.call_args.args[0]
        self.assertEqual(command[:2], ["/usr/bin/git", "--no-optional-locks"])
        self.assertIn("protocol.allow=never", command)
        self.assertIn("protocol.ssh.allow=always", command)
        self.assertIn("diff.external=", command)
        environment = run.call_args.kwargs["extra_environment"]
        self.assertEqual(environment["GIT_OPTIONAL_LOCKS"], "0")
        self.assertEqual(environment["GIT_CONFIG_COUNT"], "0")
        self.assertEqual(environment["GIT_CONFIG_GLOBAL"], os.devnull)
        self.assertEqual(environment["GIT_EXTERNAL_DIFF"], "")
        self.assertEqual(
            environment["GIT_SSH_COMMAND"], "/usr/bin/ssh -F /dev/null"
        )

    def test_private_reader_rejects_symlink_and_hardlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            target = root / "target"
            target.write_bytes(b"secret")
            target.chmod(0o600)
            link = root / "link"
            link.symlink_to(target)
            with self.assertRaises(ROLE.RoleProvisionError):
                ROLE._read_private(link, maximum_bytes=100)
            hard = root / "hard"
            os.link(target, hard)
            with self.assertRaisesRegex(ROLE.RoleProvisionError, "unsafe"):
                ROLE._read_private(target, maximum_bytes=100)

    def test_atomic_journal_replaces_private_regular_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            path = root / "journal.json"
            ROLE._atomic_private_json(path, {"phase": "one"})
            first_inode = path.stat().st_ino
            ROLE._atomic_private_json(path, {"phase": "two"})
            self.assertNotEqual(first_inode, path.stat().st_ino)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(json.loads(path.read_text()), {"phase": "two"})


class MutableAuditRoleSourceTrustTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.source = Path(self.temporary.name) / "source"
        self.source.mkdir(mode=0o700)
        for relative, mode in (
            (ROLE.SCRIPT_PATH, 0o700),
            (ROLE.ROLE_SQL_PATH, 0o600),
        ):
            destination = self.source / relative
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            shutil.copyfile(SCRIPT.parents[1] / relative, destination)
            destination.chmod(mode)
        subprocess.run(
            ["/usr/bin/git", "init", "--quiet", "--initial-branch=main"],
            cwd=self.source,
            check=True,
        )
        for key, value in (
            ("user.name", "Role Source Test"),
            ("user.email", "role-source@example.invalid"),
        ):
            subprocess.run(
                ["/usr/bin/git", "config", key, value],
                cwd=self.source,
                check=True,
            )
        subprocess.run(
            ["/usr/bin/git", "add", "."],
            cwd=self.source,
            check=True,
        )
        subprocess.run(
            ["/usr/bin/git", "commit", "--quiet", "-m", "source fixture"],
            cwd=self.source,
            check=True,
        )
        subprocess.run(
            ["/usr/bin/git", "remote", "add", "origin", ROLE.SSH_ORIGIN],
            cwd=self.source,
            check=True,
        )
        subprocess.run(
            [
                "/usr/bin/git",
                "update-ref",
                "refs/remotes/origin/main",
                "HEAD",
            ],
            cwd=self.source,
            check=True,
        )
        for path in self.source.rglob("*"):
            if not path.is_symlink():
                path.chmod(stat.S_IMODE(path.stat().st_mode) & ~0o077)
        self.source.chmod(0o700)
        self.sha = subprocess.run(
            ["/usr/bin/git", "rev-parse", "HEAD"],
            cwd=self.source,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
        ).stdout.strip()
        self.tree = subprocess.run(
            ["/usr/bin/git", "rev-parse", "HEAD^{tree}"],
            cwd=self.source,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
        ).stdout.strip()
        self.old_source_root = ROLE.SOURCE_ROOT
        ROLE.SOURCE_ROOT = self.source

    def tearDown(self) -> None:
        ROLE.SOURCE_ROOT = self.old_source_root
        self.temporary.cleanup()

    def test_dirty_filter_policy_is_rejected_before_any_git_process(self) -> None:
        marker = self.source / "filter-executed"
        attributes = self.source / ".gitattributes"
        attributes.write_text(f"{ROLE.SCRIPT_PATH} filter=evil\n")
        attributes.chmod(0o600)
        subprocess.run(
            [
                "/usr/bin/git",
                "config",
                "filter.evil.clean",
                f"/usr/bin/touch {marker}",
            ],
            cwd=self.source,
            check=True,
        )

        with mock.patch.object(
            ROLE,
            "_run",
            side_effect=AssertionError("Git ran before the pure source gate"),
        ) as controlled_run:
            with self.assertRaisesRegex(
                ROLE.RoleProvisionError,
                "executable or redirect policy|executable Git attributes",
            ):
                ROLE._source_authority(self.sha)

        controlled_run.assert_not_called()
        self.assertFalse(marker.exists())

    def test_remote_execution_policy_is_rejected_before_git(self) -> None:
        marker = self.source / "remote-policy-executed"
        subprocess.run(
            [
                "/usr/bin/git",
                "config",
                "remote.origin.uploadpack",
                f"/usr/bin/touch {marker}",
            ],
            cwd=self.source,
            check=True,
        )

        with mock.patch.object(
            ROLE,
            "_run",
            side_effect=AssertionError("Git ran before remote policy validation"),
        ) as controlled_run:
            with self.assertRaisesRegex(
                ROLE.RoleProvisionError,
                "executable or redirect policy",
            ):
                ROLE._source_authority(self.sha)

        controlled_run.assert_not_called()
        self.assertFalse(marker.exists())

    def test_assume_unchanged_and_skip_worktree_are_rejected(self) -> None:
        original_run = ROLE._run

        def controlled_run(command, **kwargs):  # type: ignore[no-untyped-def]
            if "ls-remote" in command:
                return mock.Mock(
                    stdout=f"{self.sha}\trefs/heads/main\n".encode()
                )
            return original_run(command, **kwargs)

        cases = (
            ("--assume-unchanged", "--no-assume-unchanged"),
            ("--skip-worktree", "--no-skip-worktree"),
        )
        for set_flag, clear_flag in cases:
            with self.subTest(index_flag=set_flag):
                subprocess.run(
                    [
                        "/usr/bin/git",
                        "update-index",
                        set_flag,
                        ROLE.SCRIPT_PATH,
                    ],
                    cwd=self.source,
                    check=True,
                )
                (self.source / ".git/index").chmod(0o600)
                try:
                    with mock.patch.object(
                        ROLE,
                        "_git_ssh_command",
                        return_value="/usr/bin/ssh -F /dev/null",
                    ), mock.patch.object(
                        ROLE, "_run", side_effect=controlled_run
                    ):
                        with self.assertRaisesRegex(
                            ROLE.RoleProvisionError,
                            "assume-unchanged or skip-worktree",
                        ):
                            ROLE._source_authority(self.sha)
                finally:
                    subprocess.run(
                        [
                            "/usr/bin/git",
                            "update-index",
                            clear_flag,
                            ROLE.SCRIPT_PATH,
                        ],
                        cwd=self.source,
                        check=True,
                    )
                    (self.source / ".git/index").chmod(0o600)

    def test_clean_source_uses_only_pinned_git_commands(self) -> None:
        calls: list[tuple[list[str], dict[str, str]]] = []

        def controlled_git(command, **kwargs):  # type: ignore[no-untyped-def]
            environment = dict(kwargs["extra_environment"])
            calls.append((list(command), environment))
            subcommand_index = next(
                index
                for index, value in enumerate(command)
                if value
                in {
                    "rev-parse",
                    "ls-remote",
                    "status",
                    "ls-files",
                    "remote",
                    "for-each-ref",
                    "fsck",
                    "show",
                }
            )
            arguments = command[subcommand_index:]
            if arguments == ["rev-parse", "--verify", "HEAD"]:
                output = f"{self.sha}\n".encode()
            elif arguments == [
                "rev-parse",
                "--verify",
                "refs/heads/main",
            ]:
                output = f"{self.sha}\n".encode()
            elif arguments == [
                "rev-parse",
                "--verify",
                f"{self.sha}^{{tree}}",
            ]:
                output = f"{self.tree}\n".encode()
            elif arguments == [
                "rev-parse",
                "--verify",
                "refs/remotes/origin/main",
            ]:
                output = f"{self.sha}\n".encode()
            elif arguments[0] == "ls-remote":
                output = f"{self.sha}\trefs/heads/main\n".encode()
            elif arguments[0] in {"status", "for-each-ref", "fsck"}:
                output = b""
            elif arguments == [
                "ls-files",
                "-z",
                "--others",
                "--ignored",
                "--exclude-standard",
            ]:
                output = b""
            elif arguments == ["ls-files", "-z", "--stage"]:
                output = (
                    b"100644 " + b"1" * 40 + b" 0\t" + ROLE.SCRIPT_PATH.encode() + b"\0"
                    b"100644 " + b"2" * 40 + b" 0\t" + ROLE.ROLE_SQL_PATH.encode() + b"\0"
                )
            elif arguments == ["ls-files", "-z", "-v", "--cached"]:
                output = (
                    b"H " + ROLE.SCRIPT_PATH.encode() + b"\0"
                    b"H " + ROLE.ROLE_SQL_PATH.encode() + b"\0"
                )
            elif arguments == ["remote"]:
                output = b"origin\n"
            elif arguments[:3] == ["remote", "get-url", "--all"]:
                output = f"{ROLE.SSH_ORIGIN}\n".encode()
            elif arguments[:4] == ["remote", "get-url", "--push", "--all"]:
                output = f"{ROLE.SSH_ORIGIN}\n".encode()
            elif arguments == ["rev-parse", "--is-shallow-repository"]:
                output = b"false\n"
            elif arguments[0] == "show":
                relative = arguments[1].split(":", 1)[1]
                output = (self.source / relative).read_bytes()
            else:  # pragma: no cover - explicit command contract above
                raise AssertionError(f"unexpected Git command: {arguments}")
            return mock.Mock(stdout=output)

        with mock.patch.object(
            ROLE, "_git_ssh_command", return_value="/usr/bin/ssh -F /dev/null"
        ), mock.patch.object(
            ROLE, "_trusted_root_binary"
        ), mock.patch.object(ROLE, "_run", side_effect=controlled_git):
            source, role_sql = ROLE._source_authority(self.sha)

        self.assertEqual(source["sha"], self.sha)
        self.assertEqual(source["tree"], self.tree)
        self.assertEqual(role_sql, (self.source / ROLE.ROLE_SQL_PATH).read_bytes())
        self.assertTrue(calls)
        for command, environment in calls:
            self.assertEqual(command[:2], ["/usr/bin/git", "--no-optional-locks"])
            self.assertIn("protocol.allow=never", command)
            self.assertIn("protocol.ssh.allow=always", command)
            self.assertEqual(environment["GIT_CONFIG_COUNT"], "0")
            self.assertEqual(environment["GIT_EXTERNAL_DIFF"], "")
            self.assertEqual(
                environment["GIT_SSH_COMMAND"], "/usr/bin/ssh -F /dev/null"
            )


class MutableAuditRoleAdoptionAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.runtime = Path(self.temporary.name) / "runtime"
        for relative in ("state", "config"):
            (self.runtime / relative).mkdir(mode=0o700, parents=True)
        self.old_runtime = ROLE.RUNTIME_ROOT
        self.old_pgpass = ROLE.PGPASS_PATH
        ROLE.RUNTIME_ROOT = self.runtime
        ROLE.PGPASS_PATH = self.runtime / "config/mutable-data-audit.pgpass"

        self.authority_sha = "a" * 40
        self.authority_tree = "b" * 40
        self.target_sha = "c" * 40
        self.target_tree = "d" * 40
        self.production_sha = "e" * 40
        self.production_tree = "f" * 40
        self.is_ancestor = True
        self.observed_authority_tree = self.authority_tree
        self.target_overrides: dict[str, bytes] = {}
        self.authority_overrides: dict[str, bytes] = {}

        self.active = {"schema_version": 1, "release_id": "adopted-control"}
        evidence_names = (
            "images",
            "production_config",
            "asset_identity",
            "migrations",
            "database",
            "maintenance",
            "monomer_md",
            "monomer_dft",
        )
        evidence = {
            "operation_id": "adopt-unit-test-0001",
            "live_repository": {
                "head": self.production_sha,
                "tree": self.production_tree,
            },
            **{name: {"sealed": name} for name in evidence_names},
        }
        adopted = {
            "schema_version": 1,
            "status": "adopted",
            "authority_kind": ROLE.ADOPTION_AUTHORITY_KIND,
            "operation_id": evidence["operation_id"],
            "source_sha": self.production_sha,
            "source_tree": self.production_tree,
            "adoption_evidence": evidence,
            "adoption_evidence_sha256": ROLE._digest(evidence),
            "active_control": self.active,
            **{name: evidence[name] for name in evidence_names},
        }
        # Production adoption predates canonical write-once JSON.  The
        # prerequisite transaction seals these exact pretty-printed bytes.
        self.adopted_payload = (
            json.dumps(adopted, indent=2, sort_keys=False).encode("utf-8") + b"\n"
        )
        _write_private(
            self.runtime / "state/adopted-deployment.json", self.adopted_payload
        )
        _write_private(self.runtime / "state/active-control.json", self.active)
        bootstrap_readiness = self._readiness(
            self.authority_sha, self.authority_tree
        )
        bootstrap = {
            "schema_version": 3,
            "status": "completed",
            "authority_kind": ROLE.ADOPTION_AUTHORITY_KIND,
            "operation_id": evidence["operation_id"],
            "source_sha": self.authority_sha,
            "source_tree": self.authority_tree,
            "adopted_deployment": adopted,
            "adopted_deployment_sha256": ROLE._digest(adopted),
            "adoption": evidence,
            "adoption_evidence_sha256": ROLE._digest(evidence),
            "active_control": self.active,
            "source_readiness": bootstrap_readiness,
            "source_readiness_sha256": ROLE._digest(bootstrap_readiness),
            "delivery_gate": _delivery_gate(self.authority_sha),
        }
        _write_private(self.runtime / "state/bootstrap-control.json", bootstrap)

        self.blobs = {
            source_path: f"sealed prerequisite: {source_path}\n".encode()
            for source_path, _name, _mode, _classification
            in ROLE.PREREQUISITE_INSTALLS
        }
        files: list[dict[str, object]] = []
        for source_path, name, mode, classification in ROLE.PREREQUISITE_INSTALLS:
            payload = self.blobs[source_path]
            _write_private(self.runtime / "config" / name, payload, mode)
            files.append(
                {
                    "source_path": source_path,
                    "destination": str(self.runtime / "config" / name),
                    "name": name,
                    "sha256": ROLE._digest_bytes(payload),
                    "mode": f"{mode:04o}",
                    "classification": classification,
                    "disposition": "create",
                }
            )
        self.pgpass_payload = (
            b"127.0.0.1:55432:nexpoly:nexpoly_mutable_audit:unit-test-only\n"
        )
        _write_private(ROLE.PGPASS_PATH, self.pgpass_payload)
        prereq_readiness = self._readiness(
            self.authority_sha, self.authority_tree
        )
        prereq_delivery = _delivery_gate(self.authority_sha)
        prereq_plan = {
            "schema_version": 1,
            "authority_kind": ROLE.PREREQUISITE_AUTHORITY_KIND,
            "operation_id": "adopt-prereq-unit-test-0001",
            "source_sha": self.authority_sha,
            "source_tree": self.authority_tree,
            "source_readiness": prereq_readiness,
            "source_readiness_sha256": ROLE._digest(prereq_readiness),
            "delivery_gate": prereq_delivery,
            "delivery_gate_sha256": ROLE._digest(prereq_delivery),
            "adopted_deployment_sha256": ROLE._digest_bytes(
                self.adopted_payload
            ),
            "files": files,
            "preserved_pgpass": {
                "path": str(ROLE.PGPASS_PATH),
                "sha256": ROLE._digest_bytes(self.pgpass_payload),
                "mode": "0600",
            },
            "mutations": {
                "services": False,
                "source": False,
                "database": False,
                "credentials": False,
            },
        }
        prerequisites = {
            "schema_version": 1,
            "status": "completed",
            "authority_kind": ROLE.PREREQUISITE_AUTHORITY_KIND,
            "operation_id": prereq_plan["operation_id"],
            "source_sha": self.authority_sha,
            "source_tree": self.authority_tree,
            "adopted_deployment_sha256": ROLE._digest_bytes(
                self.adopted_payload
            ),
            "plan_sha256": ROLE._digest(prereq_plan),
            "plan": prereq_plan,
            "completed_at": "2026-08-14T00:00:00Z",
        }
        self.prerequisites = prerequisites
        _write_private(
            self.runtime / "state/adopted-prerequisites.json", prerequisites
        )

    def tearDown(self) -> None:
        ROLE.RUNTIME_ROOT = self.old_runtime
        ROLE.PGPASS_PATH = self.old_pgpass
        self.temporary.cleanup()

    def _readiness(self, source_sha: str, source_tree: str) -> dict[str, object]:
        return {
            "schema_version": 2,
            "ready": True,
            "source_root": "/untrusted/old/source/path",
            "source_sha": source_sha,
            "source_tree": source_tree,
            "branch": "main",
            "origin": ROLE.SSH_ORIGIN,
            "remote_names": ["origin"],
            "origin_fetch_urls": [ROLE.SSH_ORIGIN],
            "origin_push_urls": [ROLE.SSH_ORIGIN],
            "origin_main_sha": source_sha,
            "standalone_object_database": True,
            "shallow": False,
            "dirty_entries": 0,
            "ignored_entries": 0,
            "unreachable_objects": 0,
            "replace_refs": 0,
            "special_index_entries": 0,
            "sparse_index": False,
            "owner_private": True,
            "group_or_world_writable": False,
        }

    def _git(self, *arguments: str, check: bool = True) -> bytes:
        del check
        if arguments == (
            "rev-parse",
            "--verify",
            f"{self.authority_sha}^{{tree}}",
        ):
            return f"{self.observed_authority_tree}\n".encode()
        if arguments == (
            "rev-parse",
            "--verify",
            f"{self.target_sha}^{{tree}}",
        ):
            return f"{self.target_tree}\n".encode()
        if arguments == (
            "merge-base",
            "--is-ancestor",
            self.authority_sha,
            self.target_sha,
        ):
            if not self.is_ancestor:
                raise ROLE.RoleProvisionError("controlled command failed: git")
            return b""
        if len(arguments) == 2 and arguments[0] == "show":
            revision, source_path = arguments[1].split(":", 1)
            if revision == self.authority_sha:
                return self.authority_overrides.get(
                    source_path, self.blobs[source_path]
                )
            if revision == self.target_sha:
                return self.target_overrides.get(source_path, self.blobs[source_path])
        raise AssertionError(f"unexpected Git call: {arguments}")

    def _current_gate(self, source_sha: str, *, sealed: object = None) -> dict[str, object]:
        gate = _delivery_gate(source_sha)
        if sealed is not None:
            self.assertEqual(sealed, gate)
        return gate

    def _strict(self, *, sealed: object = None) -> dict[str, object]:
        with mock.patch.object(ROLE, "_git", side_effect=self._git), mock.patch.object(
            ROLE, "_current_delivery_gate", side_effect=self._current_gate
        ):
            return ROLE._strict_adopted_authority(
                self.target_sha, sealed_delivery_gate=sealed
            )

    def test_pretty_adoption_uses_raw_digest_and_binds_ancestor_blobs(self) -> None:
        result = self._strict(sealed=_delivery_gate(self.target_sha))
        self.assertEqual(
            result["adopted_file_sha256"],
            ROLE._digest_bytes(self.adopted_payload),
        )
        self.assertNotEqual(
            result["adopted_file_sha256"], result["adopted_sha256"]
        )
        binding = result["prerequisite_source_binding"]
        self.assertEqual(binding["authority_sha"], self.authority_sha)
        self.assertEqual(binding["authority_tree"], self.authority_tree)
        self.assertEqual(binding["target_sha"], self.target_sha)
        self.assertEqual(binding["target_tree"], self.target_tree)
        self.assertEqual(binding["relation"], "ancestor-byte-identical")
        self.assertRegex(binding["files_sha256"], ROLE.DIGEST_RE)
        self.assertEqual(
            result["current_delivery_gate"], _delivery_gate(self.target_sha)
        )

    def test_nonancestor_prerequisite_is_rejected(self) -> None:
        self.is_ancestor = False
        with self.assertRaisesRegex(ROLE.RoleProvisionError, "not an ancestor"):
            self._strict()

    def test_exact_prerequisite_source_remains_supported(self) -> None:
        self.target_sha = self.authority_sha
        self.target_tree = self.authority_tree
        result = self._strict()
        self.assertEqual(
            result["prerequisite_source_binding"]["relation"], "exact"
        )

    def test_authority_or_target_blob_drift_is_rejected(self) -> None:
        source_path = ROLE.PREREQUISITE_INSTALLS[0][0]
        self.target_overrides[source_path] = b"changed target blob\n"
        with self.assertRaisesRegex(ROLE.RoleProvisionError, "digest changed"):
            self._strict()
        self.target_overrides.clear()
        self.authority_overrides[source_path] = b"changed authority blob\n"
        with self.assertRaisesRegex(ROLE.RoleProvisionError, "digest changed"):
            self._strict()

    def test_installed_prerequisite_or_pgpass_drift_is_rejected(self) -> None:
        _source_path, name, mode, _classification = ROLE.PREREQUISITE_INSTALLS[0]
        _write_private(self.runtime / "config" / name, b"installed drift\n", mode)
        with self.assertRaisesRegex(ROLE.RoleProvisionError, "digest changed"):
            self._strict()
        _write_private(
            self.runtime / "config" / name,
            self.blobs[ROLE.PREREQUISITE_INSTALLS[0][0]],
            mode,
        )
        _write_private(ROLE.PGPASS_PATH, b"changed-test-pgpass\n")
        with self.assertRaisesRegex(ROLE.RoleProvisionError, "pgpass changed"):
            self._strict()

    def test_recomputed_authority_tree_drift_is_rejected(self) -> None:
        self.observed_authority_tree = "9" * 40
        with self.assertRaisesRegex(ROLE.RoleProvisionError, "tree authority"):
            self._strict()

    def test_malformed_prerequisite_operation_or_timestamp_is_rejected(self) -> None:
        self.prerequisites["completed_at"] = "2026-08-14T00:00:00+00:00"
        _write_private(
            self.runtime / "state/adopted-prerequisites.json",
            self.prerequisites,
        )
        with self.assertRaisesRegex(
            ROLE.RoleProvisionError, "prerequisite authority is invalid"
        ):
            self._strict()

        self.prerequisites["completed_at"] = "2026-08-14T00:00:00Z"
        self.prerequisites["operation_id"] = "invalid-operation"
        self.prerequisites["plan"]["operation_id"] = "invalid-operation"
        self.prerequisites["plan_sha256"] = ROLE._digest(
            self.prerequisites["plan"]
        )
        _write_private(
            self.runtime / "state/adopted-prerequisites.json",
            self.prerequisites,
        )
        with self.assertRaisesRegex(
            ROLE.RoleProvisionError, "prerequisite authority is invalid"
        ):
            self._strict()


class MutableAuditRoleContractTests(unittest.TestCase):
    def _database(self, *, role: object = None, login: bool = False) -> dict[str, object]:
        return {
            "system_identifier": "7659245354718314530",
            "database_oid": "16384",
            "database_owner": "polyprop",
            "session_user": "polyprop",
            "session_superuser": True,
            "present_governed_schemas": ["core", "dft", "experimental", "governance", "knowledge", "lab", "md", "model_registry", "monomer_dft", "online_knowledge", "pi"],
            "role": role,
            "database_privileges": {"connect": False, "create": False, "temporary": True},
            "memberships": [],
            "database_role_settings": [],
            "schemas": [
                {"schema": name, "oid": str(index + 100), "usage": False, "create": False}
                for index, name in enumerate(["core", "dft", "experimental", "governance", "knowledge", "lab", "md", "model_registry", "monomer_dft", "online_knowledge", "pi"])
            ],
            "relations": [
                {
                    "relation": "core.polymers", "oid": "200", "kind": "r", "owner": "polyprop",
                    "select": False, "insert": False, "update": False, "delete": False,
                    "truncate": False, "references": False, "trigger": False,
                }
            ],
            "sequences": [
                {"sequence": "core.polymers_id_seq", "oid": "201", "owner": "polyprop", "select": False, "usage": False, "update": False}
            ],
            "column_write_grants": [],
            "outside_governed_privileges": [],
            "default_privileges": [],
            "owned_objects": [],
            "security_definer_execute": [],
            "large_object_update_count": 0,
            "large_object_mutators": [
                {
                    "routine": f"lo_mutator_{index}()", "oid": str(300 + index),
                    "owner": "postgres", "other_acl": [], "public_execute": True,
                    "database_owner_execute": False, "audit_execute": False,
                }
                for index in range(8)
            ],
        }

    def test_desired_contract_is_database_local_and_read_only(self) -> None:
        before = {"database": self._database(), "pgpass_login_matches": False}
        desired = ROLE._desired_state(before)
        database = desired["database"]
        self.assertEqual(database["memberships"], [])
        self.assertNotIn("pg_read_all_data", json.dumps(desired))
        self.assertTrue(all(item["select"] for item in database["relations"]))
        self.assertTrue(all(not item["update"] for item in database["relations"]))
        self.assertTrue(all(item["select"] for item in database["sequences"]))
        self.assertTrue(all(not item["usage"] for item in database["sequences"]))
        self.assertEqual(len(database["default_privileges"]), 22)
        self.assertTrue(desired["pgpass_login_matches"])
        self.assertTrue(database["database_privileges"]["temporary"])

    def test_state_comparison_requires_complete_lo_acl_projection(self) -> None:
        before = {"database": self._database(), "pgpass_login_matches": False}
        desired = ROLE._desired_state(before)
        observed = copy.deepcopy(desired)
        self.assertTrue(ROLE._state_is_desired(observed, desired))
        observed["database"]["large_object_mutators"][0]["other_acl"] = [
            {"grantee": "unexpected", "grantor": "postgres", "privilege": "EXECUTE", "grantable": False}
        ]
        self.assertFalse(ROLE._state_is_desired(observed, desired))
        observed = copy.deepcopy(desired)
        observed["database"]["relations"][0]["update"] = True
        self.assertFalse(ROLE._state_is_desired(observed, desired))

    def test_sql_template_has_one_transaction_and_no_global_read_role(self) -> None:
        payload = (SCRIPT.parents[1] / ROLE.ROLE_SQL_PATH).read_text()
        self.assertEqual(payload.count("__SCRAM_VERIFIER_LITERAL__"), 1)
        self.assertEqual(payload.count("__IN_TRANSACTION_SEALED_CAS__"), 1)
        self.assertEqual(payload.count("__IN_TRANSACTION_DESIRED_ASSERT__"), 1)
        self.assertEqual(payload.count("BEGIN TRANSACTION"), 1)
        self.assertTrue(payload.rstrip().endswith("COMMIT;"))
        self.assertNotIn("pg_read_all_data", payload)
        self.assertIn("ALTER DEFAULT PRIVILEGES FOR ROLE polyprop IN SCHEMA", payload)
        self.assertIn("FROM PUBLIC, nexpoly_mutable_audit", payload)


@unittest.skipUnless(
    os.environ.get("NEXPOLY_RUN_POSTGRES_ROLE_INTEGRATION") == "1",
    "set NEXPOLY_RUN_POSTGRES_ROLE_INTEGRATION=1 for Docker PostgreSQL test",
)
class MutableAuditRolePostgresIntegrationTests(unittest.TestCase):
    image = (
        "postgres:16-alpine@sha256:"
        "57c72fd2a128e416c7fcc499958864df5301e940bca0a56f58fddf30ffc07777"
    )

    def _final_server_identity(self) -> dict[str, object] | None:
        """Return identity only for the stable, TCP-listening final server.

        The official image briefly starts an init server with
        ``listen_addresses=''``.  A Unix-socket-only ``pg_isready`` can
        therefore succeed immediately before that server exits.  Requiring a
        TCP query on 127.0.0.1 excludes the init server; container state and
        PostgreSQL identity make consecutive success an exact stability proof.
        """

        inspected = subprocess.run(
            [
                "docker",
                "container",
                "inspect",
                "--format",
                "{{json .State}}",
                self.container,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        try:
            state = json.loads(inspected.stdout.decode())
        except (UnicodeError, json.JSONDecodeError):
            return None
        if (
            inspected.returncode != 0
            or not isinstance(state, dict)
            or state.get("Running") is not True
            or state.get("Status") != "running"
            or state.get("Restarting") is not False
            or not isinstance(state.get("Pid"), int)
            or isinstance(state.get("Pid"), bool)
            or state["Pid"] <= 0
            or not isinstance(state.get("StartedAt"), str)
            or not state["StartedAt"]
        ):
            return None
        ready = subprocess.run(
            [
                "docker",
                "exec",
                self.container,
                "pg_isready",
                "--host",
                "127.0.0.1",
                "--username",
                "polyprop",
                "--dbname",
                "nexpoly",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if ready.returncode != 0:
            return None
        probe = subprocess.run(
            [
                "docker",
                "exec",
                "--env",
                "PGPASSWORD=isolated-test-only-password",
                self.container,
                "psql",
                "-X",
                "--quiet",
                "--tuples-only",
                "--no-align",
                "--set",
                "ON_ERROR_STOP=1",
                "--host",
                "127.0.0.1",
                "--username",
                "polyprop",
                "--dbname",
                "nexpoly",
                "--command",
                "SELECT jsonb_build_object("
                "'database',current_database(),"
                "'session_user',session_user,"
                "'server_version_num',current_setting('server_version_num')::integer,"
                "'system_identifier',(SELECT system_identifier::text FROM pg_control_system()),"
                "'postmaster_start_time',pg_postmaster_start_time()::text,"
                "'in_recovery',pg_is_in_recovery())::text;",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        try:
            postgres = json.loads(probe.stdout.decode().strip())
        except (UnicodeError, json.JSONDecodeError):
            return None
        if (
            probe.returncode != 0
            or not isinstance(postgres, dict)
            or postgres.get("database") != "nexpoly"
            or postgres.get("session_user") != "polyprop"
            or not isinstance(postgres.get("server_version_num"), int)
            or isinstance(postgres.get("server_version_num"), bool)
            or not 160000 <= postgres["server_version_num"] < 170000
            or not isinstance(postgres.get("system_identifier"), str)
            or not postgres["system_identifier"].isdigit()
            or not isinstance(postgres.get("postmaster_start_time"), str)
            or not postgres["postmaster_start_time"]
            or postgres.get("in_recovery") is not False
        ):
            return None
        return {
            "container_pid": state["Pid"],
            "container_started_at": state["StartedAt"],
            "postgres": postgres,
        }

    def _wait_for_final_server(self) -> None:
        deadline = time.monotonic() + 30
        previous: dict[str, object] | None = None
        consecutive = 0
        while time.monotonic() < deadline:
            identity = self._final_server_identity()
            if identity is None:
                previous = None
                consecutive = 0
            elif identity == previous:
                consecutive += 1
            else:
                previous = identity
                consecutive = 1
            if consecutive >= 3:
                return
            time.sleep(0.25)
        logs = subprocess.run(
            ["docker", "logs", "--tail", "40", self.container],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        ).stdout.decode(errors="replace")
        self.fail(
            "isolated PostgreSQL final server did not become stable; "
            f"last identity={previous!r}; logs={logs}"
        )

    def setUp(self) -> None:
        self.container = f"nexpoly-role-cas-test-{os.getpid()}"
        inspected = subprocess.run(
            ["docker", "container", "inspect", self.container],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        self.assertNotEqual(inspected.returncode, 0, "test container already exists")
        subprocess.run(
            [
                "docker", "run", "-d", "--name", self.container,
                "--tmpfs", "/var/lib/postgresql/data:rw,noexec,nosuid,nodev",
                "-e", "POSTGRES_USER=polyprop",
                "-e", "POSTGRES_PASSWORD=isolated-test-only-password",
                "-e", "POSTGRES_DB=nexpoly",
                self.image,
            ],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        self._wait_for_final_server()
        schema_sql = "\n".join(
            f"CREATE SCHEMA {schema};"
            for schema in (
                "core", "dft", "experimental", "governance", "knowledge",
                "lab", "md", "model_registry", "monomer_dft",
                "online_knowledge", "pi",
            )
        )
        schema_sql += "\nCREATE TABLE core.polymers (id bigint PRIMARY KEY);\n"
        subprocess.run(
            [
                "docker", "exec", "-i", self.container, "psql", "-X", "--quiet",
                "--set", "ON_ERROR_STOP=1", "-U", "polyprop", "-d", "nexpoly",
                "--file=-",
            ],
            input=schema_sql.encode(),
            check=True,
        )

    def tearDown(self) -> None:
        subprocess.run(
            ["docker", "rm", "-f", self.container],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )

    def test_transaction_internal_cas_rejects_drift_before_any_mutation(self) -> None:
        before = ROLE._admin_json(self.container)
        desired = ROLE._desired_state(
            {"database": before, "pgpass_login_matches": False}
        )["database"]
        verifier = ROLE._scram_verifier(
            bytearray(b"isolated-audit-test-password"), salt=bytes(range(16))
        )
        payload = ROLE._transaction_sql(
            (SCRIPT.parents[1] / ROLE.ROLE_SQL_PATH).read_bytes(),
            verifier,
            before_database=before,
            desired_database=desired,
        )
        begin = b"BEGIN TRANSACTION ISOLATION LEVEL SERIALIZABLE;\n"
        boundary = payload.index(begin) + len(begin)
        process = subprocess.Popen(
            [
                "docker", "exec", "-i", self.container, "psql", "-X", "--quiet",
                "--set", "ON_ERROR_STOP=1", "-U", "polyprop", "-d", "nexpoly",
                "--file=-",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert process.stdin is not None
        process.stdin.write(payload[:boundary])
        process.stdin.flush()
        # The serializable transaction exists but has not acquired a snapshot
        # or executed the in-transaction CAS.  A concurrent DBA commits a
        # third state in the old observation-to-mutation window.
        subprocess.run(
            [
                "docker", "exec", self.container, "psql", "-X", "--quiet",
                "--set", "ON_ERROR_STOP=1", "-U", "polyprop", "-d", "nexpoly",
                "--command",
                "CREATE ROLE nexpoly_mutable_audit LOGIN; "
                "ALTER ROLE nexpoly_mutable_audit SET statement_timeout='1s';",
            ],
            check=True,
        )
        process.stdin.write(payload[boundary:])
        process.stdin.close()
        process.stdin = None
        stdout, stderr = process.communicate(timeout=30)
        self.assertNotEqual(process.returncode, 0, stdout.decode(errors="replace"))
        self.assertIn(
            "sealed mutable-audit before/desired CAS differs",
            stderr.decode(errors="replace"),
        )
        evidence = subprocess.run(
            [
                "docker", "exec", self.container, "psql", "-X", "--quiet",
                "--tuples-only", "--no-align", "-U", "polyprop", "-d", "nexpoly",
                "--command",
                "SELECT jsonb_build_object("
                "'settings',rolconfig,'password_is_null',auth.rolpassword IS NULL,"
                "'public_lo_create',has_function_privilege('public','pg_catalog.lo_create(oid)','EXECUTE'),"
                "'defaults',(SELECT count(*) FROM pg_default_acl d CROSS JOIN LATERAL aclexplode(d.defaclacl) a WHERE a.grantee=roles.oid))::text "
                "FROM pg_roles roles JOIN pg_authid auth ON auth.oid=roles.oid "
                "WHERE roles.rolname='nexpoly_mutable_audit';",
            ],
            check=True,
            stdout=subprocess.PIPE,
        )
        observed = json.loads(evidence.stdout.decode().strip())
        self.assertEqual(
            observed,
            {
                "settings": ["statement_timeout=1s"],
                "password_is_null": True,
                "public_lo_create": True,
                "defaults": 0,
            },
        )

    def test_transaction_internal_cas_accepts_exact_before_and_commits_desired(self) -> None:
        before = ROLE._admin_json(self.container)
        desired = ROLE._desired_state(
            {"database": before, "pgpass_login_matches": False}
        )["database"]
        verifier = ROLE._scram_verifier(
            bytearray(b"isolated-audit-test-password"), salt=bytes(range(16))
        )
        payload = ROLE._transaction_sql(
            (SCRIPT.parents[1] / ROLE.ROLE_SQL_PATH).read_bytes(),
            verifier,
            before_database=before,
            desired_database=desired,
        )
        subprocess.run(
            [
                "docker", "exec", "-i", self.container, "psql", "-X", "--quiet",
                "--set", "ON_ERROR_STOP=1", "-U", "polyprop", "-d", "nexpoly",
                "--file=-",
            ],
            input=payload,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        self.assertEqual(ROLE._admin_json(self.container), desired)


class MutableAuditRoleApplyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.runtime = self.root / "runtime"
        for relative in ("state", "config", "audit"):
            (self.runtime / relative).mkdir(mode=0o700, parents=True)
        _write_private(self.runtime / "state/deploy.lock", b"")
        self.old_runtime = ROLE.RUNTIME_ROOT
        self.old_pgpass = ROLE.PGPASS_PATH
        ROLE.RUNTIME_ROOT = self.runtime
        ROLE.PGPASS_PATH = self.runtime / "config/mutable-data-audit.pgpass"
        _write_private(
            ROLE.PGPASS_PATH,
            b"127.0.0.1:55432:nexpoly:nexpoly_mutable_audit:unit-test-secret-value\n",
        )
        self.sha = "a" * 40
        self.operation = "mutable-role-unit-test-0001"
        self.source = {
            "sha": self.sha,
            "tree": "b" * 40,
            "remote_main": self.sha,
            "script_sha256": "sha256:" + "1" * 64,
            "role_sql_sha256": "sha256:" + "2" * 64,
        }
        self.role_sql = (
            b"\\set ON_ERROR_STOP on\nBEGIN;\n"
            b"__IN_TRANSACTION_SEALED_CAS__\nALTER ROLE x PASSWORD "
            b"__SCRAM_VERIFIER_LITERAL__;\n"
            b"__IN_TRANSACTION_DESIRED_ASSERT__\nCOMMIT;\n"
        )
        self.adoption = {
            "adopted_sha256": "sha256:" + "3" * 64,
            "current_delivery_gate": _delivery_gate(self.sha),
        }
        self.postgres = {
            "container_id": "c" * 64,
            "image_id": "sha256:" + "4" * 64,
            "stable": True,
        }
        contract = MutableAuditRoleContractTests()
        self.before = {"database": contract._database(), "pgpass_login_matches": False}
        self.desired = ROLE._desired_state(self.before)
        self.plan = {
            "schema_version": 2,
            "action": "provision-mutable-data-audit-role",
            "apply": False,
            "operation_id": self.operation,
            "source": self.source,
            "role_sql": {"path": ROLE.ROLE_SQL_PATH, "sha256": ROLE._digest_bytes(self.role_sql)},
            "pgpass": {"path": str(ROLE.PGPASS_PATH), "sha256": "sha256:" + "5" * 64},
            "adoption": self.adoption,
            "postgres": self.postgres,
            "before": self.before,
            "before_sha256": ROLE._digest(self.before),
            "desired": self.desired,
            "desired_sha256": ROLE._digest(self.desired),
            "already_exact": False,
            "public_lo_acl_impact": {"sealed": True},
            "public_lo_acl_impact_sha256": ROLE._digest({"sealed": True}),
            "mutations": [],
        }
        self.plan["plan_sha256"] = ROLE._digest(self.plan)

    def tearDown(self) -> None:
        ROLE.RUNTIME_ROOT = self.old_runtime
        ROLE.PGPASS_PATH = self.old_pgpass
        self.temporary.cleanup()

    def _patches(self, observations: list[dict[str, object]]):
        return (
            mock.patch.object(ROLE, "build_plan", return_value=copy.deepcopy(self.plan)),
            mock.patch.object(ROLE, "_source_authority", return_value=(self.source, self.role_sql)),
            mock.patch.object(ROLE, "_strict_adopted_authority", return_value=self.adoption),
            mock.patch.object(
                ROLE,
                "_pgpass_authority",
                return_value=(bytearray(b"unit-test-secret-value"), self.plan["pgpass"]["sha256"]),
            ),
            mock.patch.object(ROLE, "_live_postgres", return_value=self.postgres),
            mock.patch.object(ROLE, "_observe_state", side_effect=observations),
        )

    def test_apply_uses_one_transaction_and_writes_secret_free_report(self) -> None:
        patches = self._patches([self.before, self.desired, self.desired])
        with patches[0], patches[1], patches[2] as adopted_authority, patches[3], patches[4], patches[5], mock.patch.object(
            ROLE, "_apply_transaction"
        ) as apply_transaction:
            result = ROLE.apply_plan(
                self.sha,
                self.operation,
                self.plan["plan_sha256"],
                self.plan["public_lo_acl_impact_sha256"],
            )
        apply_transaction.assert_called_once()
        adopted_authority.assert_called_once_with(
            self.sha,
            sealed_delivery_gate=self.adoption["current_delivery_gate"],
        )
        payload = apply_transaction.call_args.args[1]
        # The production implementation wipes after subprocess completion; the
        # mocked call sees the generated verifier but no plaintext password.
        self.assertNotIn(b"unit-test-secret-value", payload)
        serialized = json.dumps(result, sort_keys=True)
        self.assertNotIn("unit-test-secret-value", serialized)
        self.assertNotIn("SCRAM-SHA-256$", serialized)
        self.assertFalse(result["report"]["commit_response_recovered"])
        journal = json.loads(
            (self.runtime / "audit/mutable-data-role" / self.operation / "journal.json").read_text()
        )
        self.assertEqual(journal["phase"], "completed")
        self.assertEqual([item["phase"] for item in journal["history"]], list(ROLE.JOURNAL_PHASES))

    def test_commit_response_loss_recovers_only_from_exact_desired_state(self) -> None:
        operation_dir = self.runtime / "audit/mutable-data-role" / self.operation
        operation_dir.mkdir(mode=0o700, parents=True)
        journal = ROLE._journal_document(
            operation_id=self.operation,
            plan=self.plan,
            phase="intent",
            previous=None,
        )
        journal = ROLE._journal_document(
            operation_id=self.operation,
            plan=self.plan,
            phase="database-commit-intent",
            previous=journal,
        )
        _write_private(operation_dir / "journal.json", journal)
        patches = self._patches([self.desired, self.desired])
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], mock.patch.object(
            ROLE, "_apply_transaction"
        ) as apply_transaction:
            result = ROLE.apply_plan(
                self.sha,
                self.operation,
                self.plan["plan_sha256"],
                self.plan["public_lo_acl_impact_sha256"],
            )
        apply_transaction.assert_not_called()
        self.assertTrue(result["report"]["commit_response_recovered"])

    def test_intent_recovery_rejects_current_delivery_gate_drift_before_transaction(
        self,
    ) -> None:
        operation_dir = self.runtime / "audit/mutable-data-role" / self.operation
        operation_dir.mkdir(mode=0o700, parents=True)
        journal = ROLE._journal_document(
            operation_id=self.operation,
            plan=self.plan,
            phase="intent",
            previous=None,
        )
        _write_private(operation_dir / "journal.json", journal)
        patches = self._patches([self.before])

        def drifted_authority(
            source_sha: str, *, sealed_delivery_gate: object = None
        ) -> dict[str, object]:
            self.assertEqual(source_sha, self.sha)
            self.assertEqual(
                sealed_delivery_gate,
                self.adoption["current_delivery_gate"],
            )
            raise ROLE.RoleProvisionError("sealed current delivery gate changed")

        with patches[0], patches[1], mock.patch.object(
            ROLE,
            "_strict_adopted_authority",
            side_effect=drifted_authority,
        ), patches[3], patches[4], patches[5], mock.patch.object(
            ROLE, "_apply_transaction"
        ) as apply_transaction:
            with self.assertRaisesRegex(
                ROLE.RoleProvisionError, "sealed current delivery gate changed"
            ):
                ROLE.apply_plan(
                    self.sha,
                    self.operation,
                    self.plan["plan_sha256"],
                    self.plan["public_lo_acl_impact_sha256"],
                )
        apply_transaction.assert_not_called()

    def test_apply_rejects_third_state_and_wrong_lo_confirmation(self) -> None:
        drift = copy.deepcopy(self.before)
        drift["database"]["memberships"] = [{"role": "unexpected"}]
        patches = self._patches([drift])
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            with self.assertRaisesRegex(ROLE.RoleProvisionError, "neither sealed before nor desired"):
                ROLE.apply_plan(
                    self.sha,
                    self.operation,
                    self.plan["plan_sha256"],
                    self.plan["public_lo_acl_impact_sha256"],
                )
        other_operation = "mutable-role-unit-test-0002"
        plan = copy.deepcopy(self.plan)
        plan["operation_id"] = other_operation
        plan["plan_sha256"] = ROLE._digest({k: v for k, v in plan.items() if k != "plan_sha256"})
        with mock.patch.object(ROLE, "build_plan", return_value=plan):
            with self.assertRaisesRegex(ROLE.RoleProvisionError, "PUBLIC LO ACL impact"):
                ROLE.apply_plan(
                    self.sha,
                    other_operation,
                    plan["plan_sha256"],
                    "sha256:" + "9" * 64,
                )

    def test_journal_rejects_skipped_phase(self) -> None:
        journal = ROLE._journal_document(
            operation_id=self.operation,
            plan=self.plan,
            phase="intent",
            previous=None,
        )
        journal["phase"] = "verified"
        journal["history"].append({"phase": "verified", "recorded_at": ROLE._utc_now()})
        with self.assertRaisesRegex(ROLE.RoleProvisionError, "history"):
            ROLE._validate_journal(journal, self.operation)

    def test_crash_staged_journal_recovers_exact_next_phase(self) -> None:
        operation_dir = self.runtime / "audit/mutable-data-role" / self.operation
        operation_dir.mkdir(mode=0o700, parents=True)
        intent = ROLE._journal_document(
            operation_id=self.operation,
            plan=self.plan,
            phase="intent",
            previous=None,
        )
        _write_private(operation_dir / "journal.json", intent)
        next_state = ROLE._journal_document(
            operation_id=self.operation,
            plan=self.plan,
            phase="database-commit-intent",
            previous=intent,
        )
        _write_private(operation_dir / ".journal.json.next", next_state)
        ROLE._recover_journal_staging(operation_dir / "journal.json", self.operation)
        self.assertEqual(
            json.loads((operation_dir / "journal.json").read_text()), next_state
        )
        self.assertFalse((operation_dir / ".journal.json.next").exists())

    def test_completed_authority_recovers_staging_and_hardlink_window(self) -> None:
        operation_dir = self.runtime / "audit/mutable-data-role" / self.operation
        operation_dir.mkdir(mode=0o700, parents=True)
        report = {"safe": True}
        completed = {
            "schema_version": 2,
            "status": "completed",
            "operation_id": self.operation,
            "source_sha": self.sha,
            "plan_sha256": self.plan["plan_sha256"],
            "report": report,
            "report_sha256": ROLE._digest(report),
        }
        staging = operation_dir / ".completed.json.next"
        final = operation_dir / "completed.json"
        _write_private(staging, completed)
        ROLE._promote_completed_staging(
            final,
            source_sha=self.sha,
            operation_id=self.operation,
            plan_sha256=self.plan["plan_sha256"],
        )
        self.assertTrue(final.exists())
        self.assertFalse(staging.exists())
        # Simulate a crash after the create-only hard link and before staging
        # cleanup; recovery must accept only the same inode.
        os.link(final, staging)
        ROLE._promote_completed_staging(
            final,
            source_sha=self.sha,
            operation_id=self.operation,
            plan_sha256=self.plan["plan_sha256"],
        )
        self.assertEqual(final.stat().st_nlink, 1)


if __name__ == "__main__":
    unittest.main()
