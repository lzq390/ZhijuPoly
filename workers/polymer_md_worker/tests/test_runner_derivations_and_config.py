"""Behavioral regressions for derived values and generated input; no MD/QM runs."""
from __future__ import annotations
import json
from pathlib import Path
import re
import unittest

from workers.polymer_md_worker.app.scientific_runner import (
    derive_optical_properties as optical, derive_thermal_diffusivity as diffusivity,
    validate_rnemd_input as validate, normalize_rnemd_input as normalize,
)

WINDOW = {"start_ps": 2000., "stop_ps": 5000.}
UNITS = {"density": "g/cm3", "Cp": "J/(kg K)", "static_dielectric_const": "1", "thermal_conductivity": "W/(m K)"}


def prop(key, value, quality="passed"):
    return {"key": key, "value": value, "unit": UNITS[key], "quality_status": quality,
            "method": "controlled_test_input", "source_stage": "tc" if key == "thermal_conductivity" else "eq3",
            "analysis_range": {"start_ps": 800., "stop_ps": 1000.} if key == "thermal_conductivity" else WINDOW,
            "uncertainty": None}


def basics():
    return {"density": prop("density", 1.), "Cp": prop("Cp", 2000.),
            "static_dielectric_const": prop("static_dielectric_const", 2.)}


class DerivedPropertyTests(unittest.TestCase):
    def test_missing_optional_inputs_always_publish_two_null_records(self):
        records = optical({}, {}, "eq3", WINDOW)
        self.assertEqual([r["key"] for r in records], ["refractive_index", "electronic_corrected_dielectric"])
        self.assertTrue(all(r["value"] is None and r["quality_status"] == "failed" for r in records))
        self.assertIn("density_missing", records[0]["reason"])
        self.assertIn("static_dielectric_const_missing", records[1]["reason"])
        json.dumps(records, allow_nan=False)

    def test_bad_density_never_drives_optional_estimates_or_nonfinite_json(self):
        for value in (0., -1., float("nan"), float("inf"), True, None, 10**1000):
            with self.subTest(value=value):
                inputs = basics(); inputs["density"]["value"] = value
                records = optical(inputs, {"qm_polarizability": 1., "mol_weight": 30.}, "eq3", WINDOW)
                self.assertTrue(all(r["value"] is None for r in records))
                self.assertIn("density_non_finite_or_non_positive", records[0]["reason"])
                json.dumps(records, allow_nan=False)

    def test_warning_density_and_invalid_qm_do_not_drive_derived_values(self):
        inputs = basics(); inputs["density"]["quality_status"] = "warning"
        self.assertIn("density_quality_not_passed", optical(inputs, {"qm_polarizability": 1., "mol_weight": 30.}, "eq3", WINDOW)[0]["reason"])
        for qm in ({}, {"qm_polarizability": float("nan"), "mol_weight": 30.},
                   {"qm_polarizability": 1., "mol_weight": 0.}, None):
            with self.subTest(qm=qm):
                records = optical(basics(), qm, "eq3", WINDOW)
                self.assertTrue(all(r["value"] is None for r in records)); json.dumps(records, allow_nan=False)

    def test_unphysical_lorentz_lorenz_phi_is_a_null_diagnostic(self):
        records = optical(basics(), {"qm_polarizability": 1e300, "mol_weight": 30.}, "eq3", WINDOW)
        self.assertIsNone(records[0]["value"])
        self.assertIn("lorentz_lorenz_phi_outside", records[0]["reason"])
        json.dumps(records, allow_nan=False)

    def test_refractive_index_does_not_require_a_static_dielectric_measurement(self):
        records = optical({"density": prop("density", 1.)}, {"qm_polarizability": 1., "mol_weight": 30.}, "eq3", WINDOW)
        self.assertGreater(records[0]["value"], 1.)
        self.assertIsNone(records[1]["value"])
        self.assertIn("static_dielectric_const_missing", records[1]["reason"])

    def test_electronic_correction_requires_qualified_finite_positive_dielectric(self):
        for value, quality in ((2., "warning"), (0., "passed"), (float("inf"), "passed")):
            with self.subTest(value=value, quality=quality):
                inputs = basics(); inputs["static_dielectric_const"] = prop("static_dielectric_const", value, quality)
                ri, corrected = optical(inputs, {"qm_polarizability": 1., "mol_weight": 30.}, "eq3", WINDOW)
                self.assertIsNotNone(ri["value"]); self.assertIsNone(corrected["value"])
                self.assertIn("static_dielectric_const_", corrected["reason"])
                json.dumps([ri, corrected], allow_nan=False)

    def test_optional_valid_estimates_keep_dependency_methods_and_no_invented_uncertainty(self):
        ri, corrected = optical(basics(), {"qm_polarizability": 1., "mol_weight": 30.}, "eq3", WINDOW)
        self.assertGreater(ri["value"], 1.); self.assertGreater(corrected["value"], 2.)
        self.assertTrue(all(r["quality_status"] == "warning" and r["uncertainty"] is None for r in (ri, corrected)))
        self.assertIn("uncertainty_not_propagated", corrected["reason"])
        self.assertEqual(ri["conditions"]["density"]["method"], "controlled_test_input")
        self.assertEqual(corrected["conditions"]["static_dielectric_const"]["source_stage"], "eq3")
        self.assertEqual(ri["conditions"]["qm"]["polarizability_method"], "wb97m-d3bj/6-311+G(2d,p)")

    def test_warning_kappa_never_produces_thermal_diffusivity(self):
        for quality in ("warning", "not_converged", "failed", "not_evaluated"):
            with self.subTest(quality=quality):
                result = diffusivity({"thermal_conductivity": prop("thermal_conductivity", .2, quality)}, basics(), "tc", {"start_ps": 800., "stop_ps": 1000.})
                self.assertIsNone(result["value"])
                self.assertIn("thermal_conductivity_quality_not_passed", result["reason"])

    def test_bad_or_missing_thermal_dependencies_are_explicit_nulls(self):
        for key in ("thermal_conductivity", "density", "Cp"):
            for value in (0., -1., float("nan"), float("inf"), None, True):
                with self.subTest(key=key, value=value):
                    thermal = {"thermal_conductivity": prop("thermal_conductivity", .2)}; base = basics()
                    (thermal if key == "thermal_conductivity" else base)[key]["value"] = value
                    result = diffusivity(thermal, base, "tc", {"start_ps": 800., "stop_ps": 1000.})
                    self.assertIsNone(result["value"]); self.assertIn(key + "_non_finite_or_non_positive", result["reason"])
                    json.dumps(result, allow_nan=False)
        self.assertIsNone(diffusivity({}, {}, "tc", WINDOW)["value"])

    def test_valid_diffusivity_converts_density_and_declares_missing_error_propagation(self):
        result = diffusivity({"thermal_conductivity": prop("thermal_conductivity", .2)}, basics(), "tc", {"start_ps": 800., "stop_ps": 1000.})
        self.assertAlmostEqual(result["value"], 1e-7)
        self.assertEqual(result["quality_status"], "warning"); self.assertIsNone(result["uncertainty"])
        self.assertIn("uncertainty_not_propagated", result["reason"])
        self.assertEqual(result["conditions"]["dependencies"]["Cp"]["analysis_range"], WINDOW)

    def test_dependency_unit_mismatch_is_rejected(self):
        base = basics(); base["density"]["unit"] = "kg/m3"
        result = diffusivity({"thermal_conductivity": prop("thermal_conductivity", .2)}, base, "tc", WINDOW)
        self.assertIsNone(result["value"]); self.assertIn("density_unit_mismatch", result["reason"])


class GeneratedProtocolTests(unittest.TestCase):
    def setUp(self):
        self.text = Path(__file__).with_name("rnemd_total_input.in").read_text()
        self.raw_text = Path(__file__).with_name("rnemd_upstream_input.in").read_text()
        self.request = {"tc_axis": "x", "tc_rep": 3, "tc_ns": 1., "temp": 300., "seed": 1729, "sample_interval_ps": 7.}

    def test_normalized_frozen_template_records_actual_sampling_and_distinct_velocity_seed(self):
        result = validate(self.text, self.request)
        self.assertRegex(self.text, r"dump\s+1 all custom")  # Present text is guarded off by dumpf=None.
        self.assertFalse(result["bulk_ascii_dump"])
        self.assertEqual(result["slab_count"], 20); self.assertEqual(result["exchange_steps"], 1000)
        self.assertEqual(result["xtc_steps"], 1000); self.assertEqual(result["xtc_interval_ps"], .2)
        self.assertEqual(result["thermo_steps"], 1000); self.assertEqual(result["thermo_interval_ps"], .2)
        self.assertEqual(result["profile_nrepeat"], 1000); self.assertEqual(result["preheat_ps"], 2.)
        self.assertEqual(result["velocity_gaussian_seed"], 43210); self.assertNotEqual(result["velocity_gaussian_seed"], self.request["seed"])
        self.assertIn("EQ_only", result["sampling_policy"])
        self.assertEqual(result["compatibility_patch"], "fixed_output_boolean_guards")

    def test_normalizer_changes_only_two_frozen_output_guards(self):
        expected = self.raw_text.replace('if "${dumpf} != None" then', 'if "0" then').replace('if "${xtcf} != None" then', 'if "1" then')
        self.assertEqual(normalize(self.raw_text), expected)
        self.assertEqual(normalize(self.raw_text), self.text)
        extra = '\nif "0" then "print unrelated_guard"\n'
        self.assertEqual(normalize(self.raw_text + extra), expected + extra)

    def test_normalizer_is_idempotent_for_the_fixed_numeric_guards(self):
        self.assertEqual(normalize(normalize(self.raw_text)), normalize(self.raw_text))
        self.assertEqual(normalize(self.text), self.text)

    def test_validator_rejects_original_filename_comparisons(self):
        with self.assertRaisesRegex(RuntimeError, "not normalized"):
            validate(self.raw_text, self.request)

    def test_dotted_or_hyphenated_filenames_are_never_boolean_operands(self):
        raw = self.raw_text.replace('variable xtcf string fixture.xtc', 'variable xtcf string nemd_TC-MP_x.xtc')
        normalized = normalize(raw)
        self.assertIn('variable xtcf string nemd_TC-MP_x.xtc', normalized)
        self.assertIn('if "1" then', normalized)
        self.assertNotIn('${xtcf} != None', normalized)
        self.assertEqual(validate(normalized, self.request)["compatibility_patch"], "fixed_output_boolean_guards")

    def test_normalizer_rejects_other_output_policies_and_ambiguous_guards(self):
        variants = [self.raw_text.replace('if "${dumpf} != None" then', 'if "1" then'),
                    self.raw_text.replace('if "${xtcf} != None" then', 'if "0" then'),
                    self.raw_text.replace('if "${xtcf} != None" then', 'if "2" then'),
                    self.raw_text.replace('if "${xtcf} != None" then &', ''),
                    self.raw_text + '\nif "${xtcf} != None" then &\n  "dump 2 all xtc 1000 ${xtcf}"\n']
        for text in variants:
            with self.subTest(changed=text != self.raw_text):
                self.assertNotEqual(text, self.raw_text)
                with self.assertRaises(RuntimeError): normalize(text)

    def test_request_axes_and_replication_are_parsed_from_generated_values(self):
        for axis in ("x", "y", "z"):
            for replication in (1, 3, 5):
                with self.subTest(axis=axis, replication=replication):
                    text = self.text.replace("variable axis string x", "variable axis string " + axis).replace("variable rep equal 3", "variable rep equal " + str(replication))
                    actual = validate(text, {**self.request, "tc_axis": axis, "tc_rep": replication})
                    self.assertEqual(actual["axis"], axis); self.assertEqual(actual["rep"], replication)

    def test_each_wrong_generated_control_is_rejected(self):
        bad = {"axis": "string y", "rep": "equal 4", "repo": "equal 2", "TimeSt": "equal .1",
               "NStep": "equal 5000001", "Ttemp": "equal 310", "slab": "equal 22", "exchg": "equal 500", "Nevery": "equal 2"}
        for name, replacement in bad.items():
            with self.subTest(variable=name):
                text = re.sub(r"(?m)^variable " + name + r" (?:string|equal) .*?$", "variable " + name + " " + replacement, self.text)
                with self.assertRaises(RuntimeError): validate(text, self.request)

    def test_missing_duplicate_expression_or_nonfinite_variables_are_rejected_without_eval(self):
        variants = [self.text + "\nvariable axis string x\n",
                    self.text.replace("variable slab equal 20\n", ""),
                    self.text.replace("variable TimeSt equal 0.200000", "variable TimeSt equal 0.1+0.1"),
                    self.text.replace("variable TimeSt equal 0.200000", "variable TimeSt equal NaN")]
        for text in variants:
            with self.subTest(text=text[:60]):
                with self.assertRaises(RuntimeError): validate(text, self.request)

    def test_native_warmup_sampling_guards_and_axis_mapping_are_frozen(self):
        variants = [self.text.replace("run             10000", "run             5000"),
                    self.text.replace("xtc 1000", "xtc 500"),
                    self.text.replace('if "0" then &', 'if "1" then &'),
                    self.text.replace("variable dumpf string None", "variable dumpf string bulk.dump"),
                    self.text.replace("variable xtcf string fixture.xtc", "variable xtcf string None"),
                    self.text.replace("${rep} ${repo} ${repo}", "${repo} ${rep} ${repo}", 1),
                    self.text.replace("thermo          ${exchg}", "thermo          999"),
                    self.text + "\ncompute heatflux all heat/flux KE PE stress\n"]
        for text in variants:
            with self.subTest(changed=text != self.text):
                self.assertNotEqual(text, self.text)
                with self.assertRaises(RuntimeError): validate(text, self.request)

    def test_actual_velocity_seed_must_remain_valid_frozen_gaussian_seed(self):
        for value in ("0", "999999", "43210.5"):
            with self.subTest(value=value):
                with self.assertRaises(RuntimeError): validate(self.text.replace("variable seed equal 43210", "variable seed equal " + value), self.request)



if __name__ == "__main__":
    unittest.main()
