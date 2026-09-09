from jarvis.diagnostics import (
    CapabilityStatus,
    JsonlDiagnosticLogger,
    export_redacted_report,
    redact,
    run_diagnostics,
)


def test_redaction_removes_keys_and_bearer_values():
    assert redact({"api_key": "secret", "message": "Bearer token-value"}) == {
        "api_key": "[redacted]",
        "message": "Bearer [redacted]",
    }


def test_jsonl_logger_rotates_and_keeps_correlation_id(tmp_path):
    logger = JsonlDiagnosticLogger(tmp_path / "diagnostics.jsonl", max_bytes=1, backups=1)
    first = logger.event("tool", api_key="secret")
    second = logger.event("tool", duration_ms=12)
    assert first and second
    text = (tmp_path / "diagnostics.jsonl").read_text(encoding="utf-8")
    assert second in text
    assert "secret" not in text


def test_diagnostics_isolate_a_failed_capability_check():
    report = run_diagnostics([
        lambda: CapabilityStatus("network", "available"),
        lambda: (_ for _ in ()).throw(RuntimeError("boom")),
    ])
    assert report.capabilities[0].status == "available"
    assert report.capabilities[1].status == "unavailable"


def test_report_export_is_redacted(tmp_path):
    report = run_diagnostics([
        lambda: CapabilityStatus("network", "available", cause="Bearer abc")
    ])
    output = export_redacted_report(report, tmp_path / "support.json")
    assert output.exists()
    assert "Bearer [redacted]" in output.read_text(encoding="utf-8")
