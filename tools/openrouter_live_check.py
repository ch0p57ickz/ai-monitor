"""Live capability check for the two OpenRouter streams (task 5.1 evidence).

Runs the *unmodified* adapters against the real OpenRouter API and reports, per
stream, whether it collected and which error category it produced. This is what
verifies the "partial OpenRouter results" requirement: the account-credit stream
needs a management/provisioning key for ``/api/v1/credits``, so an inference key
can succeed on the current-key stream while the account stream fails with
``authorization``, and each stream keeps its own health.

Credentials are read from the environment only and are never printed:

- ``OPENROUTER_MGMT_KEY`` or ``OPENROUTER_MANAGEMENT_KEY`` feeds the credit
  balance (both names are accepted; the short one is what the local ``.env``
  uses);
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
import shutil
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


def _management_key_from_env() -> str:
    """Read the credit-balance key, accepting both documented spellings."""
    for name in ("OPENROUTER_MGMT_KEY", "OPENROUTER_MANAGEMENT_KEY"):
        value = os.environ.get(name, "")
        if value:
            return value
    return ""


def main() -> int:
    management = _management_key_from_env()
    inference = os.environ.get("OPENROUTER_API_KEY", "")
    if not management and not inference:
        print(
            "SKIP: none of OPENROUTER_MGMT_KEY, OPENROUTER_MANAGEMENT_KEY or\n"
            "OPENROUTER_API_KEY is set. Export one (for example\n"
            "`set -a; . ./.env; set +a`) and re-run."
        )
        return 2

    secret = management or inference
    workdir = tempfile.mkdtemp(prefix="ai-monitor-openrouter-check-")
    config = Config(
        data_dir=workdir,
        openrouter_api_key=inference,
        openrouter_management_key=management,
        openrouter_timeout_seconds=20,
    )

    from ai_monitor.sources.registry import default_registry

    registry = default_registry()
    outcomes: dict[str, dict[str, Any]] = {}
    any_leak = False

    try:
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
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    ok = [sid for sid, rec in outcomes.items() if rec["status"] == "success"]
    failed = [sid for sid, rec in outcomes.items() if rec["status"] != "success"]

    # Redacted before printing: when ``leaked`` fires, the offending payload is
    # exactly what must not reach stdout, and this dump happens before the FAIL
    # line below.
    print(json.dumps(_redact({"streams": outcomes}, secret), indent=2, sort_keys=True))
    print()
    print(f"collected: {ok or 'none'}")
    print(f"failed:    {failed or 'none'}")
    for note in _explain(outcomes, management=bool(management), inference=bool(inference)):
        print(note)
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


def _explain(
    outcomes: dict[str, dict[str, Any]], *, management: bool, inference: bool
) -> list[str]:
    """Explain an asymmetric result from what was observed, not from a template.

    The two streams need different credentials — account credits need a
    management/provisioning key, current-key usage needs its own key — so the
    interesting cases are the ones where exactly one is configured. Saying which
    one is missing is the whole value of the check.
    """
    notes: list[str] = []
    account = outcomes.get("openrouter-account", {})
    key = outcomes.get("openrouter-key", {})
    account_ok = account.get("status") == "success"
    key_ok = key.get("status") == "success"

    if account_ok and not key_ok:
        notes.append(
            "partial: the account-credit stream collected while the current-key "
            "stream did not, and each keeps its own health"
        )
    elif key_ok and not account_ok:
        notes.append(
            "partial: the current-key stream collected while the account-credit "
            "stream did not, and each keeps its own health"
        )
    elif not account_ok and not key_ok:
        notes.append("neither stream collected; see the error categories above")

    if not inference and key.get("errorCategory") == "not_configured":
        notes.append(
            "note: the current-key stream needs its own key credential "
            "(OPENROUTER_API_KEY). A management key covers account credits only, "
            "so this stream reports `not_configured` until one is set — the spec "
            "expects a stream to be unavailable when the available credential "
            "cannot call it"
        )
    if not management and account.get("errorCategory") == "not_configured":
        notes.append(
            "note: the account-credit stream has no management/provisioning key "
            "(OPENROUTER_MGMT_KEY), so it falls back to the inference key and now "
            "has none either"
        )
    elif not management and account_ok:
        notes.append(
            "note: no management key was set, so the account-credit stream used "
            "the inference key; its balance is available but a stricter "
            "deployment would use a provisioning key"
        )
    elif not management and account.get("errorCategory") == "authorization":
        notes.append(
            "note: no management key was set, so the account-credit stream fell "
            "back to the inference key and was refused — that is the documented "
            "split where only the current-key stream succeeds"
        )
    return notes


if __name__ == "__main__":
    sys.exit(main())
