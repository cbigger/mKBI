#!/usr/bin/env python3
"""
mKBI FastAPI wrapper
Exposes LLMService as a standalone HTTP microservice.

Endpoints:
  GET  /health                      — liveness check
  GET  /skills                      — list registered skills
  POST /skills/reload               — rescan skills directory
  POST /skills/{skill}/execute      — full chain for named skill
  POST /skills/{skill}/interpret    — interpreter stage only for named skill
  POST /execute                     — full chain using default_skill (backward compat)
  POST /interpret                   — interpret only using default_skill (backward compat)

Configuration:
  MKBI_CONFIG   path to mKBI.toml  (default: mKBI.toml)
  MKBI_HOST     bind host          (default: 0.0.0.0)
  MKBI_PORT     bind port          (default: 8000)
  MKBI_TOKEN    bearer token for auth (optional; if unset, auth is disabled)

Run:
  uvicorn mKBI_api:app --host 0.0.0.0 --port 8000
"""

import os
import time
import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Depends, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field

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
MKBI_TOKEN  = os.getenv("MKBI_TOKEN", "")


# ---------------------------------------------------------------------------
# Service lifecycle
# ---------------------------------------------------------------------------

_service: Optional[LLMService] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _service
    log.info("Loading LLMService from config: %s", MKBI_CONFIG)
    _service = LLMService(config_path=MKBI_CONFIG)
    log.info("LLMService ready. Model: %s | Default skill: %s", _service.model, _service.default_skill)
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
        return
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


class SkillInfo(BaseModel):
    name: str
    executor: str
    analysis: Optional[str]


class HealthResponse(BaseModel):
    status: str
    model: str
    default_skill: str
    uptime_seconds: float


class ExecuteResponse(BaseModel):
    skill: str
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


class ReloadResponse(BaseModel):
    skills: list[SkillInfo]


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="mKBI API",
    description="HTTP interface for the mini Kernel Bound Intelligence agent.",
    version="2.0.0",
    lifespan=lifespan,
)

_start_time = time.monotonic()


# ---------------------------------------------------------------------------
# Meta endpoints
# ---------------------------------------------------------------------------

@app.get("/health", response_model=HealthResponse, tags=["meta"])
def health(svc: LLMService = Depends(get_service)):
    return HealthResponse(
        status="ok",
        model=svc.model,
        default_skill=svc.default_skill,
        uptime_seconds=round(time.monotonic() - _start_time, 2),
    )


@app.get("/skills", response_model=list[SkillInfo], tags=["meta"])
def list_skills(svc: LLMService = Depends(get_service)):
    """List all registered skills and their executor info."""
    return svc.list_skills()


@app.post("/skills/reload", response_model=ReloadResponse, tags=["meta"],
          dependencies=[Depends(check_auth)])
def reload_skills(svc: LLMService = Depends(get_service)):
    """Rescan the skills directory and reload all skill definitions."""
    skills = svc.reload_skills()
    log.info("Skills reloaded: %s", [s["name"] for s in skills])
    return ReloadResponse(skills=skills)


# ---------------------------------------------------------------------------
# Skill-scoped endpoints
# ---------------------------------------------------------------------------

def _do_execute(body: ExecuteRequest, svc: LLMService, skill: str):
    log.info("execute [%s]: %s", skill, body.request[:120])
    t0 = time.monotonic()

    if skill not in {s["name"] for s in svc.list_skills()}:
        raise HTTPException(status_code=404, detail=f"Skill not found: {skill}")

    outcome = svc.execute_task(body.request, skill=skill, output_only=body.output_only)
    elapsed = round(time.monotonic() - t0, 3)

    if body.output_only:
        return OutputOnlyResponse(output=str(outcome), elapsed_seconds=elapsed)

    outcome["elapsed_seconds"] = elapsed
    return outcome


def _do_interpret(body: InterpretRequest, svc: LLMService, skill: str):
    log.info("interpret [%s]: %s", skill, body.request[:120])
    t0 = time.monotonic()

    if skill not in {s["name"] for s in svc.list_skills()}:
        raise HTTPException(status_code=404, detail=f"Skill not found: {skill}")

    response = svc.create_chat([{"role": "user", "content": body.request}], skill=skill)
    elapsed = round(time.monotonic() - t0, 3)
    return InterpretResponse(response=response, elapsed_seconds=elapsed)


@app.post("/skills/{skill}/execute", tags=["agent"], dependencies=[Depends(check_auth)])
def skill_execute(
    skill: str,
    body: ExecuteRequest,
    svc: LLMService = Depends(get_service),
):
    """Run the full chain for a named skill."""
    return _do_execute(body, svc, skill)


@app.post(
    "/skills/{skill}/interpret",
    response_model=InterpretResponse,
    tags=["agent"],
    dependencies=[Depends(check_auth)],
)
def skill_interpret(
    skill: str,
    body: InterpretRequest,
    svc: LLMService = Depends(get_service),
):
    """Run the interpreter stage only for a named skill."""
    return _do_interpret(body, svc, skill)


# ---------------------------------------------------------------------------
# Backward-compatible endpoints (route to default_skill)
# ---------------------------------------------------------------------------

@app.post("/execute", tags=["agent"], dependencies=[Depends(check_auth)])
def execute(
    body: ExecuteRequest,
    svc: LLMService = Depends(get_service),
):
    """Full chain using the configured default_skill."""
    return _do_execute(body, svc, svc.default_skill)


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
    """Interpreter only using the configured default_skill."""
    return _do_interpret(body, svc, svc.default_skill)


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
