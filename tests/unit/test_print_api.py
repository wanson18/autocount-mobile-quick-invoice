"""Office print-job API is gone; the phone cannot queue Epson jobs.

Open Cloud Report (GET .../cloud-report) remains the print handoff.
Print-agent claim/complete routes must not exist.
"""

from fastapi.testclient import TestClient

from app.main import app

GONE = (
    "/api/sdn_bhd/invoices/INV-2026-0001/print",
    "/api/sdn_bhd/invoices/INV-2026-0001/print/job-1",
    "/api/print-agent/jobs/next",
    "/api/print-agent/jobs/job-1/complete",
)


def _client() -> TestClient:
    app.openapi_schema = None
    return TestClient(app)


def _assert_gone(response) -> None:
    assert response.status_code in {404, 405}
    try:
        body = response.json()
    except ValueError:
        return
    if not isinstance(body, dict):
        return
    # A resurrected print handler that 404s as invoice_not_found would still
    # be a print route. Starlette's unmatched mount answers {"detail": ...}.
    assert "data" not in body
    assert body.get("error") not in {
        "invoice_not_found",
        "unsupported",
        "unauthorized",
        "print_job_not_found",
        "print_job_conflict",
    }


def test_print_enqueue_and_status_routes_are_gone():
    client = _client()
    _assert_gone(client.post("/api/sdn_bhd/invoices/INV-2026-0001/print"))
    _assert_gone(client.get("/api/sdn_bhd/invoices/INV-2026-0001/print/job-1"))


def test_print_agent_claim_and_complete_routes_are_gone():
    client = _client()
    _assert_gone(
        client.post(
            "/api/print-agent/jobs/next",
            headers={"Authorization": "Bearer anything"},
        )
    )
    _assert_gone(
        client.post(
            "/api/print-agent/jobs/job-1/complete",
            headers={"Authorization": "Bearer anything"},
            json={"status": "printed"},
        )
    )


def test_print_routes_are_absent_from_gpt_schema():
    client = _client()
    schema = client.get("/openapi.json").json()
    for path in schema["paths"]:
        assert "/print" not in path
        assert "print-agent" not in path
    for path in GONE:
        assert path not in schema["paths"]
