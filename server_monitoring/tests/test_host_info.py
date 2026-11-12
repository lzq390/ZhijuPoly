import os

import pytest

from server_monitoring import collector
from server_monitoring.models import HostInfo, validate_snapshot


def cpu_record(processor, socket=0, core=0, model="Example CPU"):
    return f"processor : {processor}\nmodel name : {model}\nphysical id : {socket}\ncore id : {core}\n\n"


def complete_info(**changes):
    result = {"cpu_model": "Example CPU", "cpu_socket_count": 2, "cpu_physical_core_count": 4,
              "os_name": "Example Linux 1.0", "kernel_release": "6.8.0-test", "nvidia_driver_version": "535.288.01"}
    result.update(changes)
    return result


@pytest.fixture
def source_files(tmp_path):
    proc = tmp_path / "proc"
    (proc / "sys/kernel").mkdir(parents=True)
    (proc / "driver/nvidia").mkdir(parents=True)
    (proc / "cpuinfo").write_text("".join(cpu_record(index, index // 4, index % 4 // 2) for index in range(8)))
    (proc / "sys/kernel/osrelease").write_text("6.8.0-test\n")
    (proc / "driver/nvidia/version").write_text("NVRM version: NVIDIA UNIX x86_64 Kernel Module  535.288.01  Build details omitted\nGCC version: gcc 13.3.0\n")
    os_release = tmp_path / "os-release"
    os_release.write_text('PRETTY_NAME="Example Linux 1.0"\nNAME="Example Linux"\nVERSION="1.0"\n')
    return proc, os_release


def read(source_files):
    proc, os_release = source_files
    return collector.read_host_info(proc_root=proc, os_release_path=os_release)


def test_counts_physical_cores_by_socket_and_core_pair_without_counting_smt_twice(source_files):
    result = read(source_files)
    assert result == complete_info()


@pytest.mark.parametrize("text, sockets, cores", [
    ("processor : 0\nmodel name : Example CPU\n", None, None),
    ("processor : 0\nmodel name : Example CPU\nphysical id : 0\n", 1, None),
    (cpu_record(0) + "processor : 1\nmodel name : Example CPU\ncore id : 1\n", None, None),
    (cpu_record(0) + cpu_record(0, 1, 1), None, None),
    (cpu_record(0, "bad", 0), None, None),
    (cpu_record(0, 0, -1), 1, None),
])
def test_missing_or_inconsistent_cpu_topology_is_not_guessed(text, sockets, cores):
    result = collector.parse_cpu_info(text)
    assert result == {"cpu_model": "Example CPU", "cpu_socket_count": sockets, "cpu_physical_core_count": cores}


def test_cpu_model_is_independent_from_topology_and_does_not_invent_a_homogeneous_model():
    result = collector.parse_cpu_info(cpu_record(0, 0, 0, "") + cpu_record(1, 1, 0, ""))
    assert result == {"cpu_model": None, "cpu_socket_count": 2, "cpu_physical_core_count": 2}
    result = collector.parse_cpu_info(cpu_record(0, 0, 0, "First") + cpu_record(1, 1, 0, "Second"))
    assert result["cpu_model"] is None and result["cpu_physical_core_count"] == 2


@pytest.mark.parametrize("text, expected", [
    ('PRETTY_NAME="Ubuntu 24.04 LTS"\nNAME=Ubuntu\n', "Ubuntu 24.04 LTS"),
    ("PRETTY_NAME='Example Linux 1.0'\n", "Example Linux 1.0"),
    ('NAME="Example Linux"\nVERSION="1.0"\n', "Example Linux 1.0"),
    ('PRETTY_NAME="Example \\"Quoted\\" Linux"\n', 'Example "Quoted" Linux'),
    ('PRETTY_NAME="$(touch /tmp/not-executed)"\n', None),
    ('PRETTY_NAME="`uname`"\n', None),
    ('PRETTY_NAME=Ubuntu;id\n', None),
    ('PRETTY_NAME="one" "two"\n', None),
    ('PRETTY_NAME="one""two"\n', None),
    ('PRETTY_NAME="unterminated\n', None),
    ('PRETTY_NAME="one"\nPRETTY_NAME="two"\n', None),
    ('PRETTY_NAME="Bad\x00Name"\n', None),
    ('PRETTY_NAME="' + "x" * 161 + '"\n', None),
])
def test_os_release_uses_bounded_literal_parsing_without_shell_expansion(text, expected):
    assert collector.parse_os_name(text) == expected


def test_os_release_standard_symlink_is_supported_without_returning_its_target(source_files):
    proc, path = source_files
    target = path.with_name("os-release-target")
    path.rename(target); path.symlink_to(target)
    assert read((proc, path))["os_name"] == "Example Linux 1.0"


@pytest.mark.parametrize("text, expected", [
    ("NVRM version: NVIDIA UNIX x86_64 Kernel Module  535.288.01  date\nGCC version: gcc 13.3.0", "535.288.01"),
    ("NVRM version: NVIDIA UNIX Open Kernel Module for x86_64  580.82.09  Release Build", "580.82.09"),
    ("GCC version: gcc 13.3.0", None),
    ("NVRM version: NVIDIA UNIX Kernel Module  535.288.01.12345", None),
    ("NVRM version: NVIDIA UNIX Kernel Module  535.288.01.1.2", None),
    ("NVRM version: NVIDIA UNIX Kernel Module  bad", None),
])
def test_driver_parser_only_returns_the_kernel_driver_version(text, expected):
    assert collector.parse_nvidia_driver(text) == expected


@pytest.mark.parametrize("source, affected", [
    ("cpuinfo", {"cpu_model", "cpu_socket_count", "cpu_physical_core_count"}),
    ("os", {"os_name"}), ("sys/kernel/osrelease", {"kernel_release"}),
    ("driver/nvidia/version", {"nvidia_driver_version"}),
])
@pytest.mark.parametrize("failure", ["missing", "invalid_utf8", "oversize", "fifo"])
def test_each_unreadable_source_only_nulls_its_own_fields(source_files, source, affected, failure):
    proc, os_release = source_files
    path = os_release if source == "os" else proc / source
    path.unlink()
    if failure == "invalid_utf8": path.write_bytes(b"\xff")
    elif failure == "oversize": path.write_bytes(b"x" * (4 * 1024 * 1024 + 1))
    elif failure == "fifo": os.mkfifo(path)
    result = read(source_files)
    assert result == {key: None if key in affected else value for key, value in complete_info().items()}


def test_metadata_reader_never_invokes_subprocesses_or_returns_extra_source_content(source_files, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("host metadata must not execute a command")
    monkeypatch.setattr(collector.subprocess, "run", forbidden)
    proc, os_release = source_files
    with (proc / "cpuinfo").open("a") as stream: stream.write("private_field : not-returned\n")
    with os_release.open("a") as stream: stream.write('UNRELATED_SECRET="not-returned"\n')
    result = read(source_files)
    assert set(result) == set(collector.HOST_INFO_FIELDS)
    assert "not-returned" not in repr(result)


def test_hourly_metadata_cache_does_not_depend_on_wall_clock_or_requery_each_sample(source_files, tmp_path):
    proc, _ = source_files
    (proc / "stat").write_text("cpu 10 0 20 70 0 0 0 0\n")
    (proc / "meminfo").write_text("MemTotal: 1000 kB\nMemAvailable: 250 kB\n")
    (proc / "uptime").write_text("100 0\n")
    calls, mono, wall = [], [0.0], [1_800_000_000.0]
    def metadata():
        calls.append(mono[0]); return read(source_files)
    current = collector.Collector(tmp_path / "state", proc_root=proc, disks=(), gpu_query=lambda: ([], False),
                                  clock=lambda: wall[0], monotonic=lambda: mono[0], host_info_reader=metadata)
    first, _ = current.sample()
    assert first["host_info"] == complete_info() and len(calls) == 1
    (proc / "sys/kernel/osrelease").write_text("6.9.0-next\n")
    for elapsed in (5, 60, 3599):
        mono[0] = elapsed; wall[0] -= 30
        sampled, _ = current.sample()
        assert sampled["host_info"]["kernel_release"] == "6.8.0-test"
    assert len(calls) == 1
    mono[0] = 3600
    updated, _ = current.sample()
    assert updated["host_info"]["kernel_release"] == "6.9.0-next" and len(calls) == 2
    mono[0] = 3605; (proc / "driver/nvidia/version").unlink()
    assert current.sample()[0]["host_info"]["nvidia_driver_version"] == "535.288.01"
    mono[0] = 7200
    updated, _ = current.sample()
    assert updated["host_info"]["nvidia_driver_version"] is None
    assert updated["host_info"]["cpu_model"] == "Example CPU" and len(calls) == 3


@pytest.mark.parametrize("failure", ["read_error", "invalid_metadata"])
def test_metadata_failure_is_cached_and_does_not_fail_realtime_metrics(source_files, tmp_path, failure):
    proc, _ = source_files
    (proc / "stat").write_text("cpu 10 0 20 70 0 0 0 0\n")
    (proc / "meminfo").write_text("MemTotal: 1000 kB\nMemAvailable: 250 kB\n")
    (proc / "uptime").write_text("100 0\n")
    calls = []
    def metadata():
        calls.append(True)
        if failure == "read_error": raise OSError("unavailable metadata")
        return complete_info(cpu_socket_count=True)
    current = collector.Collector(tmp_path / "state", proc_root=proc, disks=(), gpu_query=lambda: ([], False),
                                  monotonic=lambda: 0, host_info_reader=metadata)
    for _ in range(2):
        snapshot, _history = current.sample()
        assert snapshot["host_info"] == dict.fromkeys(collector.HOST_INFO_FIELDS)
        assert snapshot["memory"]["total_bytes"] == 1000 * 1024
        assert snapshot["components"]["memory"]["status"] == "ok"
        assert snapshot["components"]["gpus"]["status"] == "ok"
    assert len(calls) == 1


def test_legacy_snapshot_accepts_missing_host_info_and_strict_metadata_is_projected():
    from server_monitoring.tests.test_api import snapshot
    original = snapshot()
    assert validate_snapshot(original).host_info is None
    original["host_info"] = complete_info()
    assert validate_snapshot(original).model_dump()["host_info"] == complete_info()


@pytest.mark.parametrize("change", [
    {"cpu_socket_count": True}, {"cpu_socket_count": 0}, {"cpu_physical_core_count": 1},
    {"cpu_model": "Bad\nName"}, {"os_name": " trailing "}, {"kernel_release": "kernel/secret"},
    {"nvidia_driver_version": "driver unknown"}, {"owner": "must-not-return"},
])
def test_host_info_contract_rejects_invalid_or_additional_fields(change):
    with pytest.raises(ValueError): HostInfo.model_validate(complete_info(**change))
