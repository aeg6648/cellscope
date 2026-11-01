# CellScope: an open-weight battery-cell research copilot

Ask a battery-engineering question ("Compare Samsung INR21700-50E vs Molicel INR21700-P45B for a 3C fast-charge pack and
estimate temperature rise") and CellScope returns:

- a **sourced spec table** (datasheet URLs found with Otari web search/fetch),
- **simulation numbers + a temperature plot** from a 1RC equivalent-circuit + lumped-thermal model that runs in the
  **Otari code-execution sandbox**, checked against a local reference before it's shown,
- **modeling notes from the PyBaMM repo** pulled through the **DeepWiki MCP server**,
- a **plain-language explanation** for a junior engineer,
- a **two-layer safety screen** (Otari Lakera guardrail on input + Llama Guard 4 / battery-abuse rules in the app),
- a **per-request cost and per-end-user budget readout** (`usage.cost_usd`; Otari per-user budgets give the hard cap).

Every model is open-weight and every call goes through one gateway: `https://otari.ai/api/v1`.

## Architecture

```
 browser (static/index.html)            CLI: python -m cellscope "question"
        |  POST /ask  (NDJSON stage events)        |
        v                                          v
 cellscope/server.py ──────────────> cellscope/pipeline.py ──> cellscope/otari_client.py ──> Otari gateway
                                                                 (role -> candidate models,      |  guardrail cellscope-safety (Lakera)
                                                                  fallback, cost ledger,         |  budgets: per-user $0.30/day, org ceiling
                                                                  Otari-Tags, metadata, user)    |  routing alias cellscope-researcher
   1 critic (in)  meta/llama-guard-4-12b  ── prompt screen + battery-abuse regex rules
   2 router       alibaba/qwen3.5-9b      ── JSON {intent, cells, c_rate, t_amb}
   3 researcher   cellscope-researcher    ── tools: otari_web_search + otari_web_fetch  -> spec table + sources
                  (Qwen3.6-35B-A3B -> Llama-3.3-70B -> DeepSeek-V3.2)
   4 mcp docs     cellscope-researcher    ── DeepWiki MCP (pybamm-team/PyBaMM) -> modeling notes + deepwiki URL
   5 simulator    qwen3-coder-30b-a3b     ── tool: otari_code_execution runs ecm.SANDBOX_TEMPLATE
                                             -> CELLSCOPE_RESULT + SHA, checked against ecm.simulate (local)
   6 explainer    google/gemma-3-27b-it   ── markdown answer from spec table + sim + notes only
   7 critic (out) meta/llama-guard-4-12b  ── answer screen; unsafe -> refusal
```

## Otari features used (and where)

| Feature | What the app does | Code |
|---|---|---|
| Model routing & fallbacks | App level: ordered candidate list per role, retried on 408/429/5xx/timeout/404, each fallback logged in the trace (`event: fallback`). Seen live: Gemma-3-27B returned 504 and the explainer fell back to Llama-3.3-70B. Otari level: researcher/MCP roles send the routing alias `cellscope-researcher` (`OTARI_ROUTE_ALIAS`), and the trace shows the upstream that answered (`otari-model`, `otari-provider`, `otari-fallback` headers). | `cellscope/otari_client.py:19` (ROLE_MODELS), `:31` (alias), `:225-231` (fallback loop), `:215` (upstream model) |
| MCP integrations | DeepWiki MCP via the workspace server id (`mcp_server_ids`), falling back to an inline `mcp_servers` config. The model must return DeepWiki's per-query `deepwiki.com/search/...` URL, which shows the tool really ran. Otherwise the stage is marked "MCP unavailable, skipped". | `cellscope/pipeline.py:140-175` |
| Web search / web fetch | Researcher runs with `[{"type":"otari_web_search"},{"type":"otari_web_fetch"}]`, `max_tool_iterations: 6`. If the workspace blocks web tools, it degrades to answers flagged UNSOURCED. | `cellscope/pipeline.py:111-138` |
| Code execution | Simulator runs `ecm.SANDBOX_TEMPLATE` with `otari_code_execution`. The app parses the `CELLSCOPE_RESULT=` / `CELLSCOPE_SHA=` stdout lines (never the prose), re-hashes them, and compares them with the local run of the same `SIM_CORE`. | `cellscope/pipeline.py:177-192`, `cellscope/ecm.py:17,71,129,147` |
| Budgets & spending limits | Every call is tagged (`metadata`, `Otari-Tags`, `session_label`). Otari budget/ceiling refusals (402, or 403/429 with a budget code) are never routed around: the app shows the Otari status, `Otari-Error-Code` and body. | `cellscope/otari_client.py:56,148-158,173,225`, `cellscope/pipeline.py:285` |
| Per-user budgets | `user: <end_user>` on every request. The service key accepts it, and Otari auto-assigns "CellScope per-engineer daily" ($0.30/day). The app also keeps its own per-user ledger from `usage.cost_usd` with a soft cap (`CELLSCOPE_USER_BUDGET_USD`) and a UI budget bar. | `cellscope/otari_client.py:67,155,169`, `cellscope/server.py:30` |
| Guardrails | Request-level `guardrails:[{"profile":"cellscope-safety","on":["input"],"mode":"block"}]` (Lakera prompt-injection, BLOCK). A 403 `guardrail_violation` shows up as "Blocked by Otari guardrail cellscope-safety (score X)". In-app second layer: Llama Guard 4 on input and output, plus battery-abuse rules, because Llama Guard rated "bypass the BMS so the cell vents" as `safe`. | `cellscope/otari_client.py:47,158`, `cellscope/pipeline.py:20,86,275` |
| Multiple models, distinct roles | Five open-weight families in six roles (table below). | `cellscope/otari_client.py:19` |

## Model roles

| Role | Model (open-weight) | Why |
|---|---|---|
| Router | `alibaba/qwen3.5-9b` (fallback `meta/llama-3.1-8b-instruct`) | cheap JSON classification, `reasoning_effort: none` |
| Researcher + MCP docs | `cellscope-researcher` alias -> `alibaba/qwen3.6-35b-a3b` -> `meta/llama-3.3-70b-instruct` -> `deepseek/deepseek-v3.2` | tool use (web, MCP) |
| Simulator | `alibaba/qwen3-coder-30b-a3b-instruct` (fallback `alibaba/qwen-3.5-35b-a3b`) | runs code faithfully in the sandbox |
| Explainer | `google/gemma-3-27b-it` (fallback `meta/llama-3.3-70b-instruct`) | different family, writing for juniors |
| Safety critic | `meta/llama-guard-4-12b` (fallback `alibaba/qwen3guard-gen-8b`) | input + output classification |

## Run it

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt      # or: uv venv && uv pip install -r requirements.txt
cp .env.example .env   # fill OTARI_API_KEY (service key), OTARI_MCP_SERVER_IDS
set -a; source .env; set +a
.venv/bin/python -m pytest -q                                            # offline, mocked HTTP
.venv/bin/python -m uvicorn cellscope.server:app --port 8000             # UI at http://localhost:8000
.venv/bin/python -m cellscope "Compare Samsung INR21700-50E vs Molicel INR21700-P45B for a 3C fast-charge pack and estimate temperature rise." --user engineer-a --out run.json
```

Env vars: see `.env.example` (`OTARI_API_KEY`, `OTARI_BASE_URL`, `OTARI_ROUTE_ALIAS`, `OTARI_MCP_SERVER_IDS`,
`CELLSCOPE_MCP_MODE`, `OTARI_GUARDRAIL_PROFILE`, `CELLSCOPE_USER_BUDGET_USD`, `CELLSCOPE_USAGE_FILE`, `CELLSCOPE_SEND_USER_FIELD`).

A recorded run is in `examples/run_samsung50E_vs_P45B.json`: all stage events, spec table, verified sandbox output and
the final answer, $0.074 and 60 s end to end. The CLI transcript is in `examples/*.stderr.txt` and the answer is in `examples/*.md`.

## Demo script (5 steps)

1. **Full pipeline**: as `engineer-a`, run the default 50E vs P45B question. Point at the timeline: seven stages, each
   with its model, upstream provider, latency and cost. Then the spec table with datasheet links, the
   "sandbox output verified" badge, the plot, and the answer.
2. **Routing**: open the Trace panel. `cellscope-researcher -> Qwen/Qwen3.6-35B-A3B [deepinfra]`, the
   `otari_fallback` flag, the DeepWiki search URL from the MCP stage, and the code-execution tool line.
3. **Guardrail**: click "Injection (Otari guardrail)". Otari returns 403 `guardrail_violation` before any model runs,
   and the red card shows the profile and score.
4. **App safety layer**: click "Unsafe (Llama Guard + rules)". Llama Guard plus the battery-abuse rules refuse
   ("bypass the BMS ... vent").
5. **Per-user budget**: switch to `intern-b` and run until the $0.30/day Otari per-user budget is exhausted. The next
   request is refused by Otari, and the card shows the HTTP status, `Otari-Error-Code` and body. The budget bars come
   from `GET /usage`.

## Limitations

- Single-cell, single-RC model with a fixed NMC OCV curve, h = 15 W/m²K natural convection and DCIR split 70/30 into R0/R1.
  It's fine for comparing cells, not for sizing a cooling system. Missing datasheet values fall back to generic defaults,
  and the explainer is told which ones.
- Otari responses don't include tool results, so the app can't read sandbox stdout or MCP output directly:
  - Sandbox: the model has to echo stdout, so the app checks it with a SHA plus a local re-run.
  - MCP: the app relies on the DeepWiki search URL and call latency. This is a heuristic.
- The plot is drawn client-side as SVG from the verified arrays. Matplotlib is available in the sandbox (3.10.9; the
  sandbox renders a ~7 KB PNG and reports its size), but having the model echo a base64 PNG would cost ~6k output tokens
  and one wrong character breaks the image.
- With `reasoning_effort: none`, Qwen3.6 often skips the MCP tool and invents file paths. The MCP stage uses
  `reasoning_effort: low` plus a `tools_header`.
- Researcher spec values come from whatever pages web search finds, sometimes distributor copies of the datasheet.
  Always check the linked source.
