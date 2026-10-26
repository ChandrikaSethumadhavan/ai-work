import os

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter, SimpleSpanProcessor

from app.orchestrator.agents import prompt_version
from app.orchestrator.graph import run_review

tracer = trace.get_tracer("pr-review")


def setup_tracing(exporter=None) -> TracerProvider:
    """Console by default. Set OTEL_EXPORTER_OTLP_ENDPOINT (+ OTEL_EXPORTER_OTLP_HEADERS) to ship to Grafana,
    Honeycomb etc. with no code change. Pass an exporter to override (tests)."""
    provider = TracerProvider()
    if exporter:
        provider.add_span_processor(SimpleSpanProcessor(exporter))
    elif os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    else:
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
    trace.set_tracer_provider(provider)
    global tracer
    tracer = provider.get_tracer("pr-review")
    return provider


def traced_llm(llm):
    """Wrap the model call from outside the graph: one span per attempt, with model, effort, prompt version, tokens."""
    async def call(model, system, user, effort):
        with tracer.start_as_current_span("llm.call") as span:  # exceptions are recorded on the span
            span.set_attributes({"llm.model": model, "llm.effort": effort, "llm.prompt_version": prompt_version(system)})
            res = await llm(model, system, user, effort)
            span.set_attributes({"llm.tokens_in": res.tokens_in, "llm.tokens_out": res.tokens_out})
            return res
    return call


async def traced_review(graph, diff: str, context: str, thread_id: str, **kw):
    with tracer.start_as_current_span("review") as span:
        span.set_attribute("review.delivery_id", thread_id)
        out = await run_review(graph, diff, context, thread_id, **kw)
        review = out.get("review", {})
        span.set_attributes({"review.action": review.get("action", ""), "review.cost_usd": out.get("cost_usd", 0.0),
                             "review.failures": len(out.get("failures", []))})
        return out
