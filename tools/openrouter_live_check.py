"""Live capability check for the two OpenRouter streams (task 5.1 evidence).

Runs the *unmodified* adapters against the real OpenRouter API and reports, per
stream, whether it collected and which error category it produced. This is what
verifies the "partial OpenRouter results" requirement: the account-credit stream
needs a management/provisioning key for ``/api/v1/credits``, so an inference key
can succeed on the current-key stream while the account stream fails with
``authorization``, and each stream keeps its own health.

Credentials are read from the environment only and are never printed:

- ``OPENROUTER_MANAGEMENT_KEY`` is preferred for the credit balance;
- ``OPENROUTER_API_KEY`` feeds the current-key stream, and is used as the
  credit-balance fallback when no management key is set.

Run it in a shell where the variables are exported, so the values never pass
through an argument list:

    set -a; . ./.env; set +a
    PYTHONPATH=. python3 tools/openrouter_live_check.py

Exit status: 0 when a stream collected and nothing leaked; 2 when no credential
is present at all (a configuration problem, not a code failure); 1 otherwise.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from typing import Any

from ai_monitor.config import Config
from ai_monitor.models import get_source_def, normalize_observation
from ai_monitor.pace import PaceSettings, enrich_observation
from ai_monitor.sources.registry import CollectionError

STREAMS = ("openrouter-account", "openrouter-key")


def leaked(secret: str, *payloads: Any) -> bool:
    """Return whether ``secret`` appears in any of the serialized payloads."""
    if not secret:
        return False
    for payload in payloads:
        try:
            text = json.dumps(payload, default=str)
        except (TypeError, ValueError):
            text = str(payload)
        if secret in text:
            return True
    return False


def _redact(value: Any, secret: str) -> Any:
    """Replace the secret with a placeholder, recursively."""
    if isinstance(value, str):
        return value.replace(secret, "<redacted>") if secret else value
    if isinstance(value, dict):
        return {key: _redact(item, secret) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(item, secret) for item in value]
    return value


def main() -> int:
    management = os.environ.get("OPENROUTER_MANAGEMENT_KEY", "")
    inference = os.environ.get("OPENROUTER_API_KEY", "")
    if not management and not inference:
        print(
            "SKIP: neither OPENROUTER_MANAGEMENT_KEY nor OPENROUTER_API_KEY is set.\n"
            "Export one (for example `set -a; . ./.env; set +a`) and re-run."
        )
        return 2

    secret = management or inference
    config = Config(
        data_dir=tempfile.mkdtemp(prefix="ai-monitor-openrouter-check-"),
        openrouter_api_key=inference,
        openrouter_management_key=management,
        openrouter_timeout_seconds=20,
    )

    from ai_monitor.sources.registry import default_registry

    registry = default_registry()
    outcomes: dict[str, dict[str, Any]] = {}
    any_leak = False

    for source_id in STREAMS:
        adapter = registry[source_id]()
        record: dict[str, Any] = {"source": source_id}
        try:
            raw = adapter.fetch(config)
        except CollectionError as exc:
            record["status"] = "failed"
            record["errorCategory"] = exc.category
            record["error"] = _redact(str(exc), secret)
        except Exception as exc:  # noqa: BLE001
            record["status"] = "failed"
            record["errorCategory"] = "unexpected"
            record["error"] = _redact(str(exc), secret)
        else:
            source = get_source_def(source_id)
            raw = enrich_observation(source, raw, PaceSettings(), {}, None, {})
            observation = normalize_observation(source, raw)
            serialized = observation.to_dict()
            record["status"] = raw.get("status")
            record["budgets"] = [
                {
                    "scope": window.scope,
                    "totalCreditsUsd": window.total_credits_usd,
                    "totalUsageUsd": window.total_usage_usd,
                    "remainingUsd": window.remaining_usd,
                    "limitUsd": window.limit_usd,
                    "usedUsd": window.used_usd,
                    "resetsAt": window.resets_at,
                    "pace": window.pace,
                }
                for window in observation.budgets
            ]
            # A secret must never reach a stored or returned payload.
            if leaked(secret, serialized, raw):
                any_leak = True
                record["leakedSecret"] = True
        finally:
            adapter.close()  # nothing to release today, but keep the contract
        outcomes[source_id] = record

    ok = [sid for sid, rec in outcomes.items() if rec["status"] == "success"]
    failed = [sid for sid, rec in outcomes.items() if rec["status"] != "success"]

    print(json.dumps({"streams": outcomes}, indent=2, sort_keys=True))
    print()
    print(f"collected: {ok or 'none'}")
    print(f"failed:    {failed or 'none'}")
    if ok and failed:
        print("partial: one stream collected while the other failed (expected for "
              "an inference key against the credit balance)")
    if not management and inference:
        print(
            "note: no management key was set, so the account-credit stream falls "
            "back to the inference key and may report `authorization`"
        )
    if any_leak:
        print()
        print("FAIL: a credential appeared in an observation payload")
        return 1
    if not ok:
        print()
        print("FAIL: no stream collected")
        return 1
    print()
    print("live openrouter check complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
