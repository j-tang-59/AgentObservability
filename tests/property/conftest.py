from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def span_file(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("spans") / "spans.jsonl"


@pytest.fixture(scope="session")
def http_server(span_file: Path) -> Iterator[str]:
    port = _free_port()
    env = dict(os.environ)
    env["AGENTTRACE_TEST_SPAN_FILE"] = str(span_file)
    # Left at its default ("dev-secret"): HttpRunner in this test process
    # binds SHARED_SECRET at import time from the parent's own environment,
    # which we don't control retroactively, so both sides must agree on the
    # same default rather than one being overridden here.
    env["OTEL_SDK_DISABLED"] = "false"
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "agenttrace.harness.server:app", "--port", str(port), "--log-level", "warning"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(50):
            try:
                r = httpx.get(f"{base_url}/health", timeout=0.5)
                if r.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        else:
            raise RuntimeError("agenttrace HTTP worker did not start in time")
        yield base_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
