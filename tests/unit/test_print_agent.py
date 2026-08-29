"""Windows print-agent config and job handling, without talking to a printer."""

import importlib.util
import json
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
        raise agent.PrintAgentError("Chrome is not logged into AutoCount Cloud")

    status, error = agent.handle_claimed_job(JOB, _config(), print_fn=print_fn)
    assert status == "failed"
    assert "logged into AutoCount Cloud" in error


def test_load_config_reads_chrome_profile_dir(tmp_path):
    path = tmp_path / "print-agent.local.json"
    path.write_text(
        json.dumps(
            {
                "token": "file-token",
                "printer_name": PRINTER,
                "chrome_user_data_dir": r"C:\Profiles\ChromePrint",
            }
        ),
        encoding="utf-8",
    )
    config = agent.load_config(path, env={})
    assert config.chrome_user_data_dir == r"C:\Profiles\ChromePrint"


def _print_script_source():
    return (ROOT / "scripts" / "print_cloud_report.ps1").read_text(encoding="utf-8")


def test_print_script_uses_chrome_and_never_changes_default_printer():
    source = _print_script_source()
    assert r"C:\Program Files\Google\Chrome\Application\chrome.exe" in source
    assert "SetDefaultPrinter" not in source
    assert "$_.Default" not in source
    assert "msedge" not in source.lower()
    assert 'InvokeVerbEx("printto", $PrinterName)' in source
    assert "[string]$PrinterName" in source
    assert "Get-Printer -Name $PrinterName" in source


def test_print_script_helpers_are_not_advanced_functions():
    """Windows PowerShell 5.1 threw AmbiguousParameterSet on a nested helper
    with [Parameter()] inside this script (also [Parameter()] on param()).
    Helpers may exist but must stay non-advanced. Never combine Start-Process
    -Verb with -ArgumentList.
    """
    source = _print_script_source()
    assert "[Parameter(" not in source
    assert "[CmdletBinding" not in source
    assert "System.Diagnostics.ProcessStartInfo" in source
    assert "UseShellExecute" in source
    assert 'Verb = "printto"' in source
    assert "-Verb PrintTo" not in source
    assert "-Verb PrintTo -ArgumentList" not in source


def test_print_script_does_not_call_dictionary_contains():
    """PS 5.1 binds Dictionary[string,object].Contains($key) to
    ICollection<KeyValuePair>.Contains, which needs a KeyValuePair, not a
    string. The live office agent failed at Get-OrCreateDict before Chrome
    opened. Use the indexer instead; -contains is fine.
    """
    source = _print_script_source()
    assert "$parent.Contains(" not in source
    assert ".Contains($key)" not in source


def test_print_script_opens_headed_chrome_not_headless_pdf():
    source = _print_script_source()
    assert "--headless" not in source
    assert "--print-to-pdf" not in source
    assert "--kiosk-printing" in source
    assert "--no-first-run" in source
    assert "--no-default-browser-check" in source
    assert "--start-maximized" in source
    assert "--user-data-dir" in source


def test_print_script_fails_closed_on_autocount_login_page():
    source = _print_script_source()
    lowered = source.lower()
    assert "log in" in lowered
    assert "UIAutomationClient" in source
    assert "Print Report" in source
    assert "Cetak" in source
    assert "ctrl+p" in lowered or "keybd_event" in source
    assert "Math]::Max($WaitSeconds, 90)" in source or "Math]::Max($WaitSeconds,90)" in source
    assert "Get-PrintJob" in source
    assert "GetForegroundWindow" in source


def test_print_script_never_logs_cloud_url():
    source = _print_script_source()
    assert "Write-Host $Url" not in source
    assert "Write-Output $Url" not in source
    assert "Write-Verbose $Url" not in source
    assert "Write-Error $Url" not in source


def test_print_script_merges_named_printer_into_chrome_prefs():
    source = _print_script_source()
    assert "isHeaderFooterEnabled" in source
    assert '"origin":"local"' in source or '"origin": "local"' in source
    assert "Preferences" in source
    assert "ConvertFrom-Json" in source or "JavaScriptSerializer" in source


def test_print_script_stops_only_profile_chrome_not_default_user_data():
    source = _print_script_source()
    assert "--user-data-dir" in source
    assert r"Google\Chrome\User Data" in source
    assert "Stop-Process" in source


def test_print_cloud_report_docstring_describes_headed_print_report():
    doc = agent.print_cloud_report.__doc__ or ""
    lowered = doc.lower()
    assert "print report" in lowered
    assert "headless" not in lowered
    assert "print-to-pdf" not in lowered


def test_default_print_wait_covers_sso_and_report_render():
    assert agent.DEFAULT_PRINT_WAIT_SECONDS >= 90
    config = _config()
    assert config.print_wait_seconds >= 90


def test_print_cloud_report_fails_closed_off_windows():
    with pytest.raises(agent.PrintAgentError, match="Windows"):
        agent.print_cloud_report(
            JOB["cloud_report_url"], PRINTER, _config()
        )


def test_redact_secret_urls_strips_https_cloud_urls():
    raw = (
        "Chrome failed https://accounting-report.autocountcloud.com/"
        "rpt/secret-book/invoice?docKey=1 extra"
    )
    redacted = agent.redact_secret_urls(raw)
    assert "https://" not in redacted
    assert "secret-book" not in redacted
    assert "[cloud-report-url]" in redacted
    assert "extra" in redacted


def test_print_cloud_report_subprocess_has_timeout():
    source = SCRIPT.read_text(encoding="utf-8")
    assert "timeout=" in source
    assert "TimeoutExpired" in source
