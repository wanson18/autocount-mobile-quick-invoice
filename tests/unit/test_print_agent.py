"""Office Windows print-agent scripts are removed with the queue feature."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_print_agent_script_is_gone():
    assert not (ROOT / "scripts" / "print_agent.py").exists()


def test_print_cloud_report_script_is_gone():
    assert not (ROOT / "scripts" / "print_cloud_report.ps1").exists()


def test_print_agent_local_config_example_is_gone():
    assert not (ROOT / "scripts" / "print-agent.local.json.example").exists()
