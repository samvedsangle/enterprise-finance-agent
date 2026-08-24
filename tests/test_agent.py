import sqlite3

import pytest

import app


@pytest.fixture
def isolated_database(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    app.initialize_enterprise_erp_db()
    yield tmp_path / "enterprise_finance.db"


def set_invoice_amount(database_path, amount):
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE vendor_invoices SET billed_amount = ? WHERE invoice_id = 'INV-502'",
            (amount,),
        )
        connection.commit()


def test_zero_variance_has_no_discrepancies(isolated_database):
    set_invoice_amount(isolated_database, 8200.50)

    result = app.sql_3way_matching_node({})

    assert result["discrepancies"] == []


def test_overbilling_detects_exact_variance(isolated_database):
    set_invoice_amount(isolated_database, 8500.50)

    result = app.sql_3way_matching_node({})

    assert len(result["discrepancies"]) == 1
    discrepancy = result["discrepancies"][0]
    assert discrepancy["invoice_id"] == "INV-502"
    assert discrepancy["invoice_po_variance"] == pytest.approx(300.00)
    assert discrepancy["invoice_ledger_variance"] == pytest.approx(300.00)


def test_rejected_controller_skips_webhook():
    result = app.webhook_notification_node({
        "approval_status": "REJECTED_BY_CONTROLLER",
        "discrepancies": [{"invoice_id": "INV-502"}],
    })

    assert result["webhook_status"] == "SKIPPED_NOT_APPROVED"
