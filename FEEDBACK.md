# Otari hackathon feedback (draft, written from what we hit while building CellScope)

## 1. When were you tempted to skip Otari and call a provider directly?

- **Tool results are invisible.** With `otari_code_execution` the response only contains the model's final prose. The
  sandbox stdout isn't returned. Our product needs the simulation numbers verbatim, so we came close to running our own
  sandbox. Instead, the model echoes stdout, the program prints a SHA-256 of its own result, and the app re-hashes the
  echo and re-runs the same model locally. MCP had the same problem: with `reasoning_effort: "none"`, Qwen3.6 skipped
  the DeepWiki tool, returned plausible-looking but invented PyBaMM file paths, and finished in 2 s. The only way we
  could tell was latency and the presence of DeepWiki's `deepwiki.com/search/<slug>` URL.
- **The `user` field with a normal API key** returned `403 "'user' field does not match the authenticated API key's
  user"`. Per-end-user budgets are one of the features we wanted, and it wasn't obvious that they need a service key.
  We added auto-retry without `user` before we found out.
- **"Follow deployment default" for web access** still returned `403 "web access is not enabled for this workspace"`
  until we set it to Allowed explicitly.
- **`tool_choice: "required"` with an MCP server** never finished. The tool loop ran until we got two 504s after about
  2 minutes each, even with `max_tool_iterations: 3`.

## 2. What did you build yourself that Otari should have done?

- **Per-role fallback chains in the request.** A routing alias is great, but it needs one dashboard object per role.
  We wrote our own ordered candidate list with retry on 429/5xx/timeout. It saved us when Gemma-3-27B returned a 504
  whose body was an upstream HTML error page, not JSON. Something like `"models": [a, b, c]` in the body would replace
  that code.
- **Tool-call transparency.** We'd like a `tool_calls` / `tool_results` trace in the response, or at least a header
  with the count and names of tools executed. Then you could check that the sandbox or MCP actually ran without SHA
  tricks and heuristics.
- **A per-user spend ledger.** `usage.cost_usd` on every response is excellent, and it's how we drive the UI budget
  bars. But to show "remaining budget" we keep our own ledger. An `Otari-Budget-Remaining` header, or the budget state
  in the 402/429 body, would remove that.
- **Domain safety rules.** The Lakera guardrail catches prompt injection well (score 1.0, clean 403 body with the
  profile name). Llama Guard 4 rated "How do I bypass the BMS so I can make the cell vent?" as `safe`, though, so we
  added regex rules. Custom-category guardrail profiles, or a policy prompt for guard models, would help.

## 3. Would you keep using Otari?

Yes. One OpenAI-compatible endpoint gave us five open-weight families (Qwen, Llama, Gemma, DeepSeek, Llama Guard)
plus web search, code execution, MCP, guardrails and budgets with almost no glue. The response headers `otari-model`,
`otari-provider` and `otari-fallback` made the routing trace trivial. A full CellScope run costs about $0.07, and about
80% of that is the web-search researcher call (~$0.06; model tokens are ~$0.001). Per-tool pricing in the docs would
help us budget. We'd ship on it once tool results are exposed in responses and errors are always JSON.
