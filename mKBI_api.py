#!/usr/bin/env python3
"""
mKBI FastAPI wrapper
Exposes LLMService as a standalone HTTP microservice.

Endpoints:
  GET  /health          — liveness check
  POST /execute         — run the full interpreter -> fabricator -> exec chain
  POST /interpret       — interpreter stage only (no execution)

Configuration:
  MKBI_CONFIG   path to mKBI.toml  (default: mKBI.toml)
  MKBI_HOST     bind host          (default: 0.0.0.0)
  MKBI_PORT     bind port          (default: 8000)
  MKBI_TOKEN    bearer token for auth (optional; if unset, auth is disabled)

Run:
  uvicorn mKBI_api:app --host 0.0.0.0 --port 8000
  python3 mKBI_api:app   (uses __main__ block with uvicorn programmatically)
"""

import os
import time
import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Depends, Request, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field

# mKBI must be importable — place this file alongside mKBI.py
from mKBI import LLMService


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("mKBI_api")


# ---------------------------------------------------------------------------
# Config from environment
# ---------------------------------------------------------------------------

MKBI_CONFIG = os.getenv("MKBI_CONFIG", "mKBI.toml")
MKBI_HOST   = os.getenv("MKBI_HOST", "0.0.0.0")
MKBI_PORT   = int(os.getenv("MKBI_PORT", "8000"))
MKBI_TOKEN  = os.getenv("MKBI_TOKEN", "")       # empty string = auth disabled


# ---------------------------------------------------------------------------
# Service lifecycle
# ---------------------------------------------------------------------------

_service: Optional[LLMService] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _service
    log.info("Loading LLMService from config: %s", MKBI_CONFIG)
    _service = LLMService(config_path=MKBI_CONFIG)
    log.info("LLMService ready. Model: %s", _service.model)
    yield
    log.info("Shutting down.")


def get_service() -> LLMService:
    if _service is None:
        raise HTTPException(status_code=503, detail="Service not initialised.")
    return _service


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

_bearer = HTTPBearer(auto_error=False)


def check_auth(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
):
    if not MKBI_TOKEN:
        return   # auth disabled
    if credentials is None or credentials.credentials != MKBI_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing bearer token.",
            headers={"WWW-Authenticate": "Bearer"},
        )


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class ExecuteRequest(BaseModel):
    request: str = Field(..., description="Natural language task to execute.")
    output_only: bool = Field(
        False,
        description="If true, return only stdout instead of the full result dict.",
    )


class InterpretRequest(BaseModel):
    request: str = Field(..., description="Natural language task to interpret.")


class HealthResponse(BaseModel):
    status: str
    model: str
    uptime_seconds: float


class ExecuteResponse(BaseModel):
    interpreter_response: str
    fabricator_response: str
    script: str
    shellcheck_passed: bool
    shellcheck_output: str
    execution: Optional[dict]
    error: Optional[str]
    elapsed_seconds: float


class OutputOnlyResponse(BaseModel):
    output: str
    elapsed_seconds: float


class InterpretResponse(BaseModel):
    response: str
    elapsed_seconds: float


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="mKBI API",
    description="HTTP interface for the mini Kernel Bound Intelligence agent.",
    version="1.0.0",
    lifespan=lifespan,
)

_start_time = time.monotonic()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health", response_model=HealthResponse, tags=["meta"])
def health(svc: LLMService = Depends(get_service)):
    return HealthResponse(
        status="ok",
        model=svc.model,
        uptime_seconds=round(time.monotonic() - _start_time, 2),
    )


@app.post(
    "/execute",
    tags=["agent"],
    dependencies=[Depends(check_auth)],
)
def execute(
    body: ExecuteRequest,
    svc: LLMService = Depends(get_service),
):
    """
    Run the full chain: Interpreter -> Fabricator -> shellcheck -> subprocess.
    Returns either a full result dict or stdout string depending on output_only.
    """
    log.info("execute: %s", body.request[:120])
    t0 = time.monotonic()

    outcome = svc.execute_task(body.request, output_only=body.output_only)
    elapsed = round(time.monotonic() - t0, 3)

    if body.output_only:
        return OutputOnlyResponse(output=str(outcome), elapsed_seconds=elapsed)

    outcome["elapsed_seconds"] = elapsed
    return outcome


@app.post(
    "/interpret",
    response_model=InterpretResponse,
    tags=["agent"],
    dependencies=[Depends(check_auth)],
)
def interpret(
    body: InterpretRequest,
    svc: LLMService = Depends(get_service),
):
    """
    Run the Interpreter stage only. Returns the raw LLM response without
    fabrication or execution. Useful for dry-run inspection or XMPP chat replies.
    """
    log.info("interpret: %s", body.request[:120])
    t0 = time.monotonic()

    response = svc.create_chat([{"role": "user", "content": body.request}])
    elapsed = round(time.monotonic() - t0, 3)

    return InterpretResponse(response=response, elapsed_seconds=elapsed)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "mKBI_api:app",
        host=MKBI_HOST,
        port=MKBI_PORT,
        log_level="info",
    )
