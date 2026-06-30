"""
OT Container Management API (v2.0)

RESTful API for managing Linux native containers (runc) on OT edge devices.
All /api/* endpoints require an X-API-Key header.

Environment variables:
    API_KEY           (required) API authentication key
    BUNDLE_PATH       (optional, default ".") runc bundle directory containing config.json
    RUNC_TIMEOUT      (optional, default "30") seconds before a runc command times out
    STOP_GRACE_PERIOD (optional, default "10") seconds to wait for SIGTERM before SIGKILL
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Security
from fastapi.security.api_key import APIKeyHeader
from pydantic import BaseModel

from . import __version__

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration from environment
# ---------------------------------------------------------------------------
API_KEY = os.environ.get("API_KEY", "")
BUNDLE_PATH = os.environ.get("BUNDLE_PATH", ".")
RUNC_TIMEOUT = int(os.environ.get("RUNC_TIMEOUT", "30"))
STOP_GRACE_PERIOD = int(os.environ.get("STOP_GRACE_PERIOD", "10"))

# ---------------------------------------------------------------------------
# API Key authentication
# ---------------------------------------------------------------------------
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def verify_api_key(key: Optional[str] = Security(_api_key_header)) -> str:
    if not API_KEY:
        logger.critical("API_KEY environment variable is not set -- all requests rejected.")
        raise HTTPException(
            status_code=503,
            detail="Server misconfiguration: API_KEY is not configured",
        )
    if key != API_KEY:
        raise HTTPException(status_code=403, detail="Invalid or missing API key")
    return key


# ---------------------------------------------------------------------------
# Container ID validation (whitelist pattern -- prevents path traversal)
# ---------------------------------------------------------------------------
_CONTAINER_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$")


def _validate_container_id(container_id: str) -> str:
    if not _CONTAINER_ID_RE.match(container_id):
        raise HTTPException(
            status_code=400,
            detail=(
                "Invalid container ID. Must start with an alphanumeric character, "
                "contain only [a-zA-Z0-9_.-], and be 1-64 characters long."
            ),
        )
    return container_id


# ---------------------------------------------------------------------------
# runc helper -- runs in a thread pool so the event loop is not blocked
# ---------------------------------------------------------------------------
async def _run_runc(*args: str, timeout: int = RUNC_TIMEOUT) -> str:
    """Execute a runc sub-command and return its stdout.

    Raises:
        HTTPException(504)              on subprocess timeout
        subprocess.CalledProcessError   on non-zero exit code (caller decides HTTP status)
    """

    def _blocking_run() -> subprocess.CompletedProcess:
        return subprocess.run(
            ["runc", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    try:
        result: subprocess.CompletedProcess = await asyncio.to_thread(_blocking_run)
    except subprocess.TimeoutExpired:
        raise HTTPException(
            status_code=504,
            detail=f"runc operation timed out after {timeout}s",
        )

    if result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, "runc", output=result.stdout, stderr=result.stderr
        )

    return result.stdout


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------
app = FastAPI(
    title="OT Container Management API",
    description=(
        "RESTful API for managing Linux native containers (runc) on OT edge devices. "
        "All /api/* endpoints require the **X-API-Key** header."
    ),
    version=__version__,
)


# ---------------------------------------------------------------------------
# Request/Response models
# ---------------------------------------------------------------------------
class ContainerAction(BaseModel):
    container_id: str


class StopAction(BaseModel):
    container_id: str
    grace_period: int = STOP_GRACE_PERIOD


class ResourceUpdate(BaseModel):
    cpu_shares: Optional[int] = None    # relative CPU weight, e.g. 512 or 1024
    memory_limit: Optional[int] = None  # bytes, e.g. 134217728 for 128 MiB


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.get("/health", summary="Health Check", tags=["Operations"])
async def health() -> dict:
    """Return service liveness status. No authentication required."""
    return {"status": "ok"}


@app.post("/api/containers/start", summary="Start Container", tags=["Containers"])
async def start_container(
    action: ContainerAction,
    _: str = Depends(verify_api_key),
) -> dict:
    """Create and start the specified container.

    Uses ``runc create`` followed by ``runc start`` so the API call returns
    as soon as the container's init process is running (non-blocking).
    """
    container_id = _validate_container_id(action.container_id)
    logger.info("Starting container: %s", container_id)
    try:
        await _run_runc("create", "--bundle", BUNDLE_PATH, container_id)
        await _run_runc("start", container_id)
    except subprocess.CalledProcessError as e:
        logger.error("Failed to start container %s: %s", container_id, e.stderr)
        if e.stderr and "already exists" in e.stderr:
            raise HTTPException(
                status_code=409,
                detail=f"Container '{container_id}' already exists",
            )
        raise HTTPException(
            status_code=500,
            detail=f"Failed to start container: {e.stderr}",
        )
    logger.info("Container %s started", container_id)
    return {"message": f"Container '{container_id}' started successfully"}


@app.post("/api/containers/stop", summary="Stop Container", tags=["Containers"])
async def stop_container(
    action: StopAction,
    _: str = Depends(verify_api_key),
) -> dict:
    """Gracefully stop and delete the specified container.

    Sends SIGTERM and waits up to ``grace_period`` seconds for the process to
    exit before sending SIGKILL. Deletes the container state afterwards.
    """
    container_id = _validate_container_id(action.container_id)
    logger.info("Stopping container %s (grace_period=%ds)", container_id, action.grace_period)

    # Step 1: Request graceful shutdown
    try:
        await _run_runc("kill", container_id, "SIGTERM", timeout=5)
    except (subprocess.CalledProcessError, HTTPException):
        pass  # Already stopped or not running -- proceed to cleanup

    # Step 2: Poll until stopped or grace period expires
    for _ in range(action.grace_period):
        await asyncio.sleep(1)
        try:
            state_raw = await _run_runc("state", container_id, timeout=5)
            state = json.loads(state_raw)
            if state.get("status") == "stopped":
                break
        except (subprocess.CalledProcessError, HTTPException, json.JSONDecodeError):
            break  # Container gone or state unreachable -- proceed

    # Step 3: Force kill if still running
    try:
        await _run_runc("kill", container_id, "SIGKILL", timeout=5)
    except (subprocess.CalledProcessError, HTTPException):
        pass

    # Step 4: Remove container state
    try:
        await _run_runc("delete", container_id, timeout=10)
    except subprocess.CalledProcessError as e:
        logger.error("Failed to delete container %s: %s", container_id, e.stderr)
        if e.stderr and "does not exist" in e.stderr:
            raise HTTPException(
                status_code=404,
                detail=f"Container '{container_id}' not found",
            )
        raise HTTPException(
            status_code=500,
            detail=f"Failed to delete container: {e.stderr}",
        )

    logger.info("Container %s stopped", container_id)
    return {"message": f"Container '{container_id}' stopped successfully"}


@app.patch(
    "/api/containers/{container_id}/resources",
    summary="Update Container Resources",
    tags=["Containers"],
)
async def update_resources(
    container_id: str,
    update: ResourceUpdate,
    _: str = Depends(verify_api_key),
) -> dict:
    """Update runtime resource limits using ``runc update``.

    Changes take effect immediately without restarting the container.
    """
    container_id = _validate_container_id(container_id)

    runc_args: list[str] = []
    updated: dict = {}

    if update.cpu_shares is not None:
        runc_args += ["--cpu-shares", str(update.cpu_shares)]
        updated["cpu_shares"] = update.cpu_shares

    if update.memory_limit is not None:
        runc_args += ["--memory", str(update.memory_limit)]
        updated["memory_limit"] = update.memory_limit

    if not runc_args:
        raise HTTPException(
            status_code=400,
            detail="No resource fields specified. Provide cpu_shares and/or memory_limit.",
        )

    logger.info("Updating resources for container %s: %s", container_id, updated)
    try:
        await _run_runc("update", container_id, *runc_args)
    except subprocess.CalledProcessError as e:
        logger.error("Failed to update resources for %s: %s", container_id, e.stderr)
        if e.stderr and "does not exist" in e.stderr:
            raise HTTPException(
                status_code=404,
                detail=f"Container '{container_id}' not found",
            )
        raise HTTPException(
            status_code=500,
            detail=f"Failed to update container resources: {e.stderr}",
        )

    logger.info("Resources updated for container %s", container_id)
    return {
        "message": f"Container '{container_id}' resource settings updated",
        "updated": updated,
    }


@app.get("/api/containers", summary="List All Containers", tags=["Containers"])
async def get_all_containers(_: str = Depends(verify_api_key)) -> dict:
    """Retrieve a JSON list of all containers known to runc."""
    logger.info("Listing all containers")
    try:
        output = await _run_runc("list", "--format", "json")
    except subprocess.CalledProcessError as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to list containers: {e.stderr}",
        )
    containers = json.loads(output) if output.strip() else []
    return {"containers": containers}


@app.get("/api/containers/{container_id}", summary="Get Container State", tags=["Containers"])
async def get_container_by_id(
    container_id: str,
    _: str = Depends(verify_api_key),
) -> dict:
    """Retrieve the current state of the specified container."""
    container_id = _validate_container_id(container_id)
    logger.info("Getting state for container: %s", container_id)
    try:
        output = await _run_runc("state", container_id)
    except subprocess.CalledProcessError as e:
        if e.stderr and "does not exist" in e.stderr:
            raise HTTPException(
                status_code=404,
                detail=f"Container '{container_id}' not found",
            )
        raise HTTPException(
            status_code=500,
            detail=f"Failed to get container state: {e.stderr}",
        )
    return {"state": json.loads(output)}
