"""ad-9: gates (request / approve / pending query)."""
from __future__ import annotations

from auto_dev.stages import gates


def test_request_then_approve(tmp_path):
    gates.request(tmp_path, "prd-review", detail="pending")
    assert gates.is_pending(tmp_path, "prd-review")
    assert not gates.is_approved(tmp_path, "prd-review")
    assert gates.list_pending(tmp_path) == ["prd-review"]

    gates.approve(tmp_path, "prd-review", approver="user")
    assert gates.is_approved(tmp_path, "prd-review")
    assert not gates.is_pending(tmp_path, "prd-review")


def test_close_gate(tmp_path):
    gates.request(tmp_path, "close-approval")
    assert "close-approval" in gates.list_pending(tmp_path)
    gates.approve(tmp_path, "close-approval")
    assert gates.is_approved(tmp_path, "close-approval")
