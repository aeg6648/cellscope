"""Thin Otari gateway client: role-based model routing with fallbacks, cost
tracking per end user, Otari-Tags / metadata attribution, guardrails, and
budget-block surfacing. Plain httpx, no SDK."""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import httpx

BASE_URL = os.environ.get("OTARI_BASE_URL", "https://otari.ai/api/v1")

# Ordered candidate list per role (app-level routing & fallback). All open-weight.
ROLE_MODELS: dict[str, list[str]] = {
    "router": ["alibaba/qwen3.5-9b", "meta/llama-3.1-8b-instruct"],
    "researcher": ["alibaba/qwen3.6-35b-a3b", "meta/llama-3.3-70b-instruct", "deepseek/deepseek-v3.2"],
    "mcp_docs": ["alibaba/qwen3.6-35b-a3b", "meta/llama-3.3-70b-instruct", "deepseek/deepseek-v3.2"],
    "simulator": ["alibaba/qwen3-coder-30b-a3b-instruct", "alibaba/qwen-3.5-35b-a3b"],
    "explainer": ["google/gemma-3-27b-it", "meta/llama-3.3-70b-instruct"],
    "critic": ["meta/llama-guard-4-12b", "alibaba/qwen3guard-gen-8b"],
}
RETRYABLE = {408, 429, 500, 502, 503, 504, 529}
BUDGET_CODES = ("budget", "ceiling", "spend", "quota", "insufficient", "balance", "credit")


def candidates_for(role: str) -> list[str]:
    """Model candidates for a role; an Otari routing alias (dashboard-defined,
    with its own provider fallback) is tried first for the researcher roles."""
    models = list(ROLE_MODELS[role])
    alias = os.environ.get("OTARI_ROUTE_ALIAS")
    if alias and role in ("researcher", "mcp_docs"):
        models.insert(0, alias)
    return models


class OtariError(Exception):
    def __init__(self, status: int, code: str, body: Any, model: str = ""):
        self.status, self.code, self.body, self.model = status, code, body, model
        super().__init__(f"Otari HTTP {status} {code}: {str(body)[:300]}")

    @property
    def guardrail(self) -> Optional[dict]:
        """Otari request-level guardrail verdict, e.g. 403 {"detail": {"code": "guardrail_violation", ...}}."""
        d = self.body.get("detail") if isinstance(self.body, dict) else None
        if isinstance(d, dict) and (d.get("code") == "guardrail_violation" or "guardrail" in self.code):
            g = (d.get("guardrails") or [{}])[0]
            return {"profile": g.get("profile", "?"), "score": g.get("score"), "message": d.get("message", "")}
        return None

    @property
    def is_budget_block(self) -> bool:
        if self.guardrail:
            return False
        blob = (self.code + " " + json.dumps(self.body, default=str)).lower()
        return self.status == 402 or (self.status in (403, 429) and any(k in blob for k in BUDGET_CODES))


class UserBudgetExceeded(Exception):
    pass


class UsageLedger:
    """Cumulative spend (usage.cost_usd) per end user, persisted to JSON."""

    def __init__(self, path: Optional[str] = None):
        self.path = Path(path or os.environ.get("CELLSCOPE_USAGE_FILE", ".cellscope_usage.json"))
        self.lock = threading.Lock()
        self.data: dict[str, float] = {}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text())
            except ValueError:
                self.data = {}

    @staticmethod
    def cap() -> float:
        return float(os.environ.get("CELLSCOPE_USER_BUDGET_USD", "0.25"))

    def spent(self, user: str) -> float:
        return self.data.get(user, 0.0)

    def add(self, user: str, cost: float) -> None:
        with self.lock:
            self.data[user] = round(self.data.get(user, 0.0) + cost, 6)
            try:
                self.path.write_text(json.dumps(self.data, indent=1))
            except OSError:
                pass

    def snapshot(self) -> dict:
        return {u: {"spent_usd": s, "cap_usd": self.cap()} for u, s in self.data.items()}


@dataclass
class CallResult:
    role: str
    model_requested: str
    model_answered: str
    content: str
    latency_s: float
    cost_usd: float
    provider: str = ""
    upstream_model: str = ""
    otari_fallback: str = ""
    fallbacks: list = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    def summary(self) -> dict:
        return {"role": self.role, "model_requested": self.model_requested,
                "model_answered": self.model_answered, "provider": self.provider,
                "upstream_model": self.upstream_model, "otari_fallback": self.otari_fallback,
                "latency_s": round(self.latency_s, 2), "cost_usd": self.cost_usd,
                "fallbacks": self.fallbacks}


def parse_cost(body: dict) -> float:
    try:
        return float((body.get("usage") or {}).get("cost_usd") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def error_code(resp: httpx.Response) -> str:
    code = resp.headers.get("Otari-Error-Code", "")
    if not code:
        try:
            b = resp.json()
            code = str(b.get("code") or (b.get("error") or {}).get("code") or "") if isinstance(b, dict) else ""
        except ValueError:
            pass
    return code


class OtariClient:
    def __init__(self, api_key: Optional[str] = None, ledger: Optional[UsageLedger] = None,
                 transport: Optional[httpx.BaseTransport] = None, timeout: float = 150.0):
        self.api_key = api_key or os.environ.get("OTARI_API_KEY", "")
        self.ledger = ledger or UsageLedger()
        self.http = httpx.Client(base_url=BASE_URL, timeout=timeout, transport=transport)
        self.send_user_field = os.environ.get("CELLSCOPE_SEND_USER_FIELD", "1") == "1"

    def _body(self, model, role, messages, end_user, **kw) -> dict:
        body: dict[str, Any] = {
            "model": model, "messages": messages,
            "metadata": {"app": "cellscope", "role": role, "end_user": end_user},
            "safety_identifier": end_user,
            "session_label": kw.pop("session_label", None) or f"cellscope-{end_user}",
        }
        if self.send_user_field:
            body["user"] = end_user
        profile = os.environ.get("OTARI_GUARDRAIL_PROFILE")
        if profile:
            body["guardrails"] = [{"profile": profile, "on": ["input"], "mode": "block"}]
        body.update({k: v for k, v in kw.items() if v is not None})
        return body

    def chat(self, role: str, messages: list, end_user: str = "engineer-a", *,
             tools: Optional[list] = None, mcp_servers: Optional[list] = None,
             mcp_server_ids: Optional[list] = None, max_tool_iterations: Optional[int] = None, response_format: Optional[dict] = None,
             max_tokens: int = 800, reasoning_effort: Optional[str] = "none",
             temperature: Optional[float] = 0.2, session_label: Optional[str] = None,
             tools_header: Optional[str] = None, max_candidates: Optional[int] = None) -> CallResult:
        if self.ledger.spent(end_user) >= self.ledger.cap():
            raise UserBudgetExceeded(
                f"End user '{end_user}' has spent ${self.ledger.spent(end_user):.4f} of the "
                f"${self.ledger.cap():.2f} CellScope soft cap. Ask an admin to raise CELLSCOPE_USER_BUDGET_USD.")
        headers = {"Authorization": f"Bearer {self.api_key}",
                   "Otari-Tags": f"app=cellscope,role={role},end_user={end_user}"}
        fallbacks: list = []
        last_err: Optional[Exception] = None
        for model in candidates_for(role)[:max_candidates]:
            body = self._body(model, role, messages, end_user, tools=tools, mcp_servers=mcp_servers,
                              mcp_server_ids=mcp_server_ids,
                              max_tool_iterations=max_tool_iterations, response_format=response_format,
                              max_tokens=max_tokens, reasoning_effort=reasoning_effort,
                              temperature=temperature, session_label=session_label,
                              tools_header=tools_header)
            t0 = time.time()
            for _attempt in range(3):  # same-model retries for request-shape problems only
                try:
                    resp = self.http.post("/chat/completions", headers=headers, json=body)
                except (httpx.TimeoutException, httpx.TransportError) as e:
                    resp, last_err = None, e
                    break
                if resp.status_code == 403 and "user" in resp.text and "does not match" in resp.text:
                    # API keys can only send their own user id; attribute via metadata instead.
                    self.send_user_field = False
                    body.pop("user", None)
                    fallbacks.append({"model": model, "event": "user_field_rejected", "status": 403})
                    continue
                if resp.status_code in (400, 422) and "response_format" in body:
                    body.pop("response_format")
                    fallbacks.append({"model": model, "event": "response_format_unsupported"})
                    continue
                if resp.status_code in (400, 422) and body.get("reasoning_effort"):
                    body.pop("reasoning_effort")
                    fallbacks.append({"model": model, "event": "reasoning_effort_unsupported"})
                    continue
                break
            if resp is None:
                fallbacks.append({"model": model, "event": "timeout_or_network", "error": str(last_err)[:200]})
                continue
            if resp.status_code == 200:
                data = resp.json()
                cost = parse_cost(data)
                self.ledger.add(end_user, cost)
                msg = (data.get("choices") or [{}])[0].get("message") or {}
                return CallResult(role=role, model_requested=model,
                                  model_answered=data.get("model") or model,
                                  upstream_model=resp.headers.get("otari-model", ""),
                                  otari_fallback=resp.headers.get("otari-fallback", ""),
                                  content=msg.get("content") or "", latency_s=time.time() - t0,
                                  cost_usd=cost, provider=resp.headers.get("otari-provider", ""),
                                  fallbacks=fallbacks, raw=data)
            try:
                err_body: Any = resp.json()
            except ValueError:
                err_body = resp.text[:500]
            err = OtariError(resp.status_code, error_code(resp), err_body, model)
            if err.is_budget_block or err.guardrail:
                raise err  # hard Otari budget/ceiling: surface verbatim, never route around it
            if resp.status_code in RETRYABLE or resp.status_code == 404:
                fallbacks.append({"model": model, "event": "fallback", "status": resp.status_code,
                                  "code": err.code, "error": str(err_body)[:200]})
                last_err = err
                continue
            raise err
        raise last_err or RuntimeError(f"no candidates for role {role}")
