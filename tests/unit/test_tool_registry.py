from app.tools.factory import build_tool_registry


def test_registry_includes_support_submission_and_scoped_status() -> None:
    class _Retriever:
        pass

    registry = build_tool_registry(_Retriever())  # type: ignore[arg-type]
    assert len(registry.names()) == 16
    assert "get_order" in registry.names()
    assert "escalate_to_human" in registry.names()
    assert "get_support_ticket" in registry.names()
