"""The optional native quota manager pins both sides of the dedicated NFS transport."""
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import openscience_per_user_dev as manager

nfs = manager.pilot_module("quota_nfs")


class QuotaNfsTest(unittest.TestCase):
    def setUp(self):
        area = TemporaryDirectory()
        self.addCleanup(area.cleanup)
        self.root = Path(area.name)
        self.registry = manager.Registry(self.root)
        self.registry.data = {"format": 1, "workspaces": {}, "deployment": uuid4().hex, "image": "reviewed-image",
                              "template_version": manager.VERSION, "max_active": 2}
        self.ssh = self.root / "ssh.conf"
        self.ssh.write_text("Host m5-quota-guest")
        self.ssh.chmod(0o600)
        self.path = self.root / "manifest"
        self.bindings, self.rows = [], {}
        for slot in nfs.SLOTS:
            suffix = slot[-1]
            b = {"environment": "dev-m1", "user_id": "test-user-" + suffix, "workspace": suffix * 32,
                 "uid": 220000 + int(suffix == "b"), "mount": nfs.GUEST_ROOT + "/" + slot,
                 "filesystem_uuid": str(uuid4()), "generation": 24}
            self.rows[slot] = {**b, "pseudo": "/m5-" + slot, "volume": "nexpoly-m5-a0123456789f-" + slot}
            if slot.startswith("src-"):
                self.bindings.append(b)
        self.manifest = {"format": "nexpoly-m5-nfs-v1", "run_id": "a0123456789f", "workspaces": self.rows,
                         "ssh_config": str(self.ssh), "ssh_alias": "m5-quota-guest", "port": 38901}
        self.write_manifest()
        self.ep = nfs.endpoints(self.path, "src", self.bindings)["dev-m1/test-user-a"]
        self.claim = nfs.claim_record("src", *nfs.ownership(self.registry), *(b["filesystem_uuid"] for b in self.bindings))

    def write_manifest(self):
        self.path.write_text(json.dumps(self.manifest))
        self.path.chmod(0o600)

    def test_deploy_registry_requires_named_owners_verified_quota_and_original_host_media(self):
        owners, members = {}, []
        primary = self.root.stat().st_dev
        filesystem = f"{os.major(primary)}:{os.minor(primary)}"
        disks = {"primary": {"device": "259:0", "name": "nvme0n1"}}
        inodes = {}
        for role, binding in zip("ab", self.bindings):
            account = str(uuid4())
            binding.update(environment="deploy-m6", user_id=account)
            owners[role] = {k: binding[k] for k in ("environment", "user_id", "workspace", "uid")}
            owners[role]["gid"] = binding["uid"]
            members.append({"user_id": account, "workspace_user_id": account})
            self.rows["src-" + role].update(binding)
            backing = self.root / f"m5-{role}.raw"
            backing.touch()
            inodes[backing.stem] = backing.stat().st_ino
        roster = {"environment": "deploy-m6", "members": members}
        with self.assertRaises(manager.Problem):
            manager.candidate_roster(owners, {**roster, "members": members[:1] * 2})
        with self.assertRaises(manager.Problem):
            manager.candidate_roster(owners, {**roster, "members": members[::-1]})
        self.registry.data.update(format=6, candidate_owners=manager.candidate_roster(owners, roster))
        self.write_manifest()
        vm = self.root / "vm.json"
        vm.write_text(json.dumps({"format": "nexpoly-m5-isolated-browser-v1", "disk_inodes": inodes, "host_disks": disks}))
        vm.chmod(0o600)
        account = owners["a"]["user_id"]
        with self.assertRaises(manager.Problem):
            self.registry.workspace("deploy-m6", account, create=True)
        with self.assertRaises(manager.Problem):
            self.registry.register_quota_storage([{**self.bindings[0], "uid": owners["a"]["uid"] + 10}, self.bindings[1]],
                                                 manifest=self.path, phase="src")
        with patch("openscience_m5_backup.physical_disk", return_value=(disks["primary"], "disk")) as physical, \
                patch.object(nfs, "attest", return_value={}), patch.object(nfs, "claim_pair", return_value=self.claim), \
                patch.object(manager.pilot_module("quota_web_files"), "prepare"):
            self.registry.register_quota_storage(self.bindings[::-1], manifest=self.path, phase="src")
        physical.assert_called_with(filesystem)
        fresh = manager.Registry(self.root)
        ws = fresh.workspace("deploy-m6", account)
        with patch.object(ws, "resource", return_value=ws.volume), patch.object(ws, "helper", return_value="900000"), \
                patch.object(nfs, "host_free_mib", return_value=99):
            self.assertEqual(ws.volume_free_mib(), 99)
        for environment, user in [("dev-m1", "test-user-a"), ("deploy-m6", str(uuid4()))]:
            with self.assertRaises(manager.Problem):
                fresh.workspace(environment, user, create=True)
        source = self.root / "m5-a.raw"
        source.rename(self.root / "saved")
        source.touch()
        with self.assertRaisesRegex(ValueError, "storage medium"):
            nfs.host_free_mib(fresh.data["host_media"]["deploy-m6/" + account])

    def test_cross_host_dst_volume_uses_primary_recovery_disk_and_pinned_inode(self):
        backup = self.root / "backup"
        backup.mkdir()
        data = self.root / "m5-restore-a.raw"
        data.touch()
        config = {"format": "nexpoly-m5-isolated-browser-v1", "restore_target": True, "backup_dir": str(backup),
                  "disk_inodes": {data.stem: data.stat().st_ino}, "host_disks": {"primary": "primary", "backup": "backup"}}
        vm = self.root / "vm.json"
        vm.write_text(json.dumps(config))
        vm.chmod(0o600)
        endpoint = {"slot": "dst-a", "ssh_config": str(self.ssh)}
        with patch("openscience_m5_backup.physical_disk", return_value=("primary", "disk")):
            self.assertEqual(nfs.candidate_medium(endpoint)["file"], str(data))
            data.rename(self.root / "previous")
            data.touch()
            with self.assertRaisesRegex(ValueError, "storage medium"):
                nfs.candidate_medium(endpoint)

    def volume(self):
        return ["local", {"type": "nfs", "device": ":/m5-src-a/workspace",
                          "o": "addr=127.0.0.1,port=38901,nfsvers=4.1,proto=tcp,resvport,hard,timeo=30,retrans=2"},
                {"org.nexpoly.m5.native": self.manifest["run_id"], "org.nexpoly.m5.slot": "src-a",
                 "org.nexpoly.m5.filesystem-uuid": self.bindings[0]["filesystem_uuid"]}]

    @staticmethod
    def inspector(fields):
        return Mock(return_value="\n".join(map(json.dumps, fields)))

    def test_entire_pair_registered_or_none_and_no_local_mount_fallback(self):
        with patch.object(nfs, "attest", side_effect=[{}, ValueError("B quota is not enforced")]), self.assertRaises(ValueError):
            self.registry.register_quota_storage(self.bindings, manifest=self.path, phase="src")
        self.assertFalse(self.registry.file.exists())
        with patch.object(nfs, "attest", return_value={}), patch.object(nfs, "claim_pair", return_value=self.claim), \
                patch.object(manager.pilot_module("quota_web_files"), "prepare"):
            result = self.registry.register_quota_storage(self.bindings, manifest=self.path, phase="src")
        self.assertEqual(result["storage_backend"], "nfs-ext4-user-quota")
        fresh = manager.Registry(self.root)
        ws = fresh.workspace("dev-m1", "test-user-a")
        self.assertEqual(ws.volume, self.ep["volume"])
        with patch.object(ws, "helper") as helper, patch.object(nfs, "attest", return_value={}):
            ws.prepare_volume()
            helper.assert_called_once_with(nfs.CLIENT_CHECK, "/data", "src-a", "38901", self.bindings[0]["workspace"], "220000")
        self.assertEqual(ws.data_mount("/openscience"), "type=volume,src=" + self.ep["volume"] + ",dst=/openscience,volume-nocopy")
        with patch.object(nfs, "attest", side_effect=ValueError("guest offline")), patch.object(ws, "helper") as helper, \
                patch.object(manager, "docker") as docker, self.assertRaisesRegex(ValueError, "guest offline"):
            ws.prepare_volume()
        helper.assert_not_called()
        docker.assert_not_called()
        del fresh.data["nfs_endpoints"]
        with self.assertRaisesRegex(manager.Problem, "endpoint"):
            fresh.workspace("dev-m1", "test-user-a")

    def test_first_claim_wins_atomically_and_retries_do_not_change_disk_ownership(self):
        directory = self.root / "claims"
        records = [{**self.claim, "deployment": uuid4().hex} for _ in range(8)]
        def compete(record):
            try:
                return nfs.apply_claim(directory, record, True)
            except ValueError:
                return None
        with ThreadPoolExecutor(max_workers=8) as pool:
            winners = [x for x in pool.map(compete, records) if x]
        self.assertEqual(len(winners), 1)
        winner = winners[0]
        self.assertEqual(nfs.apply_claim(directory, winner, False), winner)
        self.assertEqual(nfs.apply_claim(directory, winner, True), winner)
        self.assertEqual(len(list(directory.iterdir())), 1)
        for change in ({"registry_sha256": "b" * 64}, {"deployment": "f" * 32},
                       {"filesystem_uuids": {"a": str(uuid4()), "b": str(uuid4())}}):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, "another registry"):
                nfs.apply_claim(directory, {**winner, **change}, True)
        with self.assertRaises(FileNotFoundError):
            nfs.apply_claim(directory, {**winner, "phase": "dst"}, False)

    def test_confirmed_publication_syncs_both_claims_directory_and_its_parent(self):
        directory = self.root / "claims"
        fsync = os.fsync
        for attempt in ("initial", "replayed"):
            synced = []
            def track(fd):
                synced.append(Path(os.readlink("/proc/self/fd/" + str(fd))))
                fsync(fd)
            with self.subTest(attempt=attempt), patch.object(nfs.os, "fsync", side_effect=track):
                nfs.apply_claim(directory, self.claim, True)
                self.assertEqual(synced[-2:], [directory, directory.parent])

    def test_claim_refuses_foreign_container_and_can_resume_after_local_registry_save_failure(self):
        eps = nfs.endpoints(self.path, "src", self.bindings)
        pins = {b["environment"] + "/" + b["user_id"]: b for b in self.bindings}
        with patch.object(nfs, "guest_client") as remote, self.assertRaisesRegex(ValueError, "Another container"):
            nfs.claim_pair(self.registry, eps, pins, Mock(return_value="foreign-container"))
        remote.assert_not_called()
        directory = self.root / "claims"
        self.registry.save()  # The original format-1 registry fixes the deployment nonce before claiming.
        def request(_python, _script, op, phase, dep, path_hash, a, b, **kwargs):
            record = nfs.apply_claim(directory, nfs.claim_record(phase, dep, path_hash, a, b), op == "claim")
            return SimpleNamespace(stdout=json.dumps(record))
        with patch.object(nfs, "attest", return_value={}), patch.object(manager.pilot_module("quota_web_files"), "prepare"), \
                patch.object(manager, "docker", return_value=""), patch.object(nfs, "guest_client") as client:
            client.return_value.run.side_effect = request
            with patch.object(self.registry, "save", side_effect=OSError("interrupted local save")), self.assertRaises(OSError):
                self.registry.register_quota_storage(self.bindings, manifest=self.path, phase="src")
            reopened = manager.Registry(self.root)
            self.assertEqual(reopened.data["format"], 1)
            receipt = reopened.register_quota_storage(self.bindings, manifest=self.path, phase="src")
            self.assertEqual((reopened.data["format"], receipt["native_claim"]), (4, self.claim))
            a = reopened.workspace("dev-m1", "test-user-a")
            a.row["generation"] += 1
            reopened.save()
            old = manager.Registry(self.root)
            old.data["format"] = 3
            del old.data["native_claim"]
            with self.assertRaisesRegex(manager.Problem, "exclusive registration"):
                old.workspace("dev-m1", "test-user-a")
            old.register_quota_storage(self.bindings, manifest=self.path, phase="src")
            self.assertEqual((old.data["format"], old.workspace("dev-m1", "test-user-a").row["generation"]), (4, 25))
            with TemporaryDirectory() as other:
                copy = manager.Registry(Path(other))
                copy.data = old.data.copy()
                with self.assertRaisesRegex(manager.Problem, "registry or its location"):
                    copy.workspace("dev-m1", "test-user-a")

    def test_manifest_rejects_owner_or_volume_mismatch_and_non_private_metadata(self):
        with self.assertRaises(ValueError):
            nfs.endpoints(self.path, "dst", self.bindings)
        self.rows["src-a"]["volume"] = self.rows["src-b"]["volume"]
        self.write_manifest()
        with self.assertRaisesRegex(ValueError, "differ"):
            nfs.endpoints(self.path, "src", self.bindings)
        self.path.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "0600"):
            nfs.endpoints(self.path, "src", self.bindings)

    def test_scope_or_transport_option_changes_never_pass_docker_inspection(self):
        nfs.inspect_volume(self.ep, self.bindings[0], self.inspector(self.volume()))
        for field, key, value in [(1, "device", ":/m5-src-b/workspace"), (1, "o", "vers=4,soft"), (1, "type", "none"),
                                  (2, "org.nexpoly.m5.filesystem-uuid", str(uuid4()))]:
            with self.subTest(field=field, key=key):
                fields = self.volume()
                fields[field][key] = value
                with self.assertRaisesRegex(ValueError, "driver, options or ownership"):
                    nfs.inspect_volume(self.ep, self.bindings[0], self.inspector(fields))

    def test_live_attestation_requires_pinned_ssh_config_uid_uuid_and_enforced_kernel_bytes(self):
        binding = self.bindings[0]
        quota = {"accounting": True, "enforced": True, "hard_bytes": 2000000000, "uid": binding["uid"],
                 "filesystem_type": "ext4", "filesystem_uuid": binding["filesystem_uuid"], "volume_label": "m5quota-src-a"}
        data = {"binding": {k: binding[k] for k in nfs.IDENTITY}, "quota": quota}
        for changed in [{}, {"enforced": False}, {"hard_bytes": 0}, {"uid": 220001}, {"filesystem_uuid": str(uuid4())}]:
            with self.subTest(changed=changed), patch("openscience_m5_storage_ssh.StorageGuest") as guest:
                guest.return_value.run.return_value = SimpleNamespace(stdout=json.dumps({**data, "quota": {**quota, **changed}}))
                if changed:
                    with self.assertRaisesRegex(ValueError, "attestation"):
                        nfs.attest(self.ep, binding, self.inspector(self.volume()))
                else:
                    self.assertEqual(nfs.attest(self.ep, binding, self.inspector(self.volume())), quota)
                guest.return_value.run.assert_called_once_with("python3", nfs.GUEST_SCRIPT, "attest", "src-a", capture_output=True, timeout=25)
        with patch("openscience_m5_storage_ssh.StorageGuest") as guest:
            guest.return_value.run.return_value.stdout = json.dumps({**data, "claim": self.claim})
            self.assertEqual(nfs.attest(self.ep, binding, self.inspector(self.volume()), owner=self.claim), quota)
            for receipt in (None, {**self.claim, "phase": "dst"}, {**self.claim, "deployment": uuid4().hex},
                            {**self.claim, "filesystem_uuids": {"a": binding["filesystem_uuid"], "b": str(uuid4())}}):
                guest.return_value.run.return_value.stdout = json.dumps({**data, "claim": receipt})
                with self.subTest(receipt=receipt), self.assertRaisesRegex(ValueError, "different native registry"):
                    nfs.attest(self.ep, binding, self.inspector(self.volume()), owner=self.claim)
        self.ssh.write_text("changed operator settings")
        with patch("openscience_m5_storage_ssh.StorageGuest") as guest, self.assertRaisesRegex(ValueError, "SSH config"):
            nfs.attest(self.ep, binding, self.inspector(self.volume()))
        guest.assert_not_called()

    def test_container_mount_rejects_wrong_export_protocol_and_nested_mount(self):
        line = "1 2 0:699 / /data ro - nfs4 :/m5-src-a/workspace rw,vers=4.1,hard,proto=tcp,port=38901,sec=sys,addr=127.0.0.1"
        args = ["-c", *nfs.client_args(self.ep, self.bindings[0], "/data")]
        for text in (line.replace("src-a", "dst-a"), line.replace(",hard,", ",soft,"),
                     line.replace("38901", "38902"), line + "\n" + line.replace(" /data ", " /data/foreign ")):
            with patch.object(Path, "read_text", return_value=text), patch.object(sys, "argv", args), self.assertRaises(SystemExit):
                exec(nfs.CLIENT_CHECK, {})


if __name__ == "__main__":
    unittest.main()
