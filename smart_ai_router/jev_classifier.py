"""Experimental Jev Decisions adapter. Used by the comparison script only.

No production classifier settings or routing decisions are changed. Two calls
select fields first, then assess depth for those fields without assuming one
independent Jev question can read another question's answer.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass

import httpx

from smart_ai_router.llm_classifier import _system_prompt
from smart_ai_router.taxonomy import (
    DEMANDS, DEPTH_KEYS, FIELDS, STAKES_KEYS, PromptProfile, normalize_profile,
)

MODEL = "typesafe/jev-1.13"
URL = "https://openrouter.ai/api/alpha/decisions"
_DEPTH = dict(zip(DEPTH_KEYS, (
    "Any well-informed generalist can answer correctly.",
    "Requires daily practical experience: idiomatic code or standard analysis.",
    "Requires focused expertise, non-obvious edge cases or domain formalism.",
    "At the limit of published expertise; novel synthesis or interacting specialist constraints.",
)))


@dataclass
class JevResult:
    profile: PromptProfile | None
    latency_ms: int
    usage: list[dict]
    answers: dict
    error: str = ""


def _choice(answer: dict, options) -> str:
    if not isinstance(answer, dict) or answer.get("type") != "choice":
        raise ValueError("missing choice answer")
    value = answer.get("choice")
    if value not in options:
        raise ValueError("out-of-vocabulary choice")
    return value


def _probability(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("invalid probability")
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError("invalid probability")
    return float(value)


async def classify_jev(prompt: str, *, api_key: str, timeout: float = 15) -> JevResult:
    started = time.monotonic()
    usage, answers = [], {}
    profile, error = None, ""
    if not prompt.strip() or not api_key:
        return JevResult(None, 0, [], {}, "missing prompt or API key")
    fields = {key: label for key, (label, _) in FIELDS.items()}
    questions = {
        "primary": {"type": "choice", "instructions":
                    "Which field is most important to answering the latest request correctly?",
                    "criteria": fields},
    }
    for slot, rank in (("secondary", "second"), ("tertiary", "third")):
        questions[slot] = {"type": "choice", "instructions":
            f"Which distinct field is {rank} most important? Select none unless weakness "
            "in that additional field would make the answer wrong. Topic mentions alone do not count.",
            "criteria": {**fields, "none": "No additional expertise is required."}}
    state = {"prompt": prompt, "rubric": _system_prompt()}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            async def ask(q):
                resp = await client.post(URL, headers={"Authorization": f"Bearer {api_key}"},
                                         json={"model": MODEL, "state": state, "questions": q})
                resp.raise_for_status()
                data = resp.json()
                if isinstance(data.get("usage"), dict):
                    usage.append(data["usage"])
                block = data["answers"]
                if not isinstance(block, dict):
                    raise ValueError("invalid answers")
                answers.update(block)
                return block

            selected = await ask(questions)
            chosen = []
            for slot in questions:
                field = _choice(selected[slot], questions[slot]["criteria"])
                if field != "none" and field not in chosen:
                    chosen.append(field)
            questions = {
                f"depth_{field}": {"type": "choice", "instructions":
                    f"How much expertise in {FIELDS[field][0]} does a correct answer require?",
                    "criteria": _DEPTH} for field in chosen
            }
            questions["stakes"] = {"type": "choice", "instructions":
                "What are the consequences of an incorrect answer?", "criteria": {
                    "low": "Casual or exploratory question.",
                    "medium": "Professional work that will actually be used.",
                    "high": "Someone could act on it to medical, legal, financial or safety harm.",
                }}
            for key, (_, description) in DEMANDS.items():
                questions[f"demand_{key}"] = {"type": "noul", "instructions":
                    f"Does answering correctly require this property: {description}?",
                    "criteria": {"true": description, "false": "This property is not required."}}
            assessed = await ask(questions)
            domains = [{"field": field, "depth": _choice(assessed[f"depth_{field}"], DEPTH_KEYS)}
                       for field in chosen]
            demands = []
            for key in DEMANDS:
                answer = assessed[f"demand_{key}"]
                if not isinstance(answer, dict) or answer.get("type") != "noul":
                    raise ValueError("missing yes/no answer")
                if _probability(answer.get("noul")) >= 0.5:
                    demands.append(key)
            stakes = _choice(assessed["stakes"], STAKES_KEYS)
            profile = normalize_profile({"domains": domains, "demands": demands, "stakes": stakes})
    except httpx.HTTPStatusError as exc:
        error = f"HTTP {exc.response.status_code}"  # no provider body or credentials
    except (httpx.HTTPError, KeyError, TypeError, ValueError):
        error = "request failed or malformed decisions response"
    return JevResult(profile, int((time.monotonic() - started) * 1000), usage, answers, error)
