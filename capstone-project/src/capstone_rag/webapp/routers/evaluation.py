"""Local testing and evidence collection, from inside the application.

Everything here runs against isolated records or a disposable database. None
of it writes to the live demonstration data, and none of it is counted in the
live trends: scenario runs carry a `run_id`, which `analytics` already uses to
keep replays out of the business figures.
"""

from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException

from ... import config
from .. import views
from ..runtime import get_runtime
from ..schemas import ScenarioRun

router = APIRouter(prefix="/api/eval", tags=["evaluation"])


@router.post("/triage")
def triage_text(body: dict):
    """Run the deterministic screening over arbitrary text.

    Exposed because the deterministic layer is the part that must never be
    waiting on the model: this returns in milliseconds and shows exactly what
    the screening concluded and why.
    """
    text = (body or {}).get("text") or ""
    if not text.strip():
        raise HTTPException(400, "text is required")
    return views.triage_preview(text)


@router.get("/scenarios")
def scenarios(category: str = "", limit: int = 200):
    """The documented synthetic scenarios available to run locally."""
    from ... import scenarios as scenarios_mod
    cases = scenarios_mod.load(category=category or None)
    return {
        "count": len(cases),
        "categories": scenarios_mod.categories(),
        "scenarios": [c.summary() for c in cases[:limit]],
    }


@router.get("/scenarios/{scenario_id}")
def scenario(scenario_id: str):
    from ... import scenarios as scenarios_mod
    found = scenarios_mod.get(scenario_id)
    if found is None:
        raise HTTPException(404, f"no scenario {scenario_id}")
    return found.as_dict()


@router.post("/scenarios/run")
def run_scenarios(body: ScenarioRun):
    """Run scenarios deterministically against a disposable database."""
    from ... import scenarios as scenarios_mod
    rt = get_runtime()
    out_dir = body.out_dir or os.path.join(config.DATA_DIR, "_evidence")
    result = scenarios_mod.run(
        scenario_ids=[body.scenario_id] if body.scenario_id else None,
        category=body.category, limit=body.limit, seed=body.seed,
        out_dir=out_dir, agent=rt.flow.agent, prefer_llm=False)
    return result


@router.get("/runs")
def runs(limit: int = 25):
    rt = get_runtime()
    return {"runs": rt.store.query(
        "SELECT * FROM eval_runs ORDER BY started_at DESC LIMIT ?", (limit,))}


@router.get("/runs/{run_id}")
def run_detail(run_id: str):
    rt = get_runtime()
    row = rt.store.one("SELECT * FROM eval_runs WHERE run_id=?", (run_id,))
    if row is None:
        raise HTTPException(404, f"no run {run_id}")
    import json
    row["report"] = json.loads(row["report"]) if row["report"] else None
    row["cases"] = rt.store.query(
        "SELECT * FROM cases WHERE run_id=? ORDER BY created_at", (run_id,))
    return row
