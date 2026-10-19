from dataclasses import replace
from pathlib import Path
from scripts import acceptance_gpu_status as s
from scripts.dev_gpu_cohort import DevGpuCohort


def test_projection_reuses_native_states_without_recovery_implementation():
    stopped=s.project({'status':'stopped'},source_sha='a'*40,source_tree='b'*40)
    ready=s.project({'status':'ready'},source_sha='a'*40,source_tree='b'*40)
    assert stopped['phase']=='stopped' and stopped['can_recover'] is True
    assert ready['phase']=='ready' and ready['can_recover'] is False
    assert stopped['source_sha']=='a'*40 and ready['source_tree']=='b'*40


def test_readonly_mount_is_selected_only_for_exact_new_scope():
    obj=object.__new__(DevGpuCohort)
    object.__setattr__(obj,'cohort_id','r2-a-20261019')
    object.__setattr__(obj,'repo_root',s.FIXED_ROOT)
    original=DevGpuCohort.runtime_mounts()
    selected=obj.selected_runtime_mounts()
    assert selected==(*original,('acceptance-status','/app/.runtime/acceptance-status',True))
    object.__setattr__(obj,'cohort_id','another-cohort')
    assert obj.selected_runtime_mounts()==original
    object.__setattr__(obj,'cohort_id','r2-a-20261019');object.__setattr__(obj,'repo_root',Path('/other'))
    assert obj.selected_runtime_mounts()==original
