"""Optional OTel spans, adapted from evoagent without global provider mutation."""

from contextlib import contextmanager


@contextmanager
def span(name, **attributes):
    try:
        from opentelemetry import trace
    except ImportError:
        yield None
        return
    with trace.get_tracer("commerce-agent-harness").start_as_current_span(name) as current:
        for key, value in attributes.items():
            if value is not None:
                current.set_attribute(key, value)
        yield current
