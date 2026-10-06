"""AgentCore Platform v1.0"""

# Standalone HTTP entry point for the agent.
# Entry points are adapters only — no business logic here.
# For platform-level routing, AgentGateway calls agent.invoke() directly.
#
# Class-name contract:
#   graph.py:     class AppiComplianceQAAgent(AgentBaseGraph)
#   agent.yaml:   class: "src.graph.graph.AppiComplianceQAAgent"
#   server.py:    from src.graph.graph import AppiComplianceQAAgent

import json
import os
import secrets
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import yaml
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from shared.secrets import factory as secrets_factory
from src.graph.graph import AppiComplianceQAAgent

# Serialized size cap for the caller-metadata channel — enforced at the
# adapter so oversized payloads never reach the graph.
_MAX_INPUT_CONTEXT_BYTES = 256 * 1024

app = FastAPI(title="FIN-C2-145 APPI 2026 Compliance Q&A")


def _load_runtime_config() -> dict[str, Any]:
    """Load config/config.yaml — the runtime parameters (max_retry, ...).

    The platform registry loads this file itself and passes it to the graph
    constructor; the standalone server must do the same, or the declared
    values silently never reach the graph.
    """
    config_path = Path(__file__).resolve().parents[2] / "config" / "config.yaml"
    if not config_path.exists():
        return {}
    with open(config_path, encoding="utf-8") as fh:
        loaded = yaml.safe_load(fh)
    return loaded if isinstance(loaded, dict) else {}


agent = AppiComplianceQAAgent(config=_load_runtime_config())
agent.compile()
# namespace and agent_name match the manifest values in config/agent.yaml
agent.provision_secrets(secrets_factory(namespace="fin", agent_name="AppiComplianceQAAgent"))


class InvokeRequest(BaseModel):
    input: str
    session_id: str = ""
    # Structured caller metadata (channel, topic_filter, top_k) — validated
    # field-by-field in PreProcessNode; the adapter only enforces the size cap.
    input_context: dict[str, Any] = Field(default_factory=dict)


@app.post("/invoke")
async def invoke(req: InvokeRequest, request: Request) -> dict[str, Any]:
    context_size = len(json.dumps(req.input_context, ensure_ascii=False).encode("utf-8"))
    if context_size > _MAX_INPUT_CONTEXT_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"input_context exceeds the {_MAX_INPUT_CONTEXT_BYTES}-byte limit",
        )

    trust = getattr(request.state, "trust_level", TrustLevel.ANONYMOUS)
    # Standalone caller auth: when INVOKE_AUTH_TOKEN is set on the server
    # environment, callers that no upstream middleware vouched for (still
    # ANONYMOUS) must present it as a Bearer token and run at
    # VERIFIED_EXTERNAL. Middleware-established trust is never demoted.
    # This adapter is the entry-point auth boundary (standalone equivalent
    # of the platform auth middleware) — a deployment-level caller
    # credential, not an agent secret, so the agent secrets provider does
    # not apply (no InvocationContext exists before auth).
    #
    # Required here specifically: PreProcessNode occupies the pre_process
    # backbone slot and declares required_trust_level =
    # TrustLevel.VERIFIED_EXTERNAL. Nothing else sets
    # request.state.trust_level in the standalone deployment, so without
    # this boundary every caller arrives ANONYMOUS, the trust gate denies
    # it, and the agent can only ever return status="error".
    expected = os.environ.get("INVOKE_AUTH_TOKEN")
    if expected and trust is TrustLevel.ANONYMOUS:
        supplied = request.headers.get("authorization", "")
        # Compare bytes: compare_digest raises TypeError on non-ASCII str input
        # (headers decode as latin-1), which would 500 instead of the generic 401.
        if not secrets.compare_digest(supplied.encode(), f"Bearer {expected}".encode()):
            # Generic body on purpose — do not leak whether the token was absent,
            # malformed, or wrong.
            raise HTTPException(status_code=401, detail="Token is invalid or expired.")
        trust = TrustLevel.VERIFIED_EXTERNAL
    with bound_secrets(agent._secrets_provider):
        ctx = InvocationContext(
            session_id=req.session_id or str(uuid4()),
            caller_trust_level=trust,
            caller_id=getattr(request.state, "caller_id", ""),
        )
        return cast(
            "dict[str, Any]",
            agent.invoke(req.input, input_context=req.input_context, ctx=ctx),
        )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "agent": "AppiComplianceQAAgent", "template": "FIN-C2-145"}
