from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest


REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[2]


class MonomerDftEnvironmentSetupTests(unittest.TestCase):
    def _clone_current_head(self, destination: pathlib.Path) -> None:
        subprocess.run(
            [
                "git",
                "clone",
                "--quiet",
                "--depth",
                "1",
                "--no-hardlinks",
                "--no-checkout",
                REPOSITORY_ROOT.as_uri(),
                str(destination),
            ],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(destination), "checkout", "--quiet", "--detach", "HEAD"],
            check=True,
        )

    @staticmethod
    def _commit_environment(environment: dict[str, str]) -> dict[str, str]:
        return environment | {
            "GIT_AUTHOR_NAME": "DFT setup test",
            "GIT_AUTHOR_EMAIL": "dft-setup-test@example.invalid",
            "GIT_COMMITTER_NAME": "DFT setup test",
            "GIT_COMMITTER_EMAIL": "dft-setup-test@example.invalid",
        }

    def test_repository_check_accepts_an_arbitrary_clean_detached_clone(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            clone = pathlib.Path(raw) / "portable-dft-worktree"
            self._clone_current_head(clone)
            environment = os.environ.copy()
            environment.pop("PYTHONPATH", None)

            completed = subprocess.run(
                [str(clone / "scripts" / "setup_monomer_dft_env.sh"), "--check-repository"],
                cwd=clone,
                env=environment,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            self.assertIn("repository governance checks passed", completed.stdout)

    def test_repository_check_honors_an_explicit_expected_ref(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            clone = pathlib.Path(raw) / "pinned-dft-worktree"
            self._clone_current_head(clone)
            environment = os.environ.copy()
            environment.pop("PYTHONPATH", None)
            git_environment = self._commit_environment(environment)
            different_commit = subprocess.run(
                ["git", "-C", str(clone), "commit-tree", "HEAD^{tree}"],
                env=git_environment,
                input="different expected commit\n",
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            ).stdout.strip()
            environment["MONOMER_DFT_EXPECTED_GIT_REF"] = different_commit

            completed = subprocess.run(
                [str(clone / "scripts" / "setup_monomer_dft_env.sh"), "--check-repository"],
                cwd=clone,
                env=environment,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            self.assertEqual(completed.returncode, 2)
            self.assertIn("HEAD does not match MONOMER_DFT_EXPECTED_GIT_REF", completed.stderr)

    def test_repository_check_rejects_model_file_path_traversal_in_a_clean_clone(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            clone = pathlib.Path(raw) / "unsafe-model-lock-worktree"
            self._clone_current_head(clone)
            environment = os.environ.copy()
            environment.pop("PYTHONPATH", None)
            setup_script = clone / "scripts" / "setup_monomer_dft_env.sh"
            shutil.copy2(REPOSITORY_ROOT / "scripts" / "setup_monomer_dft_env.sh", setup_script)
            lock_path = clone / "workers" / "monomer_dft_worker" / "aimnet-source.lock.json"
            lock = json.loads(lock_path.read_text(encoding="utf-8"))
            lock["models"][0]["file"] = "../../escaped-model.pt"
            lock_path.write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(clone),
                    "add",
                    str(setup_script.relative_to(clone)),
                    str(lock_path.relative_to(clone)),
                ],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(clone), "commit", "--quiet", "-m", "unsafe lock fixture"],
                env=self._commit_environment(environment),
                check=True,
            )

            completed = subprocess.run(
                [str(clone / "scripts" / "setup_monomer_dft_env.sh"), "--check-repository"],
                cwd=clone,
                env=environment,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            self.assertEqual(completed.returncode, 2)
            self.assertIn("unsafe model metadata", completed.stderr)

    def test_repository_check_does_not_import_untracked_modules_from_clone(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            clone = pathlib.Path(raw) / "isolated-metadata-worktree"
            self._clone_current_head(clone)
            environment = os.environ.copy()
            environment.pop("PYTHONPATH", None)
            setup_script = clone / "scripts" / "setup_monomer_dft_env.sh"
            shutil.copy2(REPOSITORY_ROOT / "scripts" / "setup_monomer_dft_env.sh", setup_script)
            subprocess.run(
                ["git", "-C", str(clone), "add", str(setup_script.relative_to(clone))],
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(clone),
                    "commit",
                    "--quiet",
                    "--allow-empty",
                    "-m",
                    "isolated metadata fixture",
                ],
                env=self._commit_environment(environment),
                check=True,
            )
            sentinel = clone / ".untracked-json-imported"
            (clone / "json.py").write_text(
                f"open({str(sentinel)!r}, 'w', encoding='utf-8').write('executed\\n')\n"
                "raise RuntimeError('untracked json module was imported')\n",
                encoding="utf-8",
            )

            completed = subprocess.run(
                [str(setup_script), "--check-repository"],
                cwd=clone,
                env=environment,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("repository governance checks passed", completed.stdout)
            self.assertFalse(sentinel.exists())

    def test_aimnet_source_check_rejects_an_untracked_file(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            clone = root / "dft-worktree"
            self._clone_current_head(clone)
            aimnet = clone / ".runtime" / "aimnet-source-clone"
            aimnet.parent.mkdir(mode=0o700)
            environment = os.environ.copy()
            environment.pop("PYTHONPATH", None)
            git_environment = self._commit_environment(environment)
            setup_script = clone / "scripts" / "setup_monomer_dft_env.sh"
            shutil.copy2(
                REPOSITORY_ROOT / "scripts" / "setup_monomer_dft_env.sh",
                setup_script,
            )

            subprocess.run(["git", "init", "--quiet", str(aimnet)], check=True)
            (aimnet / "pyproject.toml").write_text(
                "[build-system]\nrequires = []\nbuild-backend = 'unused'\n",
                encoding="utf-8",
            )
            subprocess.run(
                ["git", "-C", str(aimnet), "add", "pyproject.toml"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(aimnet), "commit", "--quiet", "-m", "fixture"],
                env=git_environment,
                check=True,
            )
            repository_url = "https://example.invalid/clean-aimnet.git"
            subprocess.run(
                ["git", "-C", str(aimnet), "remote", "add", "origin", repository_url],
                check=True,
            )
            aimnet_commit = subprocess.run(
                ["git", "-C", str(aimnet), "rev-parse", "HEAD"],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            ).stdout.strip()
            aimnet_tree = subprocess.run(
                ["git", "-C", str(aimnet), "rev-parse", "HEAD^{tree}"],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            ).stdout.strip()
            source_date_epoch = int(
                subprocess.run(
                    ["git", "-C", str(aimnet), "show", "-s", "--format=%ct", "HEAD"],
                    check=True,
                    text=True,
                    stdout=subprocess.PIPE,
                ).stdout.strip()
            )

            lock_path = clone / "workers" / "monomer_dft_worker" / "aimnet-source.lock.json"
            lock = json.loads(lock_path.read_text(encoding="utf-8"))
            lock["source"]["repository_url"] = repository_url
            lock["source"]["commit"] = aimnet_commit
            lock["source"]["tree"] = aimnet_tree
            lock["source"]["source_date_epoch"] = source_date_epoch
            lock_path.write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(clone),
                    "add",
                    str(setup_script.relative_to(clone)),
                    str(lock_path.relative_to(clone)),
                ],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(clone), "commit", "--quiet", "-m", "source fixture"],
                env=git_environment,
                check=True,
            )
            environment["AIMNET_SOURCE_CLONE"] = str(aimnet)

            clean = subprocess.run(
                [str(setup_script), "--check-aimnet-source"],
                cwd=clone,
                env=environment,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(clean.returncode, 0, clean.stderr)
            self.assertIn("clean AIMNet source checks passed", clean.stdout)

            (aimnet / "running_code").mkdir()
            (aimnet / "running_code" / "local.py").write_text(
                "raise RuntimeError('must not participate')\n",
                encoding="utf-8",
            )
            dirty = subprocess.run(
                [str(setup_script), "--check-aimnet-source"],
                cwd=clone,
                env=environment,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(dirty.returncode, 2)
            self.assertIn("AIMNet source clone is dirty", dirty.stderr)

            shutil.rmtree(aimnet / "running_code")
            (aimnet / "later.txt").write_text("not the locked commit\n", encoding="utf-8")
            subprocess.run(
                ["git", "-C", str(aimnet), "add", "later.txt"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(aimnet), "commit", "--quiet", "-m", "later"],
                env=git_environment,
                check=True,
            )
            wrong_head = subprocess.run(
                [str(setup_script), "--check-aimnet-source"],
                cwd=clone,
                env=environment,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(wrong_head.returncode, 2)
            self.assertIn(
                "clean AIMNet clone HEAD must equal the locked commit",
                wrong_head.stderr,
            )

    def test_all_direct_python_metadata_and_preflight_calls_are_isolated(self) -> None:
        script = (REPOSITORY_ROOT / "scripts" / "setup_monomer_dft_env.sh").read_text(
            encoding="utf-8"
        )
        direct_invocations = [
            line.strip()
            for line in script.splitlines()
            if line.strip().startswith(
                ("python3 ", '"$BOOTSTRAP_PYTHON" ', '"$VENV_PYTHON" ')
            )
        ]

        self.assertGreaterEqual(len(direct_invocations), 9)
        self.assertIn(
            'env -u CUDA_VISIBLE_DEVICES "$VENV_PYTHON" -I '
            '"$SCRIPT_DIR/preflight_monomer_dft_env.py"',
            script,
        )
        for invocation in direct_invocations:
            with self.subTest(invocation=invocation):
                if invocation.startswith("python3 "):
                    self.assertTrue(invocation.startswith("python3 -I "))
                elif invocation.startswith('"$BOOTSTRAP_PYTHON" '):
                    self.assertTrue(invocation.startswith('"$BOOTSTRAP_PYTHON" -I '))
                else:
                    self.assertTrue(invocation.startswith('"$VENV_PYTHON" -I '))

    def test_aimnet_source_defaults_to_a_private_standalone_runtime_clone(self) -> None:
        script = (REPOSITORY_ROOT / "scripts" / "setup_monomer_dft_env.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            'DEFAULT_AIMNET_CLONE="$RUNTIME_ROOT/aimnet-source-clone"',
            script,
        )
        self.assertIn("clean AIMNet clone HEAD must equal the locked commit", script)
        self.assertIn(
            "AIMNet source must be a standalone clone without shared Git objects",
            script,
        )
        self.assertNotIn("/data/lzq/gith/aimnetcentral", script)


if __name__ == "__main__":
    unittest.main()
