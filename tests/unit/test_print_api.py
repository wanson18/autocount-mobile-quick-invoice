"""Office print API: enqueue, agent auth, job state, fail-closed config.

Hidden from the Custom GPT schema. Mobile responses must never contain the
Cloud report URL, account-book path, printer name, or agent token.
"""

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.dependencies import get_master_data, get_print_job_repository
from app.main import app
from app.models.master_data import InvoiceSummary
from app.repositories.print_job_repository import PrintJobRepository

KEY_ID = "api-key-id-99"
API_KEY = "api-key-secret-xyz-789"
ENTERPRISE_AB = "ab-wanson-enterprise-001"
SDN_BHD_AB = "ab-wanson-sdn-bhd-001"
PRINT_TOKEN = "print-agent-secret-test-token"
PRINTER = "EPSONE85FF0 (L6460 Series)"
CLOUD_TEMPLATE = (
    "https://cloud.test.invalid/rpt/secret-book-path/invoice?docKey={doc_key}"
)
CLOUD_URL = "https://cloud.test.invalid/rpt/secret-book-path/invoice?docKey=inv-1"


class FakeMasterData:
    async def get_invoice(self, company, invoice_no):
        if invoice_no == "MISSING":
            from app.autocount.errors import AutoCountRejectedError

            raise AutoCountRejectedError(404, "not found")
        return InvoiceSummary(
            id="inv-1",
            doc_no=invoice_no,
            doc_date="2026-08-05",
            debtor_code="C001",
            total=Decimal("63.00"),
            lines=(),
        )


@pytest.fixture
def print_api(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOCOUNT_ACCOUNT_BOOK_WANSON_ENTERPRISE", ENTERPRISE_AB)
    monkeypatch.setenv("AUTOCOUNT_ACCOUNT_BOOK_WANSON_SDN_BHD", SDN_BHD_AB)
    monkeypatch.setenv("AUTOCOUNT_API_KEY_ID", KEY_ID)
    monkeypatch.setenv("AUTOCOUNT_API_KEY", API_KEY)
    monkeypatch.setenv("AUTOCOUNT_CLOUD_INVOICE_URL_TEMPLATE", CLOUD_TEMPLATE)
    monkeypatch.setenv("OFFICE_PRINTER_NAME", PRINTER)
    monkeypatch.setenv("PRINT_AGENT_TOKEN", PRINT_TOKEN)
    jobs = PrintJobRepository(tmp_path / "print_jobs.db")
    master = FakeMasterData()
    app.dependency_overrides[get_master_data] = lambda: master
    app.dependency_overrides[get_print_job_repository] = lambda: jobs
    app.openapi_schema = None
    yield TestClient(app), jobs
    app.dependency_overrides.clear()
    app.openapi_schema = None


def _assert_mobile_safe(body: str) -> None:
    lowered = body.lower()
    assert SDN_BHD_AB not in body
    assert ENTERPRISE_AB not in body
    assert "secret-book-path" not in body
    assert CLOUD_URL not in body
    assert "cloud_report_url" not in lowered
    assert "accounting-report" not in lowered
    assert PRINT_TOKEN not in body
    assert PRINTER not in body
    assert API_KEY not in body


def test_enqueue_returns_queued_job_without_cloud_url(print_api):
    client, _ = print_api
    response = client.post("/api/sdn_bhd/invoices/INV-2026-0001/print")
    assert response.status_code == 201
    data = response.json()["data"]
    assert data["status"] == "queued"
    assert data["doc_no"] == "INV-2026-0001"
    assert data["company"] == "sdn_bhd"
    assert data["job_id"]
    assert "cloud_report_url" not in data
    _assert_mobile_safe(response.text)


def test_enqueue_fails_closed_without_cloud_template(print_api, monkeypatch):
    monkeypatch.delenv("AUTOCOUNT_CLOUD_INVOICE_URL_TEMPLATE", raising=False)
    client, _ = print_api
    response = client.post("/api/sdn_bhd/invoices/INV-2026-0001/print")
    assert response.status_code == 501
    assert response.json()["error"] == "unsupported"
    assert "Cloud report" in response.json()["message"]
    _assert_mobile_safe(response.text)


def test_enqueue_fails_closed_without_printer_config(print_api, monkeypatch):
    monkeypatch.delenv("OFFICE_PRINTER_NAME", raising=False)
    client, _ = print_api
    response = client.post("/api/sdn_bhd/invoices/INV-2026-0001/print")
    assert response.status_code == 501
    assert response.json()["error"] == "unsupported"
    assert "printer" in response.json()["message"].lower()
    _assert_mobile_safe(response.text)


def test_enqueue_fails_closed_on_invalid_cloud_template(print_api, monkeypatch):
    monkeypatch.setenv(
        "AUTOCOUNT_CLOUD_INVOICE_URL_TEMPLATE",
        "https://cloud.test.invalid/invoice?docKey=9001",
    )
    client, _ = print_api
    response = client.post("/api/sdn_bhd/invoices/INV-2026-0001/print")
    assert response.status_code == 500
    assert response.json()["error"] == "server_configuration_error"


def test_enqueue_missing_invoice_is_404(print_api):
    client, _ = print_api
    response = client.post("/api/sdn_bhd/invoices/MISSING/print")
    assert response.status_code == 404
    assert response.json()["error"] == "invoice_not_found"


def test_mobile_status_poll_stays_safe_through_printing(print_api):
    client, _ = print_api
    queued = client.post("/api/sdn_bhd/invoices/INV-2026-0001/print").json()["data"]
    job_id = queued["job_id"]
    claimed = client.post(
        "/api/print-agent/jobs/next",
        headers={"Authorization": f"Bearer {PRINT_TOKEN}"},
    )
    assert claimed.status_code == 200
    assert claimed.json()["data"]["cloud_report_url"] == CLOUD_URL
    assert claimed.json()["data"]["printer_name"] == PRINTER

    status = client.get(
        f"/api/sdn_bhd/invoices/INV-2026-0001/print/{job_id}"
    )
    assert status.status_code == 200
    assert status.json()["data"]["status"] == "printing"
    _assert_mobile_safe(status.text)

    done = client.post(
        f"/api/print-agent/jobs/{job_id}/complete",
        headers={"Authorization": f"Bearer {PRINT_TOKEN}"},
        json={"status": "printed"},
    )
    assert done.status_code == 200
    printed = client.get(
        f"/api/sdn_bhd/invoices/INV-2026-0001/print/{job_id}"
    )
    assert printed.json()["data"]["status"] == "printed"
    _assert_mobile_safe(printed.text)


def test_agent_claim_without_token_is_401(print_api):
    client, _ = print_api
    client.post("/api/sdn_bhd/invoices/INV-2026-0001/print")
    response = client.post("/api/print-agent/jobs/next")
    assert response.status_code == 401
    assert response.json()["error"] == "unauthorized"


def test_agent_claim_with_wrong_token_is_401(print_api):
    client, _ = print_api
    client.post("/api/sdn_bhd/invoices/INV-2026-0001/print")
    response = client.post(
        "/api/print-agent/jobs/next",
        headers={"Authorization": "Bearer totally-not-the-token"},
    )
    assert response.status_code == 401
    assert PRINT_TOKEN not in response.text


def test_agent_endpoints_fail_closed_when_token_unset(print_api, monkeypatch):
    monkeypatch.delenv("PRINT_AGENT_TOKEN", raising=False)
    client, _ = print_api
    response = client.post(
        "/api/print-agent/jobs/next",
        headers={"Authorization": f"Bearer {PRINT_TOKEN}"},
    )
    assert response.status_code == 500
    assert response.json()["error"] == "server_configuration_error"


def test_agent_claim_empty_queue_is_204(print_api):
    client, _ = print_api
    response = client.post(
        "/api/print-agent/jobs/next",
        headers={"Authorization": f"Bearer {PRINT_TOKEN}"},
    )
    assert response.status_code == 204


def test_complete_failed_stores_message_for_mobile(print_api):
    client, _ = print_api
    job_id = client.post("/api/sdn_bhd/invoices/INV-2026-0001/print").json()[
        "data"
    ]["job_id"]
    client.post(
        "/api/print-agent/jobs/next",
        headers={"Authorization": f"Bearer {PRINT_TOKEN}"},
    )
    client.post(
        f"/api/print-agent/jobs/{job_id}/complete",
        headers={"Authorization": f"Bearer {PRINT_TOKEN}"},
        json={
            "status": "failed",
            "error_message": "Edge is not logged into AutoCount Cloud",
        },
    )
    status = client.get(f"/api/sdn_bhd/invoices/INV-2026-0001/print/{job_id}")
    assert status.json()["data"]["status"] == "failed"
    assert "logged into AutoCount Cloud" in status.json()["data"]["error_message"]
    _assert_mobile_safe(status.text)


def test_cannot_complete_unclaimed_job(print_api):
    client, _ = print_api
    job_id = client.post("/api/sdn_bhd/invoices/INV-2026-0001/print").json()[
        "data"
    ]["job_id"]
    response = client.post(
        f"/api/print-agent/jobs/{job_id}/complete",
        headers={"Authorization": f"Bearer {PRINT_TOKEN}"},
        json={"status": "printed"},
    )
    assert response.status_code == 409
    assert response.json()["error"] == "print_job_conflict"


def test_status_for_other_invoice_is_404(print_api):
    client, _ = print_api
    job_id = client.post("/api/sdn_bhd/invoices/INV-2026-0001/print").json()[
        "data"
    ]["job_id"]
    response = client.get(f"/api/sdn_bhd/invoices/OTHER/print/{job_id}")
    assert response.status_code == 404


def test_print_routes_are_hidden_from_gpt_schema(print_api):
    client, _ = print_api
    paths = client.get("/openapi.json").json()["paths"]
    assert "/api/invoices" in paths
    assert "/api/{company}/invoices/{invoice_id}/pdf" in paths
    for path in paths:
        assert "/print" not in path
        assert "print-agent" not in path
    schema_text = client.get("/openapi.json").text
    assert PRINT_TOKEN not in schema_text
    assert "secret-book-path" not in schema_text
    assert SDN_BHD_AB not in schema_text


def test_failed_complete_requires_error_message(print_api):
    client, _ = print_api
    job_id = client.post("/api/sdn_bhd/invoices/INV-2026-0001/print").json()[
        "data"
    ]["job_id"]
    client.post(
        "/api/print-agent/jobs/next",
        headers={"Authorization": f"Bearer {PRINT_TOKEN}"},
    )
    response = client.post(
        f"/api/print-agent/jobs/{job_id}/complete",
        headers={"Authorization": f"Bearer {PRINT_TOKEN}"},
        json={"status": "failed"},
    )
    assert response.status_code == 422
