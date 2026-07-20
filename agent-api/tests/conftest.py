import os

# Must be set before any app.* import — config.py reads it at import time.
os.environ.setdefault("API_SECRET_KEY", "test-default-key")

import json

import pytest

from app.tenants import TenantRegistry


TENANTS_DOC = {
    "mcp_servers": {
        "wcc": {
            "url": "http://host.docker.internal:8765/mcp",
            "auth_token": "srv-token",
            "timeout_s": 60,
            "allow_private": True,
        }
    },
    "tenants": [
        {
            "name": "wcc-events",
            "api_key": "wcc-key",
            "origins": ["https://wcc-example-mcp.net"],
            "system_prompt": "WCC prompt. Current time: {current_time}",
            "local_skills": ["web_search"],
            "mcp_servers": ["wcc"],
            "max_tools": 2,
            "max_tokens": 1024,
        }
    ],
}


@pytest.fixture
def tenants_file(tmp_path):
    path = tmp_path / "tenants.json"
    path.write_text(json.dumps(TENANTS_DOC))
    return path


@pytest.fixture
def tenant_registry(tenants_file):
    return TenantRegistry(tenants_file, "test-default-key")
