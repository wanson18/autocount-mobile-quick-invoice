"""Windows print-agent config and job handling, without talking to a printer."""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "print_agent.py"


def load_agent():
    spec = importlib.util.spec_from_file_location("office_print_agent", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["office_print_agent"] = module
    spec.loader.exec_module(module)
    return module


agent = load_agent()

PRINTER = "EPSONE85FF0 (L6460 Series)"
JOB = {
    "job_id": "job-1",
    "company": "sdn_bhd",
    "doc_no": "INV-2026-0001",
    "status": "printing",
    "cloud_report_url": "https://cloud.test.invalid/invoice?docKey=inv-1",
    "printer_name": PRINTER,
}


def _config(**kwargs):
    values = dict(
        api_base_url="https://autocount-mobile-quick-invoice.vercel.app",
        token="secret-token",
        printer_name=PRINTER,
    )
    values.update(kwargs)
    return agent.AgentConfig(**values)


def test_load_config_fails_closed_without_token(tmp_path):
    with pytest.raises(agent.PrintAgentError, match="PRINT_AGENT_TOKEN"):
        agent.load_config(tmp_path / "missing.json", env={})


def test_load_config_fails_closed_without_printer(tmp_path):
    with pytest.raises(agent.PrintAgentError, match="OFFICE_PRINTER_NAME"):
        agent.load_config(
            tmp_path / "missing.json",
            env={"PRINT_AGENT_TOKEN": "secret-token"},
        )


def test_load_config_reads_json_and_env_overrides(tmp_path):
    path = tmp_path / "print-agent.local.json"
    path.write_text(
        '{"api_base_url": "https://example.test", "token": "file-token", '
        '"printer_name": "Wrong Printer"}',
        encoding="utf-8",
    )
    config = agent.load_config(
        path,
        env={
            "PRINT_AGENT_TOKEN": "env-token",
            "OFFICE_PRINTER_NAME": PRINTER,
        },
    )
    assert config.token == "env-token"
    assert config.printer_name == PRINTER
    assert config.api_base_url == "https://example.test"


def test_handle_claimed_job_dry_run_does_not_print():
    calls = []

    def print_fn(url, printer, config):
        calls.append((url, printer))

    status, error = agent.handle_claimed_job(
        JOB, _config(dry_run=True), print_fn=print_fn
    )
    assert status == "printed"
    assert error is None
    assert calls == []


def test_handle_claimed_job_uses_exact_printer_name():
    calls = []

    def print_fn(url, printer, config):
        calls.append((url, printer))

    status, error = agent.handle_claimed_job(JOB, _config(), print_fn=print_fn)
    assert status == "printed"
    assert error is None
    assert calls == [
        (JOB["cloud_report_url"], "EPSONE85FF0 (L6460 Series)")
    ]


def test_handle_claimed_job_rejects_non_https_cloud_url():
    job = dict(JOB, cloud_report_url="http://cloud.test.invalid/invoice")
    status, error = agent.handle_claimed_job(job, _config(), print_fn=lambda *a: None)
    assert status == "failed"
    assert "https" in error


def test_handle_claimed_job_reports_print_failure():
    def print_fn(url, printer, config):
        raise agent.PrintAgentError("Edge is not logged into AutoCount Cloud")

    status, error = agent.handle_claimed_job(JOB, _config(), print_fn=print_fn)
    assert status == "failed"
    assert "logged into AutoCount Cloud" in error


def test_print_cloud_report_fails_closed_off_windows():
    with pytest.raises(agent.PrintAgentError, match="Windows"):
        agent.print_cloud_report(
            JOB["cloud_report_url"], PRINTER, _config()
        )
