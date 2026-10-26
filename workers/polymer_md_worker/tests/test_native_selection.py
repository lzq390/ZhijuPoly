"""Native call bindings use synthetic molecules/engines, never QM or MD runs."""
from copy import deepcopy
import sys
from types import ModuleType
from uuid import uuid4
from typing import get_args

import numpy as np
import pytest
from rdkit import Chem

from polymer_md.contracts import NativeConfiguration, ScientificRequest, SubmitRequest, request_sha256
from workers.polymer_md_worker.app import native_execution as native
from workers.polymer_md_worker.app import scientific_runner as runner


@pytest.fixture
def anyio_backend():
    return "asyncio"


def scientific(properties, **overrides):
    config = NativeConfiguration(properties=properties, **overrides.pop("methods", {}))
    return ScientificRequest(smiles="*CC*", seed=17, n_atom=100, n_chain=1,
        calculation="basic_tc" if "thermal_conductivity" in properties else "basic",
        native_configuration=config, **overrides).model_dump(mode="json")


def test_scientific_prefix_whitelist_matches_http_contract_without_importing_pydantic():
    from polymer_md.contracts import NativeProperty, NativeMethod, NativeBasis, NativeCharge
    assert native.NATIVE_PROPERTIES == frozenset(get_args(NativeProperty))
    assert native.METHODS == frozenset(get_args(NativeMethod))
    assert native.BASES == frozenset(get_args(NativeBasis))
    assert native.CHARGES == frozenset(get_args(NativeCharge))
    request = scientific(["density"])
    assert native.configuration(request) == request["native_configuration"]
    with pytest.raises(ValueError): native.configuration({"native_configuration": {**request["native_configuration"], "opt_method": "unknown"}})


@pytest.mark.parametrize("name,module,symbol", [(name, *entry) for name, entry in native.FORCEFIELDS.items()])
def test_forcefield_names_construct_the_corresponding_native_class(monkeypatch, name, module, symbol):
    calls = []
    def load(path):
        assert path == "radonpy.ff." + module
        return type("Module", (), {symbol: lambda: calls.append(symbol) or symbol})
    monkeypatch.setattr(native.importlib, "import_module", load)
    assert native.make_forcefield({"forcefield": name}) == symbol
    assert calls == [symbol]
    with pytest.raises(ValueError): native.make_forcefield({"forcefield": "unknown"})


@pytest.fixture
def synthetic_radonpy(monkeypatch):
    modules = {}
    for name in ("radonpy", "radonpy.core", "radonpy.core.utils", "radonpy.core.calc", "radonpy.sim", "radonpy.sim.qm", "radonpy.sim.qm_wrapper"):
        modules[name] = ModuleType(name)
        monkeypatch.setitem(sys.modules, name, modules[name])
    mol = Chem.MolFromSmiles("CC")
    conf = Chem.Conformer(2);conf.SetAtomPosition(0,(0,0,0));conf.SetAtomPosition(1,(1.5,0,0));mol.AddConformer(conf)
    calls = []
    utils, qm, calc = (modules[key] for key in ("radonpy.core.utils", "radonpy.sim.qm", "radonpy.core.calc"))
    utils.mol_from_smiles = lambda value: Chem.Mol(mol)
    utils.deepcopy_mol = Chem.Mol
    calc.molecular_weight = lambda value: 30.
    qm.conformation_search = lambda value, **kwargs: (calls.append(("search", kwargs)) or value, np.array([-1.]))
    def assign(value, **kwargs):
        calls.append(("charges", kwargs))
        for atom in value.GetAtoms(): atom.SetDoubleProp("AtomicCharge", 0.)
        return True
    qm.assign_charges = assign
    class Wrapper:
        error_flag = False
        homo, lumo, dipole = -6., -1., [1., 0., 0.]
        def __init__(self, value, **kwargs):
            self.mol = Chem.Mol(value);self.kwargs=kwargs;calls.append(("wrapper", kwargs))
        def optimize(self, **kwargs):
            calls.append(("optimize", kwargs));return -10., self.mol.GetConformer().GetPositions()
        def energy(self): calls.append(("energy",self.kwargs));return -11.
        def polar(self, **kwargs): calls.append(("polar",self.kwargs));return 2.,np.eye(3)*2
    modules["radonpy.sim.qm_wrapper"].QMw = Wrapper
    monkeypatch.setattr(runner,"make_forcefield",lambda request: request["forcefield"])
    monkeypatch.setattr(runner,"store_molecule",lambda state,name,value:state["molecules"].update({name:"synthetic"}))
    monkeypatch.setenv("LAMMPS_EXEC","synthetic-test-only")
    return calls


@pytest.mark.parametrize("properties,needs_sp,needs_polar", [
    (["density"],False,False),(["qm_homo"],True,False),(["qm_polarizability"],False,True),
    (["refractive_index"],False,True),(list(native.QM_UNITS),True,True)])
def test_selected_native_qm_steps_receive_actual_methods_and_only_needed_work(synthetic_radonpy,tmp_path,properties,needs_sp,needs_polar):
    methods={"opt_method":"HF","opt_basis":"6-31G(d)","charge":"Mulliken",
             "charge_method":"wb97m-d3bj","charge_basis":"6-31G(d,p)",
             "sp_method":"HF","sp_basis":"6-311G(d,p)","polar_method":"HF","polar_basis":"6-311+G(2d,p)"}
    request=scientific(properties,forcefield="Dreiding",methods=methods)
    state={"molecules":{}}
    result=runner.qm_monomer(request,state,tmp_path)
    calls=synthetic_radonpy
    assert next(kwargs for name,kwargs in calls if name=="search")["ff"]=="Dreiding"
    opt=next(kwargs for name,kwargs in calls if name=="wrapper" and kwargs["name"].startswith("monomer_dft"))
    assert (opt["method"],opt["basis"])==("HF","6-31G(d)")
    charges=next(kwargs for name,kwargs in calls if name=="charges")
    assert charges["charge"]=="Mulliken" and charges["charge_method"]=="wb97m-d3bj" and charges["charge_basis"]=="6-31G(d,p)"
    assert any(name=="energy" for name,_ in calls)==needs_sp
    assert any(name=="polar" for name,_ in calls)==needs_polar
    assert {item["key"] for item in result["properties"]}==set(properties)&native.QM_UNITS.keys()
    for name,kwargs in calls:
        if name in {"energy","polar"}: assert kwargs["method"]=="HF"
    assert result["effective_native_configuration"]["properties"]==sorted(properties)


def test_changed_terminal_charge_or_optimization_never_uses_old_resp_cache(synthetic_radonpy,monkeypatch,tmp_path):
    monkeypatch.setattr(runner,"probe_runtime",lambda:pytest.fail("old terminal cache must not be consulted"))
    request=scientific(["qm_homo"],methods={"opt_method":"HF","charge":"zero"})
    result=runner.qm_terminal(request,{"molecules":{}},tmp_path)
    assert next(kwargs for name,kwargs in synthetic_radonpy if name=="charges")["charge"]=="zero"
    assert all(record["source"]=="task_private_verified_QM" and "zero" in record["method"] for record in result["terminal_assets"].values())


def test_only_selected_records_are_published_and_qm_provenance_is_not_relabelled():
    request=scientific(["density","qm_homo"])
    original={"properties":[{"key":"density","value":1},{"key":"temperature","value":300}],"equilibrated":True}
    before=deepcopy(original)
    result=native.retain_requested(request,original,{"qm_homo":-7.})
    assert original==before
    assert [p["key"] for p in result["properties"]]==["density","qm_homo"]
    assert result["properties"][1]["source_stage"]=="monomer_qm"
    assert result["properties"][1]["conditions"]["scope"]=="monomer"
    assert native.selected_properties({}) is None


@pytest.mark.anyio
async def test_molecular_only_job_finishes_without_build_or_equilibrium(tmp_path):
    from test_manager import supervisor,until
    manager,calls=supervisor(tmp_path)
    values=ScientificRequest.model_validate(scientific(["qm_homo"]))
    request=SubmitRequest(job_id=str(uuid4()),owner_user_id=str(uuid4()),attempt_token="a"*64,
        request_sha256=request_sha256(values),enqueue_sequence=1,scientific_request=values)
    await manager.start()
    try:
        await manager.submit(request)
        await until(lambda:manager.get(request.job_id)["status"]=="completed")
        assert calls==["terminal_qm","monomer_qm"]
        assert manager.get(request.job_id)["cleanup_confirmed"]
        assert manager.get(request.job_id)["quality_status"]=="passed"
    finally:await manager.stop()
