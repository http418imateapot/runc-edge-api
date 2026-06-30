import os
from pathlib import Path

os.environ["API_KEY"] = "test-secret-key"
os.environ["BUNDLE_PATH"] = "/tmp/test-bundle"

from runc_edge_api import __version__
from runc_edge_api.api import app


def test_version_matches_version_file():
    version_file = Path(__file__).resolve().parent.parent / "VERSION"
    assert __version__ == version_file.read_text(encoding="utf-8").strip()


def test_fastapi_app_uses_package_version():
    assert app.version == __version__
