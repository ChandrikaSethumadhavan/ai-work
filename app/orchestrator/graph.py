import asyncio
import time

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from app.aggregate.gate import aggregate_node
from app.orchestrator.agents import EFFORT, MODELS, build_prompt, cost, parse_findings, prompt_version
from app.orchestrator.state import AGENTS, ReviewState

WARN_USD, STOP_USD = 1.0, 3.0  # decision Q2=C: soft warn, hard stop per PR
MAX_DIFF_CHARS = 200_000  # ponytail: hard cap instead of chunking; chunk per file if diffs outgrow it
AGENT_TIMEOUT = 300  # seconds


class Budget:
    def __init__(self, spent: float = 0.0, warn: float = WARN_USD, stop: float = STOP_USD):
        self.spent, self.warn, self.stop, self.warned = spent, warn, stop, False


def build_graph(llm, checkpointer=None, budget: Budget | None = None):
    """llm: async (model, system, user, effort) -> LLMResult. budget is shared by all agents of one review."""
    budget = budget or Budget()

    def fan_out(state: ReviewState):
        return [Send(a, state) for a in AGENTS]

    def make_agent(agent: str):
        async def node(state: ReviewState):
            def event(kind, **kw):
                return {"time": time.time(), "agent": agent, "event_type": kind, **kw}

            if budget.spent >= budget.stop:  # NFR-3: halt further model calls
                return {"events": [event("budget", detail="stopped", spent=budget.spent)],
                        "failures": [{"agent": agent, "error": "budget exceeded"}]}
            model = MODELS[agent]
            system, user = build_prompt(agent, state["diff"][:MAX_DIFF_CHARS], state.get("context", ""))
            t0 = time.time()
            try:
                res = await asyncio.wait_for(llm(model, system, user, EFFORT[agent]), AGENT_TIMEOUT)
                spent = cost(model, res.tokens_in, res.tokens_out)
                budget.spent += spent
                events = [event("llm.call", model=model, cost_usd=spent, tokens_in=res.tokens_in,
                                tokens_out=res.tokens_out, latency_ms=int((time.time() - t0) * 1000),
                                prompt_version=prompt_version(system), context_ids=state.get("context_ids", []))]
                if budget.spent >= budget.warn and not budget.warned:
                    budget.warned = True
                    events.append(event("budget", detail="warn", spent=budget.spent))
                try:
                    found = [f.model_dump() for f in parse_findings(agent, res.text)]
                except Exception as e:  # the call was made and paid for: keep its cost and event
                    return {"failures": [{"agent": agent, "error": f"{type(e).__name__}: {e}"}],
                            "events": events + [event("failure", error=str(e))], "cost_usd": spent}
                return {"findings": found, "events": events, "cost_usd": spent}
            except Exception as e:  # FR-5/AC-5: one agent failing never blocks the others
                return {"failures": [{"agent": agent, "error": f"{type(e).__name__}: {e}"}],
                        "events": [event("failure", error=str(e))]}
        return node

    g = StateGraph(ReviewState)
    for a in AGENTS:
        g.add_node(a, make_agent(a))
    g.add_node("join", aggregate_node)  # runs only after all four specialists finish
    g.add_conditional_edges(START, fan_out, list(AGENTS))
    for a in AGENTS:
        g.add_edge(a, "join")
    g.add_edge("join", END)
    graph = g.compile(checkpointer=checkpointer)
    graph.budget = budget  # run_review reseeds it from saved state
    return graph


async def run_review(graph, diff: str, context: str, thread_id: str, muted=(), context_ids=()):
    """thread_id (the GitHub delivery id) keys the checkpoint: calling again resumes instead of restarting.
    The budget is reseeded from the saved cost, so a new worker process cannot overspend after a crash."""
    cfg = {"configurable": {"thread_id": thread_id}}
    if graph.checkpointer:  # without one there is no saved state to resume from
        snap = await graph.aget_state(cfg)
        graph.budget.spent = max(graph.budget.spent, snap.values.get("cost_usd", 0.0))
        graph.budget.warned = graph.budget.warned or graph.budget.spent >= graph.budget.warn
        if snap.next:  # unfinished review: continue it
            return await graph.ainvoke(None, cfg)
    return await graph.ainvoke({"diff": diff, "context": context, "muted": list(muted), "context_ids": list(context_ids)}, cfg)
