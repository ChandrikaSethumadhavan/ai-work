from app.orchestrator.state import ReviewState

WEIGHT = {"CRITICAL": 5, "HIGH": 4, "MEDIUM": 3, "LOW": 2, "INFO": 1}
THRESHOLD = 0.80  # decision Q1=A: below this a human reviews
MUTE_MAX = WEIGHT["MEDIUM"]  # feedback may mute MEDIUM and below, never HIGH/CRITICAL


def dedupe(findings: list[dict]) -> list[dict]:
    """Merge findings on the same file/line/category: keep the most severe, max confidence, list all agents."""
    groups: dict[tuple, dict] = {}
    for f in findings:
        key = (f["file"], f.get("line"), f["category"].lower())
        g = groups.get(key)
        if g is None:
            groups[key] = {**f, "agents": [f["agent_type"]]}
            continue
        if WEIGHT[f["severity"]] > WEIGHT[g["severity"]]:
            g.update(severity=f["severity"], rationale=f["rationale"], agent_type=f["agent_type"])
        g["confidence"] = max(g["confidence"], f["confidence"])
        if f["agent_type"] not in g["agents"]:
            g["agents"].append(f["agent_type"])
    return list(groups.values())


def overall_confidence(findings: list[dict]) -> float:
    """Severity-weighted mean (decision Q1=C); no findings means nothing doubtful, so 1.0."""
    total = sum(WEIGHT[f["severity"]] for f in findings)
    return sum(WEIGHT[f["severity"]] * f["confidence"] for f in findings) / total if total else 1.0


def decide(findings: list[dict], failures: list[dict], threshold: float = THRESHOLD) -> dict:
    """The HITL gate. CRITICAL always escalates (NFR-9); an incomplete review never auto-posts."""
    confidence = overall_confidence(findings)
    if any(f["severity"] == "CRITICAL" for f in findings):
        return {"action": "escalate", "reason": "critical", "confidence": confidence}
    if failures:
        return {"action": "human", "reason": "incomplete", "confidence": confidence}
    if confidence < threshold:
        return {"action": "human", "reason": "low_confidence", "confidence": confidence}
    return {"action": "post", "reason": "confident", "confidence": confidence}


def aggregate(findings: list[dict], failures: list[dict], muted: set[str] = frozenset()) -> dict:
    merged = dedupe(findings)
    kept = [f for f in merged if not (f["category"].lower() in muted and WEIGHT[f["severity"]] <= MUTE_MAX)]
    muted_out = [f for f in merged if f not in kept]
    return {"findings": kept, "muted": muted_out, "failures": failures, **decide(kept, failures)}


def aggregate_node(state: ReviewState):
    return {"review": aggregate(state.get("findings", []), state.get("failures", []), set(state.get("muted", [])))}
