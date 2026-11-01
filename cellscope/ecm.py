"""1RC equivalent-circuit model + lumped thermal model.

SIM_CORE is the single source of truth: it is sent verbatim to the Otari
code-execution sandbox (inside SANDBOX_TEMPLATE) AND exec'd locally as the
reference implementation used by tests and by the verifier.

Sign convention: charge current I > 0. Terminal voltage while charging is
V = OCV(SOC) + I*R0 + V1 (equivalently V = OCV - I*R0 - V1 with the spec's
discharge-positive I). dV1/dt = (I*R1 - V1)/(R1*C1);
m*cp*dT/dt = I^2*(R0+R1) - h*A*(T - Tamb).
"""
from __future__ import annotations

import hashlib
import json

SIM_CORE = r'''
import json, math, hashlib
SOC_PTS = [0.0, 0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
OCV_PTS = [3.00, 3.30, 3.45, 3.55, 3.61, 3.66, 3.72, 3.80, 3.89, 3.98, 4.08, 4.20]

def ocv(soc):
    soc = min(max(soc, 0.0), 1.0)
    for i in range(1, len(SOC_PTS)):
        if soc <= SOC_PTS[i]:
            f = (soc - SOC_PTS[i-1]) / (SOC_PTS[i] - SOC_PTS[i-1])
            return OCV_PTS[i-1] + f * (OCV_PTS[i] - OCV_PTS[i-1])
    return OCV_PTS[-1]

def simulate(p):
    cap = p["capacity_Ah"]; R0 = p["R0_ohm"]; R1 = p["R1_ohm"]; C1 = p["C1_F"]
    m = p["mass_kg"]; cp = p["cp_J_per_kgK"]; hA = p["h_W_per_m2K"] * p["area_m2"]
    Tamb = p["T_amb_C"]; vmax = p.get("v_max", 4.2); dt = p.get("dt_s", 1.0)
    I_cc = p["c_rate"] * cap; I_cut = p.get("cutoff_c", 0.05) * cap
    soc = p.get("soc0", 0.1); T = Tamb; V1 = 0.0; t = 0.0; I = I_cc; mode = "CC"
    t80 = None; peak = T; e_loss = 0.0; series = []
    while t < p.get("t_max_s", 3 * 3600):
        if mode == "CC" and ocv(soc) + I_cc * R0 + V1 >= vmax:
            mode = "CV"
        I = I_cc if mode == "CC" else max(0.0, min(I_cc, (vmax - ocv(soc) - V1) / R0))
        if mode == "CV" and I < I_cut:
            break
        V = ocv(soc) + I * R0 + V1
        q = I * I * (R0 + R1)
        V1 += dt * (I * R1 - V1) / (R1 * C1)
        T += dt * (q - hA * (T - Tamb)) / (m * cp)
        soc += I * dt / (cap * 3600.0)
        e_loss += q * dt / 3600.0
        t += dt
        peak = max(peak, T)
        if t80 is None and soc >= 0.8:
            t80 = t
        if int(t) % 60 == 0:
            series.append([round(t / 60, 2), round(V, 4), round(I, 3), round(T, 3), round(soc, 4)])
    step = max(1, len(series) // 40)
    pts = series[::step]
    return {"t_to_80pct_min": None if t80 is None else round(t80 / 60, 2),
            "peak_temp_C": round(peak, 2), "delta_T_C": round(peak - Tamb, 2),
            "energy_loss_Wh": round(e_loss, 3), "t_end_min": round(t / 60, 2),
            "series": {"t_min": [s[0] for s in pts], "V": [s[1] for s in pts],
                       "I_A": [s[2] for s in pts], "T_C": [s[3] for s in pts],
                       "soc": [s[4] for s in pts]}}

def run_all(cells):
    out = {}
    for name, p in cells.items():
        out[name] = simulate(p)
    return out
'''

SANDBOX_TEMPLATE = SIM_CORE + r'''
CELLS = json.loads(__CELLS_JSON__)
res = run_all(CELLS)
blob = json.dumps(res, sort_keys=True, separators=(",", ":"))
try:
    import matplotlib, io
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(4, 2.5), dpi=60)
    for n, r in res.items():
        ax.plot(r["series"]["t_min"], r["series"]["T_C"], label=n)
    buf = io.BytesIO(); fig.savefig(buf, format="png"); png = len(buf.getvalue())
    mpl = "matplotlib " + matplotlib.__version__ + " png_bytes=" + str(png)
except Exception as e:
    mpl = "no-matplotlib: " + type(e).__name__
print("SANDBOX_INFO=" + mpl)
print("CELLSCOPE_RESULT=" + blob)
print("CELLSCOPE_SHA=" + hashlib.sha256(blob.encode()).hexdigest()[:16])
'''

_ns: dict = {}
exec(SIM_CORE, _ns)
simulate = _ns["simulate"]
run_all = _ns["run_all"]
ocv = _ns["ocv"]

# 21700 can: d=21 mm, h=70 mm -> lateral + end area
AREA_21700 = 3.14159 * 0.021 * 0.070 + 2 * 3.14159 * 0.0105 ** 2
AREA_18650 = 3.14159 * 0.018 * 0.065 + 2 * 3.14159 * 0.009 ** 2


def params_from_spec(spec: dict, c_rate: float, t_amb: float, h: float = 15.0) -> dict:
    """Build simulation params from a (possibly partial) spec row; records defaults used."""
    cap = spec.get("nominal_capacity_Ah") or 4.5
    ir = spec.get("dc_ir_mOhm") or 20.0
    mass = (spec.get("mass_g") or 69.0) / 1000.0
    area = AREA_18650 if mass < 0.055 else AREA_21700
    R0 = ir / 1000.0 * 0.7          # assume 70% ohmic, 30% polarisation of DCIR
    R1 = ir / 1000.0 * 0.3
    return {"capacity_Ah": float(cap), "R0_ohm": round(R0, 5), "R1_ohm": round(R1, 5),
            "C1_F": round(30.0 / R1, 1), "mass_kg": mass, "cp_J_per_kgK": 1000.0,
            "h_W_per_m2K": h, "area_m2": round(area, 6), "T_amb_C": float(t_amb),
            "c_rate": float(c_rate), "v_max": 4.2, "soc0": 0.1, "dt_s": 1.0, "cutoff_c": 0.05,
            "defaults_used": [k for k, v in (("capacity", spec.get("nominal_capacity_Ah")),
                                             ("dc_ir", spec.get("dc_ir_mOhm")),
                                             ("mass", spec.get("mass_g"))) if not v]}


def sandbox_code(cells: dict) -> str:
    clean = {n: {k: v for k, v in p.items() if k != "defaults_used"} for n, p in cells.items()}
    return SANDBOX_TEMPLATE.replace("__CELLS_JSON__", repr(json.dumps(clean)))


def result_sha(result: dict) -> str:
    blob = json.dumps(result, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def parse_sandbox_output(text: str) -> tuple[dict | None, str | None, str]:
    """Extract (result, sha, sandbox_info) from the echoed stdout; never trusts prose."""
    result = sha = None
    info = ""
    for line in text.replace("```", "\n").splitlines():
        line = line.strip().strip("`")
        if line.startswith("CELLSCOPE_RESULT="):
            try:
                result = json.loads(line[len("CELLSCOPE_RESULT="):])
            except ValueError:
                result = None
        elif line.startswith("CELLSCOPE_SHA="):
            sha = line[len("CELLSCOPE_SHA="):].strip()
        elif line.startswith("SANDBOX_INFO="):
            info = line[len("SANDBOX_INFO="):].strip()
    return result, sha, info


def verify(result: dict | None, sha: str | None, reference: dict, tol: float = 0.02) -> tuple[bool, str]:
    if result is None:
        return False, "no CELLSCOPE_RESULT line in sandbox output"
    if sha != result_sha(result):
        return False, "sha mismatch: echoed JSON was altered"
    for cell, ref in reference.items():
        got = result.get(cell) or {}
        for k in ("peak_temp_C", "energy_loss_Wh", "t_to_80pct_min"):
            a, b = got.get(k), ref.get(k)
            if (a is None) != (b is None) or (a is not None and abs(a - b) > tol * max(1.0, abs(b))):
                return False, f"{cell}.{k}: sandbox {a} vs local reference {b}"
    return True, "sha ok; matches local reference"
