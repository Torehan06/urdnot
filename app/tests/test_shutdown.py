"""Exercise the real entry point, request logging, and SIGTERM drain."""

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen


def test_sigterm_drains_request_and_exits_zero(tmp_path):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    env = dict(os.environ, HOST="127.0.0.1", PORT=str(port), LOG_LEVEL="info")
    env.pop("DATABASE_URL", None)
    env.pop("REDIS_URL", None)
    base = f"http://127.0.0.1:{port}"
    log_path = tmp_path / "service.log"
    with log_path.open("w") as output:
        process = subprocess.Popen(
            [sys.executable, "-m", "app"],
            cwd=Path(__file__).resolve().parents[2], env=env,
            stdout=output, stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 15
            while True:
                assert process.poll() is None, log_path.read_text()
                try:
                    with urlopen(base + "/healthz", timeout=0.2) as response:
                        assert response.status == 200
                    break
                except (URLError, TimeoutError):
                    assert time.monotonic() < deadline, log_path.read_text()
                    time.sleep(0.05)

            def charge():
                with urlopen(base + "/api/charge?ms=800", timeout=15) as response:
                    assert response.status == 200
                    return json.load(response)

            with ThreadPoolExecutor(max_workers=1) as pool:
                result = pool.submit(charge)
                # Give the local request time to enter its long CPU-bound handler.
                time.sleep(0.2)
                assert not result.done()
                process.send_signal(signal.SIGTERM)
                assert result.result(timeout=15)["cpu_ms"] >= 800
            assert process.wait(timeout=15) == 0
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)

    entries = [json.loads(line) for line in log_path.read_text().splitlines()]
    startup = next(entry for entry in entries if entry["event"] == "startup")
    assert startup["storage_mode"] == "in-memory"
    assert startup["counter_mode"] == "in-process"
    requests = [entry for entry in entries if entry["event"] == "request"]
    assert [entry["path"] for entry in requests] == ["/healthz", "/api/charge"]
    assert all(entry["status"] == 200 for entry in requests)
    assert requests[1]["duration_ms"] >= 750
