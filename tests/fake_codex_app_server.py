#!/usr/bin/env python3
"""A minimal stand-in for `codex app-server --listen stdio://`.

Used by the stdio-transport tests. It ignores the argv it is given (the real
command line is `app-server --listen stdio://`) and speaks the same
newline-delimited JSON-RPC protocol:

- `initialize` is answered, followed by a `account/updated` notification, so
  tests exercise the notification-skipping path.
- `account/rateLimits/read` is answered with the JSON object at
  `FAKE_PAYLOAD` (or a small default).

Behaviour is selected with environment variables:

- `FAKE_MODE=ok` (default): answer both requests.
- `FAKE_MODE=hang`: answer `initialize`, then never answer the rate-limit read.
- `FAKE_MODE=die`: exit immediately without answering anything.
- `FAKE_MODE=split`: deliver the rate-limit reply in two writes, to exercise
  the reader's line buffering.
- `FAKE_MARKER`: append one line to this file on every start, so a test can
  count how many child processes were spawned.
- `FAKE_HANG_MARKER`: append one line when a rate-limit read is left unanswered
  (used with `FAKE_MODE=hang`), so a test can wait for the client to be blocked
  in a read instead of sleeping.
- `FAKE_PAYLOAD`: path to a JSON file holding the rate-limit result.
"""

import json
import os
import sys
import time

DEFAULT_RESULT = {
    "rateLimits": {"primary": {"usedPercent": 10, "windowDurationMins": 300}}
}


def send(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def main():
    mode = os.environ.get("FAKE_MODE", "ok")
    marker = os.environ.get("FAKE_MARKER")
    if marker:
        with open(marker, "a", encoding="utf-8") as handle:
            handle.write("start\n")

    if mode == "die":
        return 3

    initialized = False
    payload_path = os.environ.get("FAKE_PAYLOAD")
    if payload_path:
        with open(payload_path, encoding="utf-8") as handle:
            result = json.load(handle)
    else:
        result = DEFAULT_RESULT

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = message.get("method")
        request_id = message.get("id")
        if method == "initialize":
            if initialized:
                # The real app-server rejects a second initialize on the same
                # connection; the adapter must not send one.
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32600, "message": "Already initialized"},
                    }
                )
                continue
            initialized = True
            send({"jsonrpc": "2.0", "id": request_id, "result": {}})
            send({"method": "account/updated", "params": {"authMode": "chatgpt"}})
        elif method == "account/rateLimits/read":
            if mode == "hang":
                # Record that the read request arrived and is about to be left
                # unanswered, so a test can wait for the client to be blocked in
                # its read instead of guessing with a sleep.
                hang_marker = os.environ.get("FAKE_HANG_MARKER")
                if hang_marker:
                    with open(hang_marker, "a", encoding="utf-8") as handle:
                        handle.write("read\n")
                time.sleep(60)
                continue
            encoded = json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result})
            if mode == "split":
                # Deliver the reply in two pieces to exercise buffering.
                half = len(encoded) // 2
                sys.stdout.write(encoded[:half])
                sys.stdout.flush()
                time.sleep(0.2)
                sys.stdout.write(encoded[half:] + "\n")
                sys.stdout.flush()
                continue
            send({"jsonrpc": "2.0", "id": request_id, "result": result})
        else:
            # An unknown first request is answered with a closed connection.
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
