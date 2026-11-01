import contextlib
import io

from cellscope import ecm

SPEC = {"nominal_capacity_Ah": 5.0, "dc_ir_mOhm": 20, "mass_g": 69}


def test_ocv_monotonic_and_bounded():
    vals = [ecm.ocv(s / 100) for s in range(0, 101)]
    assert vals == sorted(vals) and vals[0] == 3.0 and vals[-1] == 4.2
    assert ecm.ocv(-1) == 3.0 and ecm.ocv(2) == 4.2


def test_simulation_physics():
    r3 = ecm.simulate({k: v for k, v in ecm.params_from_spec(SPEC, 3, 25).items() if k != "defaults_used"})
    r1 = ecm.simulate({k: v for k, v in ecm.params_from_spec(SPEC, 1, 25).items() if k != "defaults_used"})
    assert r3["delta_T_C"] > r1["delta_T_C"] > 0          # more current, more heat
    assert r3["t_to_80pct_min"] < r1["t_to_80pct_min"]    # faster charge
    assert 5 < r3["t_to_80pct_min"] < 20                  # 10->80% at 3C is ~14 min
    assert r3["energy_loss_Wh"] > r1["energy_loss_Wh"]
    s = r3["series"]
    assert len(s["t_min"]) == len(s["T_C"]) >= 10 and max(s["V"]) <= 4.2 + 1e-6


def test_lower_resistance_runs_cooler():
    hot = ecm.simulate({k: v for k, v in ecm.params_from_spec({**SPEC, "dc_ir_mOhm": 30}, 3, 25).items() if k != "defaults_used"})
    cool = ecm.simulate({k: v for k, v in ecm.params_from_spec({**SPEC, "dc_ir_mOhm": 10}, 3, 25).items() if k != "defaults_used"})
    assert cool["peak_temp_C"] < hot["peak_temp_C"]


def test_defaults_are_recorded():
    p = ecm.params_from_spec({"nominal_capacity_Ah": None}, 3, 25)
    assert set(p["defaults_used"]) == {"capacity", "dc_ir", "mass"}


def _run_sandbox(cells):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        exec(ecm.sandbox_code(cells), {})
    return buf.getvalue()


def test_sandbox_template_roundtrip_and_verify():
    cells = {"A": ecm.params_from_spec(SPEC, 3, 25)}
    ref = ecm.run_all({"A": {k: v for k, v in cells["A"].items() if k != "defaults_used"}})
    out = _run_sandbox(cells)
    res, sha, info = ecm.parse_sandbox_output("Here is the output:\n```\n" + out + "```")
    assert ecm.verify(res, sha, ref) == (True, "sha ok; matches local reference")
    assert info


def test_verify_rejects_tampered_or_missing_output():
    cells = {"A": ecm.params_from_spec(SPEC, 3, 25)}
    ref = ecm.run_all({"A": {k: v for k, v in cells["A"].items() if k != "defaults_used"}})
    out = _run_sandbox(cells).replace('"peak_temp_C":', '"peak_temp_C":1')
    res, sha, _ = ecm.parse_sandbox_output(out)
    assert ecm.verify(res, sha, ref)[0] is False
    assert ecm.verify(None, None, ref)[0] is False
    forged = dict(ref)
    forged["A"] = {**ref["A"], "peak_temp_C": 30.0}
    assert ecm.verify(forged, ecm.result_sha(forged), ref)[0] is False   # valid sha but wrong physics
