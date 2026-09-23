"""Local-only C26 A2MCP-shaped feasibility endpoint.

This module intentionally contains no wallet, x402, provider SDK, external fetch,
LLM, or deployment integration. It evaluates caller-supplied public evidence.
"""

from __future__ import annotations

import ipaddress
import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


MAX_BODY_BYTES = 32_768
SECRET_KEYS = re.compile(
    r"password|api.?key|secret|cookie|credential|private.?key|seed|authorization|token",
    re.I,
)
SECRET_VALUES = re.compile(
    r"sk-[A-Za-z0-9_-]{8,}|-----BEGIN .*PRIVATE KEY-----|Bearer\s+\S{8,}|\b(?:[a-z]+\s+){11,23}[a-z]+\b",
    re.I,
)


class TriState(str, Enum):
    YES = "YES"
    NO = "NO"
    UNKNOWN = "UNKNOWN"


class SourceKind(str, Enum):
    OFFICIAL = "OFFICIAL"
    PRIMARY_PUBLIC = "PRIMARY_PUBLIC"
    SECONDARY_PUBLIC = "SECONDARY_PUBLIC"


class Facts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    payer_identified: TriState = TriState.UNKNOWN
    payment_defined: TriState = TriState.UNKNOWN
    currently_open: TriState = TriState.UNKNOWN
    ai_automation_allowed: TriState = TriState.UNKNOWN
    korea_eligible: TriState = TriState.UNKNOWN
    terms_clear: TriState = TriState.UNKNOWN
    payout_evidence: TriState = TriState.UNKNOWN
    repeatable: TriState = TriState.UNKNOWN
    settlement_time_defined: TriState = TriState.UNKNOWN
    upfront_cost_krw: int | None = Field(default=None, ge=0, le=30_000_000)


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9._:-]+$")
    source_url: str = Field(min_length=12, max_length=2048)
    source_kind: SourceKind
    observed_at: datetime
    facts: Facts

    @field_validator("source_url")
    @classmethod
    def validate_public_https_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("source_url must be a credential-free public HTTPS URL")
        host = parsed.hostname.rstrip(".").lower()
        if host == "localhost" or host.endswith(".localhost"):
            raise ValueError("private source_url is denied")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address and not address.is_global:
            raise ValueError("private source_url is denied")
        return value

    @field_validator("observed_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("observed_at must include a timezone")
        return value.astimezone(timezone.utc)


class FeasibilityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9._:-]+$")
    title: str = Field(min_length=3, max_length=200)
    as_of: datetime
    evidence: list[Evidence] = Field(min_length=1, max_length=30)

    @field_validator("as_of")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("as_of must include a timezone")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def unique_evidence_ids(self):
        ids = [item.evidence_id for item in self.evidence]
        if len(ids) != len(set(ids)):
            raise ValueError("evidence_id must be unique")
        return self


TRI_FIELDS = (
    "payer_identified",
    "payment_defined",
    "currently_open",
    "ai_automation_allowed",
    "korea_eligible",
    "terms_clear",
    "payout_evidence",
    "repeatable",
    "settlement_time_defined",
)
WEIGHTS = {
    "payer_identified": 15,
    "payment_defined": 10,
    "currently_open": 10,
    "ai_automation_allowed": 15,
    "korea_eligible": 15,
    "terms_clear": 10,
    "payout_evidence": 10,
    "repeatable": 5,
    "settlement_time_defined": 5,
}
HARD_BLOCKERS = ("currently_open", "ai_automation_allowed", "korea_eligible", "terms_clear")
CORE_FIELDS = (
    "payer_identified",
    "payment_defined",
    "currently_open",
    "ai_automation_allowed",
    "korea_eligible",
    "terms_clear",
)


def contains_secret(value: Any) -> bool:
    if isinstance(value, dict):
        return any(SECRET_KEYS.search(str(key)) or contains_secret(item) for key, item in value.items())
    if isinstance(value, list):
        return any(contains_secret(item) for item in value)
    return isinstance(value, str) and bool(SECRET_VALUES.search(value))


def aggregate_fact(included: list[Evidence], field: str) -> tuple[str, bool]:
    values = {
        getattr(item.facts, field).value
        for item in included
        if getattr(item.facts, field) is not TriState.UNKNOWN
    }
    if len(values) == 1:
        return values.pop(), False
    return TriState.UNKNOWN.value, len(values) > 1


def evaluate(request: FeasibilityRequest) -> dict[str, Any]:
    included = [item for item in request.evidence if item.observed_at <= request.as_of]
    excluded = [item for item in request.evidence if item.observed_at > request.as_of]
    reasons: list[str] = []
    facts: dict[str, Any] = {}

    for field in TRI_FIELDS:
        value, conflict = aggregate_fact(included, field)
        facts[field] = value
        if conflict:
            reasons.append(f"CONFLICT:{field}")

    costs = [item.facts.upfront_cost_krw for item in included if item.facts.upfront_cost_krw is not None]
    facts["upfront_cost_krw"] = max(costs) if costs else None

    score = sum(WEIGHTS[field] for field in TRI_FIELDS if facts[field] == TriState.YES.value)
    if facts["upfront_cost_krw"] == 0:
        score += 5

    blockers = [field for field in HARD_BLOCKERS if facts[field] == TriState.NO.value]
    reasons.extend(f"BLOCKER:{field}" for field in blockers)
    unknown_core = [field for field in CORE_FIELDS if facts[field] == TriState.UNKNOWN.value]
    reasons.extend(f"UNKNOWN:{field}" for field in unknown_core if f"CONFLICT:{field}" not in reasons)

    if blockers:
        decision = "REJECTED"
    elif not included:
        decision = "INCONCLUSIVE"
        reasons.append("NO_EVIDENCE_AT_OR_BEFORE_CUTOFF")
    elif score >= 80 and not unknown_core:
        decision = "QUALIFIED"
    elif score >= 55:
        decision = "NEEDS_REVIEW"
    else:
        decision = "INCONCLUSIVE"

    completed = datetime.now(timezone.utc)
    if completed < request.as_of:
        completed = request.as_of
    return {
        "candidate_id": request.candidate_id,
        "title": request.title,
        "decision": decision,
        "score": score,
        "score_scale": 100,
        "actual_revenue_status": "UNKNOWN",
        "as_of": request.as_of.isoformat().replace("+00:00", "Z"),
        "decision_completed_at": completed.isoformat().replace("+00:00", "Z"),
        "facts": facts,
        "reasons": reasons,
        "included_evidence_ids": [item.evidence_id for item in included],
        "excluded_evidence_ids": [item.evidence_id for item in excluded],
        "limitations": [
            "Caller-supplied structured evidence was evaluated; source contents were not fetched.",
            "QUALIFIED is feasibility screening, not acceptance, payment, profit, or legal advice.",
            "Evidence observed after as_of was excluded from this decision.",
        ],
    }


def create_app() -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def local_safety(request: Request, call_next):
        host = request.headers.get("host", "").split(":", 1)[0].lower()
        if host not in {"127.0.0.1", "localhost", "testserver"}:
            return JSONResponse({"error": "LOCAL_HOST_ONLY"}, status_code=403)
        if request.method == "POST":
            if not request.headers.get("content-type", "").lower().startswith("application/json"):
                return JSONResponse({"error": "JSON_REQUIRED"}, status_code=415)
            try:
                declared = int(request.headers.get("content-length", "0"))
            except ValueError:
                return JSONResponse({"error": "INVALID_LENGTH"}, status_code=400)
            if declared > MAX_BODY_BYTES:
                return JSONResponse({"error": "REQUEST_TOO_LARGE"}, status_code=413)
            chunks: list[bytes] = []
            size = 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_BODY_BYTES:
                    return JSONResponse({"error": "REQUEST_TOO_LARGE"}, status_code=413)
                chunks.append(chunk)
            request._body = b"".join(chunks)
            try:
                raw = await request.json()
            except Exception:
                return JSONResponse({"error": "INVALID_JSON"}, status_code=400)
            if contains_secret(raw):
                return JSONResponse({"error": "SECRET_INPUT_DENIED"}, status_code=400)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.get("/health")
    def health():
        return {"status": "ok", "mode": "LOCAL_ONLY", "external_calls": 0}

    @app.get("/about")
    def about():
        return {
            "candidate_id": "C26",
            "service": "Official-Source Opportunity Feasibility Brief",
            "version": "0.1.0-local",
            "wallet_connected": False,
            "x402_enabled": False,
            "external_fetch_enabled": False,
            "paid_ai_enabled": False,
            "actual_revenue_status": "UNKNOWN",
        }

    @app.post("/execute")
    def execute(body: FeasibilityRequest):
        return evaluate(body)

    return app


app = create_app()

