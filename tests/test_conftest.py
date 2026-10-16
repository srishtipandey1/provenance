from __future__ import annotations

import pytest

import conftest


def test_graph_tests_skip_with_clear_docker_message(monkeypatch) -> None:
    monkeypatch.setattr(conftest, "_GRAPH_REACHABLE", False)

    class GraphDependentItem:
        fixturenames = ["tool_graph"]

        @staticmethod
        def get_closest_marker(name: str):
            return None

    with pytest.raises(pytest.skip.Exception) as skipped:
        conftest.pytest_runtest_setup(GraphDependentItem())

    assert str(skipped.value) == conftest._GRAPH_SKIP_MESSAGE
