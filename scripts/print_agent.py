"""Office Windows print agent.

Polls the live API, claims a queued invoice print job, opens the official
AutoCount Cloud report in headed Google Chrome using the office PC's Cloud
login, clicks Print Report, and sends that printout to printer
``EPSONE85FF0 (L6460 Series)`` by that exact name. Configure via environment
variables or a gitignored ``print-agent.local.json`` next to this script.

The Cloud report URL is received only after claiming a job with
``PRINT_AGENT_TOKEN``. It is never logged (the account-book path lives in
that URL). The iPhone never sees it.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

DEFAULT_PRINTER_NAME = "EPSONE85FF0 (L6460 Series)"
DEFAULT_API_BASE = "https://autocount-mobile-quick-invoice.vercel.app"
DEFAULT_POLL_INTERVAL = 3.0
DEFAULT_PRINT_WAIT_SECONDS = 90
CONFIG_FILENAME = "print-agent.local.json"


class PrintAgentError(Exception):
    """The agent cannot start or cannot print this job."""


@dataclass(frozen=True)
class AgentConfig:
    api_base_url: str
    token: str
    printer_name: str
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL
    print_wait_seconds: int = DEFAULT_PRINT_WAIT_SECONDS
    chrome_user_data_dir: str | None = None
    dry_run: bool = False


def load_config(
    path: str | Path | None = None,
    env: dict[str, str] | None = None,
) -> AgentConfig:
    """Load API URL, token, and printer name. Fail closed if any is missing."""
    env = env if env is not None else dict(os.environ)
    file_values: dict[str, Any] = {}
    config_path = Path(path) if path else Path(__file__).with_name(CONFIG_FILENAME)
    if config_path.is_file():
        file_values = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(file_values, dict):
            raise PrintAgentError(f"{config_path} must contain a JSON object")

    api_base = _first(
        env.get("PRINT_API_BASE_URL"),
        file_values.get("api_base_url"),
        DEFAULT_API_BASE,
    )
    token = _first(env.get("PRINT_AGENT_TOKEN"), file_values.get("token"))
    printer = _first(
        env.get("OFFICE_PRINTER_NAME"),
        file_values.get("printer_name"),
    )
    if not token:
        raise PrintAgentError(
            "PRINT_AGENT_TOKEN is not configured (set the env var or "
            f"{CONFIG_FILENAME})"
        )
    if not printer:
        raise PrintAgentError(
            "OFFICE_PRINTER_NAME is not configured (set the env var or "
            f"{CONFIG_FILENAME})"
        )
    if not api_base.startswith("https://") and not api_base.startswith("http://localhost"):
        raise PrintAgentError("PRINT_API_BASE_URL must be https (or localhost for tests)")

    poll = env.get("PRINT_POLL_INTERVAL_SECONDS") or file_values.get(
        "poll_interval_seconds", DEFAULT_POLL_INTERVAL
    )
    wait = env.get("PRINT_WAIT_SECONDS") or file_values.get(
        "print_wait_seconds", DEFAULT_PRINT_WAIT_SECONDS
    )
    profile = _first(
        env.get("PRINT_CHROME_USER_DATA_DIR"),
        env.get("PRINT_EDGE_USER_DATA_DIR"),
        file_values.get("chrome_user_data_dir"),
        file_values.get("edge_user_data_dir"),
    )
    dry_run = _truthy(env.get("PRINT_DRY_RUN")) or bool(file_values.get("dry_run"))
    return AgentConfig(
        api_base_url=api_base.rstrip("/"),
        token=token,
        printer_name=printer,
        poll_interval_seconds=float(poll),
        print_wait_seconds=int(wait),
        chrome_user_data_dir=profile or None,
        dry_run=dry_run,
    )


def _first(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def request_json(
    method: str,
    url: str,
    *,
    token: str,
    body: dict[str, Any] | None = None,
    timeout: float = 30,
) -> tuple[int, dict[str, Any] | None]:
    data = None
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "autocount-office-print-agent",
    }
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            if response.status == 204 or not raw:
                return response.status, None
            return response.status, json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        parsed: dict[str, Any] | None = None
        if raw:
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                parsed = {"message": raw.decode("utf-8", errors="replace")}
        return exc.code, parsed


def claim_next_job(config: AgentConfig) -> dict[str, Any] | None:
    status, payload = request_json(
        "POST",
        f"{config.api_base_url}/api/print-agent/jobs/next",
        token=config.token,
    )
    if status == 204:
        return None
    if status != 200 or not payload or "data" not in payload:
        message = (payload or {}).get("message") or f"claim failed ({status})"
        raise PrintAgentError(message)
    return payload["data"]


def complete_job(
    config: AgentConfig, job_id: str, status: str, error_message: str | None = None
) -> None:
    body: dict[str, Any] = {"status": status}
    if error_message:
        body["error_message"] = error_message
    http_status, payload = request_json(
        "POST",
        f"{config.api_base_url}/api/print-agent/jobs/{job_id}/complete",
        token=config.token,
        body=body,
    )
    if http_status != 200:
        message = (payload or {}).get("message") or f"complete failed ({http_status})"
        raise PrintAgentError(message)


def handle_claimed_job(
    job: dict[str, Any],
    config: AgentConfig,
    *,
    print_fn: Callable[[str, str, AgentConfig], None] | None = None,
) -> tuple[str, str | None]:
    """Print one claimed job. Returns (printed|failed, error_message)."""
    url = job.get("cloud_report_url")
    if not isinstance(url, str) or not url.startswith("https://"):
        return "failed", "claimed job did not include an https Cloud report URL"
    printer = (job.get("printer_name") or config.printer_name or "").strip()
    if not printer:
        return "failed", "office printer is not configured"
    if config.dry_run:
        return "printed", None
    try:
        (print_fn or print_cloud_report)(url, printer, config)
    except Exception as exc:
        return "failed", str(exc) or "print failed"
    return "printed", None


def print_cloud_report(url: str, printer_name: str, config: AgentConfig) -> None:
    """Print the official Cloud report on Windows to the named Epson.

    Opens the Cloud URL in headed Google Chrome with a dedicated profile
    (so the office AutoCount Cloud login can persist), waits until the
    Cloud report is showing (not the login page), clicks AutoCount Print Report,
    and prints that printout to the printer named exactly ``printer_name``.
    The Windows default printer is never read or changed.
    """
    if sys.platform != "win32":
        raise PrintAgentError(
            "office printing runs on the always-on Windows PC; "
            "use PRINT_DRY_RUN=1 to exercise the queue from this OS"
        )
    script = Path(__file__).with_name("print_cloud_report.ps1")
    if not script.is_file():
        raise PrintAgentError(f"missing {script}")
    args = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
        "-Url",
        url,
        "-PrinterName",
        printer_name,
        "-WaitSeconds",
        str(config.print_wait_seconds),
    ]
    if config.chrome_user_data_dir:
        args.extend(["-UserDataDir", config.chrome_user_data_dir])
    completed = subprocess.run(args, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip() or (
            f"print_cloud_report.ps1 exited {completed.returncode}"
        )
        raise PrintAgentError(detail)


def run_once(
    config: AgentConfig,
    *,
    print_fn: Callable[[str, str, AgentConfig], None] | None = None,
) -> bool:
    """Claim at most one job and print it. Returns True if a job was handled."""
    job = claim_next_job(config)
    if job is None:
        return False
    job_id = job.get("job_id")
    doc_no = job.get("doc_no")
    print(f"claimed job {job_id} for {doc_no}", flush=True)
    status, error = handle_claimed_job(job, config, print_fn=print_fn)
    complete_job(config, str(job_id), status, error)
    print(f"job {job_id} {status}", flush=True)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="path to print-agent.local.json")
    parser.add_argument("--once", action="store_true", help="claim at most one job and exit")
    parser.add_argument("--dry-run", action="store_true", help="claim and complete without printing")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
    except PrintAgentError as exc:
        print(f"print agent cannot start: {exc}", file=sys.stderr)
        return 2
    if args.dry_run:
        config = replace(config, dry_run=True)
    print(
        f"print agent polling {config.api_base_url} for printer "
        f"{config.printer_name!r}"
        + (" (dry-run)" if config.dry_run else ""),
        flush=True,
    )
    try:
        if args.once:
            run_once(config)
            return 0
        while True:
            try:
                run_once(config)
            except PrintAgentError as exc:
                print(f"print agent error: {exc}", file=sys.stderr, flush=True)
            time.sleep(config.poll_interval_seconds)
    except KeyboardInterrupt:
        print("print agent stopped", flush=True)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
