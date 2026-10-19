"""Live capability check for the Codex app-server adapter.

The service talks to the app-server control socket. Where that daemon is
unavailable on a host, this tool starts a stdio app-server and bridges one
connection to a temporary unix socket, so the *unmodified* adapter is exercised
against real provider data.

Run from the repository root with the package importable:

    python3 tools/codex_live_check.py

It prints the normalized observation only: no raw provider payload and no
credentials. It exits non-zero when the adapter cannot collect.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading

from ai_monitor.config import Config
from ai_monitor.models import get_source_def, normalize_observation
from ai_monitor.pace import PaceSettings, enrich_observation
from ai_monitor.sources.codex import CodexAdapter


def _pump(src_fd: int, dst_fd: int) -> None:
    try:
        while True:
            chunk = os.read(src_fd, 65536)
            if not chunk:
                break
            os.write(dst_fd, chunk)
    except OSError:
        pass


def _bridge_once(server: socket.socket, codex_bin: str) -> None:
    """Accept one client and relay it to a fresh stdio app-server."""
    conn, _ = server.accept()
    with conn:
        proc = subprocess.Popen(
            [codex_bin, "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        try:
            to_child = threading.Thread(
                target=_pump, args=(conn.fileno(), proc.stdin.fileno()), daemon=True
            )
            from_child = threading.Thread(
                target=_pump, args=(proc.stdout.fileno(), conn.fileno()), daemon=True
            )
            to_child.start()
            from_child.start()
            to_child.join(timeout=30)
            from_child.join(timeout=5)
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


def main() -> int:
    codex_bin = shutil.which("codex")
    if codex_bin is None:
        print("SKIP: codex CLI not found on PATH")
        return 0

    workdir = tempfile.mkdtemp(prefix="ai-monitor-codex-live-")
    sock_path = os.path.join(workdir, "codex-live.sock")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(sock_path)
    server.listen(1)

    bridge = threading.Thread(
        target=_bridge_once, args=(server, codex_bin), daemon=True
    )
    bridge.start()

    config = Config(
        data_dir=os.path.join(workdir, "data"),
        codex_app_server_socket=sock_path,
        codex_timeout_seconds=20,
    )
    try:
        raw = CodexAdapter().fetch(config)
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL: {type(exc).__name__}: {exc}")
        print(f"category: {getattr(exc, 'category', None)}")
        return 1
    finally:
        server.close()

    source = get_source_def("codex")
    raw = enrich_observation(source, raw, PaceSettings(), {}, None, {})
    observation = normalize_observation(source, raw)
    print(f"status: {raw.get('status')}")
    print(f"view: {raw.get('notes', {}).get('view')}")
    print(f"accountId: {observation.account_id}")
    print(f"windows: {len(observation.quota_windows)}")
    for window in observation.quota_windows:
        print(
            "  "
            + json.dumps(
                {
                    "id": window.id,
                    "label": window.label,
                    "usedPercent": window.used_percent,
                    "durationMinutes": window.duration_minutes,
                    "resetsAt": window.resets_at,
                    "pace": window.pace,
                },
                sort_keys=True,
            )
        )
    print("live codex check complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
