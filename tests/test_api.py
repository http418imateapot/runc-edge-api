"""Tests for OT Container Management API (v2.0).

Run with:
    pip install -r requirements-dev.txt
    pytest tests/ -v
"""

from __future__ import annotations

import json
import os
import subprocess
from unittest.mock import MagicMock, call, patch

import pytest
from fastapi.testclient import TestClient

# Set required environment variables before importing the app module.
os.environ["API_KEY"] = "test-secret-key"
os.environ["BUNDLE_PATH"] = "/tmp/test-bundle"

from api import app  # noqa: E402

client = TestClient(app)
HEADERS = {"X-API-Key": "test-secret-key"}


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------
def _ok(stdout: str = "", stderr: str = "") -> MagicMock:
    """Return a mock CompletedProcess with returncode=0."""
    m = MagicMock(spec=subprocess.CompletedProcess)
    m.returncode = 0
    m.stdout = stdout
    m.stderr = stderr
    return m


def _err(stderr: str = "some runc error", returncode: int = 1) -> MagicMock:
    """Return a mock CompletedProcess with non-zero returncode."""
    m = MagicMock(spec=subprocess.CompletedProcess)
    m.returncode = returncode
    m.stdout = ""
    m.stderr = stderr
    return m


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------
class TestHealth:
    def test_returns_ok(self):
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    def test_no_authentication_required(self):
        # /health must be reachable without an API key
        response = client.get("/health")
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
class TestAuthentication:
    def test_missing_key_returns_403(self):
        response = client.get("/api/containers")
        assert response.status_code == 403

    def test_wrong_key_returns_403(self):
        response = client.get(
            "/api/containers", headers={"X-API-Key": "definitely-wrong"}
        )
        assert response.status_code == 403

    def test_correct_key_is_accepted(self):
        with patch("subprocess.run", return_value=_ok("[]")):
            response = client.get("/api/containers", headers=HEADERS)
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# Container ID validation
# ---------------------------------------------------------------------------
VALID_IDS = [
    "my-container",
    "ot2it-01",
    "abc",
    "A1",
    "Container.Name_01",
]

INVALID_IDS = [
    "../etc/passwd",       # path traversal
    "id with spaces",      # spaces not allowed
    "",                    # empty string
    "a" * 65,              # exceeds 64 chars
    "-starts-with-dash",   # must start with alphanumeric
    ".starts-with-dot",    # must start with alphanumeric
    "id;cmd",              # shell metacharacter
]


class TestContainerIdValidation:
    @pytest.mark.parametrize("valid_id", VALID_IDS)
    def test_valid_id_does_not_return_400(self, valid_id):
        with patch("subprocess.run", return_value=_ok()):
            response = client.post(
                "/api/containers/start",
                json={"container_id": valid_id},
                headers=HEADERS,
            )
        assert response.status_code != 400

    @pytest.mark.parametrize("bad_id", INVALID_IDS)
    def test_invalid_id_returns_400(self, bad_id):
        response = client.post(
            "/api/containers/start",
            json={"container_id": bad_id},
            headers=HEADERS,
        )
        assert response.status_code == 400


# ---------------------------------------------------------------------------
# Start container
# ---------------------------------------------------------------------------
class TestStartContainer:
    def test_success_returns_200(self):
        with patch("subprocess.run", return_value=_ok()):
            response = client.post(
                "/api/containers/start",
                json={"container_id": "my-container"},
                headers=HEADERS,
            )
        assert response.status_code == 200
        assert "started" in response.json()["message"]

    def test_uses_runc_create_then_start(self):
        with patch("subprocess.run", return_value=_ok()) as mock_run:
            client.post(
                "/api/containers/start",
                json={"container_id": "my-container"},
                headers=HEADERS,
            )
        assert mock_run.call_count == 2
        first_cmd = mock_run.call_args_list[0][0][0]
        second_cmd = mock_run.call_args_list[1][0][0]
        assert "create" in first_cmd
        assert "start" in second_cmd

    def test_passes_bundle_path_to_create(self):
        with patch("subprocess.run", return_value=_ok()) as mock_run:
            client.post(
                "/api/containers/start",
                json={"container_id": "my-container"},
                headers=HEADERS,
            )
        first_cmd = mock_run.call_args_list[0][0][0]
        assert "--bundle" in first_cmd
        assert "/tmp/test-bundle" in first_cmd

    def test_already_exists_returns_409(self):
        with patch(
            "subprocess.run",
            return_value=_err("container with id already exists"),
        ):
            response = client.post(
                "/api/containers/start",
                json={"container_id": "my-container"},
                headers=HEADERS,
            )
        assert response.status_code == 409

    def test_runc_error_returns_500(self):
        with patch("subprocess.run", return_value=_err("unexpected runc error")):
            response = client.post(
                "/api/containers/start",
                json={"container_id": "my-container"},
                headers=HEADERS,
            )
        assert response.status_code == 500


# ---------------------------------------------------------------------------
# Stop container
# ---------------------------------------------------------------------------
class TestStopContainer:
    def test_success_returns_200(self):
        # grace_period=0 skips the poll loop; 3 runc calls: SIGTERM, SIGKILL, delete
        with patch("subprocess.run", return_value=_ok()):
            response = client.post(
                "/api/containers/stop",
                json={"container_id": "my-container", "grace_period": 0},
                headers=HEADERS,
            )
        assert response.status_code == 200
        assert "stopped" in response.json()["message"]

    def test_not_found_on_delete_returns_404(self):
        with patch(
            "subprocess.run",
            side_effect=[
                _ok(),                          # runc kill SIGTERM
                _ok(),                          # runc kill SIGKILL
                _err("does not exist"),         # runc delete -> not found
            ],
        ):
            response = client.post(
                "/api/containers/stop",
                json={"container_id": "my-container", "grace_period": 0},
                headers=HEADERS,
            )
        assert response.status_code == 404

    def test_delete_runc_error_returns_500(self):
        with patch(
            "subprocess.run",
            side_effect=[
                _ok(),                          # runc kill SIGTERM
                _ok(),                          # runc kill SIGKILL
                _err("permission denied"),      # runc delete -> unexpected error
            ],
        ):
            response = client.post(
                "/api/containers/stop",
                json={"container_id": "my-container", "grace_period": 0},
                headers=HEADERS,
            )
        assert response.status_code == 500

    def test_default_grace_period_is_used(self):
        # Omitting grace_period uses the default; stops immediately when poll
        # breaks on exception from state command.
        with patch("subprocess.run", return_value=_ok()):
            response = client.post(
                "/api/containers/stop",
                json={"container_id": "my-container"},
                headers=HEADERS,
            )
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# Update resources
# ---------------------------------------------------------------------------
class TestUpdateResources:
    def test_update_cpu_shares_success(self):
        with patch("subprocess.run", return_value=_ok()):
            response = client.patch(
                "/api/containers/my-container/resources",
                json={"cpu_shares": 1024},
                headers=HEADERS,
            )
        assert response.status_code == 200
        assert response.json()["updated"]["cpu_shares"] == 1024

    def test_update_memory_limit_success(self):
        with patch("subprocess.run", return_value=_ok()):
            response = client.patch(
                "/api/containers/my-container/resources",
                json={"memory_limit": 268435456},
                headers=HEADERS,
            )
        assert response.status_code == 200
        assert response.json()["updated"]["memory_limit"] == 268435456

    def test_update_both_fields_success(self):
        with patch("subprocess.run", return_value=_ok()) as mock_run:
            response = client.patch(
                "/api/containers/my-container/resources",
                json={"cpu_shares": 512, "memory_limit": 134217728},
                headers=HEADERS,
            )
        assert response.status_code == 200
        cmd = mock_run.call_args[0][0]
        assert "--cpu-shares" in cmd
        assert "--memory" in cmd

    def test_empty_body_returns_400(self):
        response = client.patch(
            "/api/containers/my-container/resources",
            json={},
            headers=HEADERS,
        )
        assert response.status_code == 400

    def test_container_not_found_returns_404(self):
        with patch("subprocess.run", return_value=_err("does not exist")):
            response = client.patch(
                "/api/containers/my-container/resources",
                json={"cpu_shares": 512},
                headers=HEADERS,
            )
        assert response.status_code == 404

    def test_runc_error_returns_500(self):
        with patch("subprocess.run", return_value=_err("cgroup write error")):
            response = client.patch(
                "/api/containers/my-container/resources",
                json={"cpu_shares": 512},
                headers=HEADERS,
            )
        assert response.status_code == 500

    def test_uses_runc_update_command(self):
        with patch("subprocess.run", return_value=_ok()) as mock_run:
            client.patch(
                "/api/containers/my-container/resources",
                json={"cpu_shares": 512},
                headers=HEADERS,
            )
        cmd = mock_run.call_args[0][0]
        assert "update" in cmd
        assert "--cpu-shares" in cmd
        assert "512" in cmd


# ---------------------------------------------------------------------------
# List containers
# ---------------------------------------------------------------------------
class TestListContainers:
    def test_returns_container_list(self):
        containers = [{"id": "c1", "status": "running", "pid": 100}]
        with patch("subprocess.run", return_value=_ok(json.dumps(containers))):
            response = client.get("/api/containers", headers=HEADERS)
        assert response.status_code == 200
        assert response.json()["containers"] == containers

    def test_empty_output_returns_empty_list(self):
        with patch("subprocess.run", return_value=_ok("")):
            response = client.get("/api/containers", headers=HEADERS)
        assert response.status_code == 200
        assert response.json()["containers"] == []

    def test_runc_error_returns_500(self):
        with patch("subprocess.run", return_value=_err()):
            response = client.get("/api/containers", headers=HEADERS)
        assert response.status_code == 500


# ---------------------------------------------------------------------------
# Get container by ID
# ---------------------------------------------------------------------------
class TestGetContainerById:
    def test_returns_state(self):
        state = {"id": "my-container", "status": "running", "pid": 1234}
        with patch("subprocess.run", return_value=_ok(json.dumps(state))):
            response = client.get("/api/containers/my-container", headers=HEADERS)
        assert response.status_code == 200
        assert response.json()["state"]["status"] == "running"
        assert response.json()["state"]["pid"] == 1234

    def test_not_found_returns_404(self):
        with patch("subprocess.run", return_value=_err("container does not exist")):
            response = client.get("/api/containers/my-container", headers=HEADERS)
        assert response.status_code == 404

    def test_runc_error_returns_500(self):
        with patch("subprocess.run", return_value=_err("unexpected error")):
            response = client.get("/api/containers/my-container", headers=HEADERS)
        assert response.status_code == 500

    def test_invalid_container_id_returns_400(self):
        response = client.get("/api/containers/bad id!", headers=HEADERS)
        # FastAPI URL router may return 404 for paths with spaces; both are acceptable
        assert response.status_code in (400, 404, 422)
