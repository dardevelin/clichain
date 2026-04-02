"""Sending pipeline metrics to a webhook or monitoring endpoint.

Common CI/CD scenario: report progress, tool versions, and results
to an external system (Slack, Datadog, PagerDuty, custom dashboard).

This example uses urllib (stdlib) to avoid requiring requests as a dependency.
Replace with requests, httpx, or any HTTP client.
"""

import json
import threading
import urllib.request

from clichain import tool, set_output

set_output(None)

WEBHOOK_URL = "https://httpbin.org/post"  # replace with your endpoint


# --- Helper: fire-and-forget POST -------------------------------------------

def post_json(url: str, data: dict) -> None:
    """Non-blocking POST. Doesn't slow down the pipeline."""
    def _send() -> None:
        body = json.dumps(data).encode()
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(req, timeout=5)
        except Exception:
            pass  # don't break the pipeline if the webhook is down

    threading.Thread(target=_send, daemon=True).start()


# --- Example 1: Report meter stats to a webhook -----------------------------

echo = tool("echo")
sort = tool("sort")
wc = tool("wc")

result = (
    echo("cherry\napple\nbanana\napricot\navocado")
    .meter(
        label="processing",
        to=lambda stats: post_json(WEBHOOK_URL, {
            "event": "meter",
            "label": stats.label,
            "bytes": stats.bytes,
            "lines": stats.lines,
            "bytes_per_sec": round(stats.bytes_per_sec, 2),
        }),
        interval=0,  # report on every line
    )
    .pipe(sort())
    .pipe(wc("-l"))
    .run(validate=False)
)


# --- Example 2: Send SBOM after execution -----------------------------------

sbom = result.sbom()
post_json(WEBHOOK_URL, {
    "event": "pipeline_complete",
    "ok": result.ok,
    "elapsed": result.elapsed,
    "tools": sbom["tools"],
    "exit_code": result.returncode,
})

print(f"ok: {result.ok}, elapsed: {result.elapsed:.4f}s")


# --- Example 3: Send check failures as alerts -------------------------------

seq = tool("seq")
missing = tool("nonexistent_xyz", on_fail="warn")

pipeline = (
    seq("10")
    .pipe(missing())
)

checks = pipeline.check()
failures = [c for c in checks if not c.ok]

if failures:
    post_json(WEBHOOK_URL, {
        "event": "preflight_failure",
        "failures": [
            {"tool": c.name, "expected": c.expected, "found": c.found, "msg": c.msg}
            for c in failures
        ],
    })
    print(f"Reported {len(failures)} check failure(s) to webhook")


# --- Example 4: Periodic progress for long-running pipelines ----------------

def progress_reporter(url: str, job_id: str):
    """Returns a meter callback that posts progress updates."""
    def report(stats):
        post_json(url, {
            "event": "progress",
            "job_id": job_id,
            "bytes_processed": stats.bytes,
            "lines_processed": stats.lines,
            "throughput_bps": round(stats.bytes_per_sec, 2),
            "elapsed": round(stats.elapsed, 2),
        })
    return report


result = (
    seq("10000")
    .meter(
        label="job-42",
        to=progress_reporter(WEBHOOK_URL, "job-42"),
        interval=1.0,  # update every second
    )
    .pipe(sort("-rn"))
    .pipe(wc("-l"))
    .run(validate=False)
)

print(f"Job complete: {result.stdout.strip()} lines")
