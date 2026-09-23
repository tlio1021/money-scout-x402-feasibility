"""Minimal x402-paid feasibility API.

The seller only supplies a public receiving address.  No wallet signer, seed,
private key, trading capability, paid AI, or outbound financial action exists in
this process.  The deterministic scoring implementation is reused from C26.
"""

from __future__ import annotations

import os
import re
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from workers.c26_okx.service import FeasibilityRequest, contains_secret, evaluate


PRICE_USD = "$0.05"
BASE_MAINNET = "eip155:8453"
BASE_SEPOLIA = "eip155:84532"
MAINNET_FACILITATOR = "https://facilitator.payai.network"
TESTNET_FACILITATOR = "https://x402.org/facilitator"
EVM_ADDRESS = re.compile(r"^0x[a-fA-F0-9]{40}$")
DISCOVERY_INPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "candidate_id": {"type": "string", "minLength": 1, "maxLength": 80},
        "title": {"type": "string", "minLength": 3, "maxLength": 200},
        "as_of": {"type": "string", "format": "date-time"},
        "evidence": {
            "type": "array",
            "minItems": 1,
            "maxItems": 30,
            "items": {
                "type": "object",
                "properties": {
                    "evidence_id": {"type": "string"},
                    "source_url": {"type": "string", "format": "uri"},
                    "source_kind": {
                        "type": "string",
                        "enum": ["OFFICIAL", "PRIMARY_PUBLIC", "SECONDARY_PUBLIC"],
                    },
                    "observed_at": {"type": "string", "format": "date-time"},
                    "facts": {"type": "object"},
                },
                "required": [
                    "evidence_id",
                    "source_url",
                    "source_kind",
                    "observed_at",
                    "facts",
                ],
            },
        },
    },
    "required": ["candidate_id", "title", "as_of", "evidence"],
}


def price_config(pay_to_address: str, *, mode: str) -> dict[str, str]:
    if not EVM_ADDRESS.fullmatch(pay_to_address):
        raise ValueError("PAY_TO_ADDRESS_MUST_BE_EVM_PUBLIC_ADDRESS")
    if mode not in {"mainnet", "testnet"}:
        raise ValueError("X402_MODE_MUST_BE_MAINNET_OR_TESTNET")
    return {
        "scheme": "exact",
        "pay_to": pay_to_address,
        "price": PRICE_USD,
        "network": BASE_MAINNET if mode == "mainnet" else BASE_SEPOLIA,
    }


def evaluate_snapshot(body: FeasibilityRequest) -> dict[str, Any]:
    if contains_secret(body.model_dump(mode="json")):
        raise ValueError("SECRET_INPUT_DENIED")
    return evaluate(body)


def _add_x402_payment(
    app: FastAPI,
    *,
    pay_to_address: str,
    mode: str,
    facilitator_url: str | None,
    public_base_url: str | None,
) -> None:
    from x402.extensions.bazaar import OutputConfig, declare_discovery_extension
    from x402.http import FacilitatorConfig, HTTPFacilitatorClient, PaymentOption
    from x402.http.middleware.fastapi import PaymentMiddlewareASGI
    from x402.http.types import RouteConfig
    from x402.mechanisms.evm.exact import ExactEvmServerScheme
    from x402.server import x402ResourceServer

    config = price_config(pay_to_address, mode=mode)
    facilitator = HTTPFacilitatorClient(
        FacilitatorConfig(
            url=facilitator_url
            or (MAINNET_FACILITATOR if mode == "mainnet" else TESTNET_FACILITATOR)
        )
    )
    server = x402ResourceServer(facilitator)
    server.register(config["network"], ExactEvmServerScheme())
    resource = f"{public_base_url.rstrip('/')}/execute" if public_base_url else None
    discovery = declare_discovery_extension(
        input={
            "candidate_id": "candidate-1",
            "title": "Public data transformation",
            "as_of": "2026-09-23T00:00:00Z",
            "evidence": [
                {
                    "evidence_id": "official-1",
                    "source_url": "https://example.com/public-evidence",
                    "source_kind": "OFFICIAL",
                    "observed_at": "2026-09-22T23:00:00Z",
                    "facts": {"payer_identified": "YES"},
                }
            ],
        },
        input_schema=DISCOVERY_INPUT_SCHEMA,
        body_type="json",
        output=OutputConfig(
            example={
                "candidate_id": "candidate-1",
                "decision": "INCONCLUSIVE",
                "actual_revenue_status": "UNKNOWN",
            },
            schema={"type": "object"},
        ),
    )
    routes = {
        "POST /execute": RouteConfig(
            accepts=[PaymentOption(**config)],
            resource=resource,
            description=(
                "Deterministic opportunity feasibility score with evidence cut-off, "
                "conflict detection, blockers, and explicit UNKNOWN preservation."
            ),
            mime_type="application/json",
            service_name="Money Scout Feasibility",
            tags=["opportunity", "feasibility", "risk", "evidence"],
            extensions=discovery,
        )
    }
    app.add_middleware(PaymentMiddlewareASGI, routes=routes, server=server)


def build_app(
    *,
    pay_to_address: str | None,
    mode: str = "mainnet",
    facilitator_url: str | None = None,
    public_base_url: str | None = None,
) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health")
    def health():
        return {
            "status": "ok",
            "service": "money-scout-feasibility",
            "x402_configured": bool(pay_to_address),
            "mode": mode,
            "paid_ai_enabled": False,
            "wallet_signer_present": False,
        }

    @app.get("/about")
    def about():
        return {
            "service": "Money Scout Feasibility",
            "price": PRICE_USD,
            "currency": "USDC",
            "network": BASE_MAINNET if mode == "mainnet" else BASE_SEPOLIA,
            "actual_revenue_status": "UNKNOWN",
            "limitations": [
                "Caller supplies public evidence; source contents are not fetched.",
                "A score is not acceptance, payment, profit, or professional advice.",
            ],
        }

    if pay_to_address:
        _add_x402_payment(
            app,
            pay_to_address=pay_to_address,
            mode=mode,
            facilitator_url=facilitator_url,
            public_base_url=public_base_url,
        )

        @app.post("/execute")
        def execute(body: FeasibilityRequest):
            try:
                return evaluate_snapshot(body)
            except ValueError as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)
    else:

        @app.post("/execute")
        def execute_unconfigured():
            return JSONResponse(
                {"error": "PAYOUT_ADDRESS_NOT_CONFIGURED"}, status_code=503
            )

    return app


app = build_app(
    pay_to_address=os.getenv("MONEY_SCOUT_X402_PAY_TO"),
    mode=os.getenv("MONEY_SCOUT_X402_MODE", "mainnet"),
    facilitator_url=os.getenv("MONEY_SCOUT_X402_FACILITATOR_URL"),
    public_base_url=(
        os.getenv("MONEY_SCOUT_X402_PUBLIC_BASE_URL")
        or os.getenv("RENDER_EXTERNAL_URL")
    ),
)
