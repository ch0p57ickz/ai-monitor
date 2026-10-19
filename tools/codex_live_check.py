"""Live capability check for the Codex app-server adapter.

Runs the *unmodified* adapter with the stdio transport: it spawns
``codex app-server --listen stdio://`` and reads the real account rate limits.
No daemon is involved, so this works on hosts where the managed daemon cannot
start.

Run from the repository root with the package importable:

    python3 tools/codex_live_check.py

It prints the normalized observation only: no raw provider payload and no
credentials. It exits non-zero when the adapter cannot collect.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

from ai_monitor.config import Config
from ai_monitor.models import get_source_def, normalize_observation
from ai_monitor.pace import PaceSettings, enrich_observation
from ai_monitor.sources.codex import CodexAdapter


def main() -> int:
    codex_bin = shutil.which("codex")
    if codex_bin is None:
        print("SKIP: codex CLI not found on PATH")
        return 0

    workdir = tempfile.mkdtemp(prefix="ai-monitor-codex-live-")
    adapter = CodexAdapter()
    config = Config(
        data_dir=os.path.join(workdir, "data"),
        codex_transport="stdio",
        codex_bin=codex_bin,
        codex_timeout_seconds=20,
    )
    try:
        raw = adapter.fetch(config)
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL: {type(exc).__name__}: {exc}")
        print(f"category: {getattr(exc, 'category', None)}")
        return 1
    finally:
        adapter.close()

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
