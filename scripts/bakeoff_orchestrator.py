"""Bake off orchestrator-lane candidates on tool-call reliability.

The orchestrator lane admits a model on one test — whether its name contains
"claude" (api/proxy.py::_orchestrator_capable). That string match decides where
nearly every interactive request goes, because claudish-smart pins Claude Code's
main loop and small/fast slot to `smart-orchestrator`. This script exists to
answer the question the string match assumes: can anything else actually hold a
tool loop together?

What to look at, in priority order:
  1. called    — emitted a well-formed tool_call when the turn required one. This
                 is the failure the lane was built to prevent, and the only one
                 that used to be observable: a model that cannot call a tool
                 returns a provider 400 rather than degrading.
  2. args      — the call's arguments parse as JSON *and* carry the keys the tool
                 requires. Worse than not calling: a call with wrong arguments is
                 executed. A model that scores well on `called` and badly here is
                 more dangerous than one that never calls at all.
  3. restraint — did NOT call a tool on the turn that needed prose. In an agent
                 loop an unwanted call is an unwanted *edit*, and it also burns a
                 turn, so over-calling is not the harmless direction.
  4. progress  — after a tool result, did not re-issue the identical call. This
                 is the "loop stamina" the agentic index claims to measure, and
                 the one dimension a single-shot benchmark cannot see.
  5. http4xx   — provider rejected the request outright. Separated from `called`
                 because it is a capability answer, not a judgment one.
  6. cost, p50 — measured from the response's own usage, not the price list.

The corpus is SYNTHETIC, and that is a real limitation rather than a caveat:
nothing in the store retains an orchestrator transcript. usage_log keeps tokens
and cost, not prompts (store/sqlite_store.py:95), and chat_messages holds only
web-UI conversations, which do not carry tool calls. So CASES below is modeled on
the shapes Claude Code sends — a first turn with four tools offered, a
continuation after a tool_result, an error result to recover from, and a turn
that must be answered in prose — and it is a proxy for real traffic, not a
sample of it.

`--replay` is the way off the synthetic corpus: turn on "Capture tool-loop turns
(%)" on the Settings page, drive your own tool traffic through the router, and
the captured turns become the cases — with the incumbent's own tool calls as the
reference for what each turn required (capture.py, replay_cases()). Prefer it
over CASES for anything decisive.

Last measured (2026-09-12, 9 cases × 2 repeats, live openrouter from the deploy
host, $0.17 all in). `agentic` is the catalog's measured loop-stamina index;
0.000 means never measured, not incapable.

  model                                agentic   ok/18  over  bad args   $ run
  openrouter/anthropic/claude-sonnet-4   0.000      18     0         0  0.0771
  openrouter/anthropic/claude-haiku-4.5  0.505      18     0         0  0.0296
  deepseek/deepseek-v4-flash-0731        0.850      18     0         0  0.0007
  openrouter/openai/gpt-5-nano           0.000      17     0         0  0.0020
  openrouter/anthropic/claude-sonnet-5   0.866      14     3         1  0.0602
  openrouter/z-ai/glm-5.3-flash          0.906      14     2         1  0.0011

The result that matters is not the ranking, it is that the `agentic` index does
not predict this corpus. It admits glm-5.3-flash (0.906), which placed last on
both restraint and arguments, and excludes claude-sonnet-4 (never measured) and
gpt-5-nano (never measured), which placed first and fourth. Within the models it
does admit, 0.505 beat 0.866 and 0.906. So the obvious patch — widen
_orchestrator_capable to "tools and agentic >= AGENTIC_FLOOR" — is not supported
by the only measurement there is, and the shipped filter is deliberately
unchanged. `--pool` prints what that patch would have done.

Two limits on the numbers above, both real: 18 calls per model is a smoke test,
and a single-turn harness cannot see the fortieth turn of a loop, which is the
failure the lane was built to prevent. The replay corpus below is the way out of
the first; nothing here addresses the second.

Usage:
  python scripts/bakeoff_orchestrator.py --pool      # who each rule admits, $0
  python scripts/bakeoff_orchestrator.py --replay    # real captured turns
  python scripts/bakeoff_orchestrator.py             # store-chosen candidates
  python scripts/bakeoff_orchestrator.py openrouter/z-ai/glm-5.3-flash ...
  python scripts/bakeoff_orchestrator.py --selftest  # scoring checks, no network
"""
from __future__ import annotations

import asyncio
import json
import statistics
import sys
import time
from dataclasses import dataclass, field

import httpx

from smart_ai_router import capture as _capture
from smart_ai_router.api.proxy import _orchestrator_capable, _resolve_provider
from smart_ai_router.facade import CapabilityRouter
from smart_ai_router.models import ModelSpec
from smart_ai_router.taxonomy import AGENTIC_FLOOR

TIMEOUT = httpx.Timeout(120.0, connect=15.0)
REPEATS = 2  # temperature=0 is not determinism, especially through a router

# The four tools stand in for what Claude Code offers on a first turn. Kept
# deliberately close to the real ones: a nested-object argument (edit_file) and
# two tools whose descriptions overlap (bash can also read a file), because
# picking between plausible tools is most of the judgment.
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file from the workspace and return its contents.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Replace an exact string in a file with another.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_string": {"type": "string"},
                    "new_string": {"type": "string"},
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "Search the workspace for a regex and return matching lines.",
            "parameters": {
                "type": "object",
                "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}},
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Run a shell command in the workspace and return its output.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    },
]

SYSTEM = (
    "You are a coding agent working in a checked-out repository. Use the "
    "provided tools to inspect and change files. Do not describe what you would "
    "do when you can do it."
)


@dataclass(frozen=True)
class Case:
    name: str
    messages: list[dict]
    expect: str                            # "call" | "prose" | "either"
    want_tool: tuple[str, ...] = ()        # any one of these is the right tool
    want_args: tuple[str, ...] = ()        # argument keys the call must carry
    forbid: tuple[str, ...] = ()           # calling one of these = stalled loop
    # {tool name: required keys} straight from the offered schemas, so a replayed
    # turn checks the arguments of whichever tool the model actually picked
    # instead of a list written for one expected tool. Empty for the synthetic
    # cases, where want_args says it more directly.
    required_args: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # Tools offered on this turn. None = the four synthetic ones below; a
    # replayed turn carries whatever the real client sent.
    tools: list[dict] | None = None


def _turn(user: str) -> list[dict]:
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]


def _after_tool(user: str, call_id: str, name: str, args: dict, result: str) -> list[dict]:
    """A continuation turn: the model already called a tool and got a result."""
    return [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": user},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)},
                }
            ],
        },
        {"role": "tool", "tool_call_id": call_id, "content": result},
    ]


CASES = [
    # ── First-turn tool selection ────────────────────────────────────────────
    Case(
        "read a named file",
        _turn("What Python version does pyproject.toml require?"),
        expect="call", want_tool=("read_file", "bash"), want_args=("path",),
    ),
    Case(
        # "path" is not in the prompt, so the model must pick grep over read_file
        # instead of inventing a filename — a wrong-but-well-formed read_file
        # call scores as args-ok, which is why want_tool is checked first.
        "find where a symbol is defined",
        _turn("Where is AGENTIC_FLOOR defined?"),
        expect="call", want_tool=("grep", "bash"), want_args=("pattern",),
    ),
    Case(
        "run the tests",
        _turn("Run the test suite and tell me if it passes."),
        expect="call", want_tool=("bash",), want_args=("command",),
    ),
    Case(
        # Reading before an exact-string edit is correct, not a miss: all three
        # Claude incumbents opened with read_file here, and the first version of
        # this case scored that as wrong_tool. `path` is the key both tools share;
        # the nested-argument shape is measured by the continuation case below,
        # where the file contents are already in the transcript and there is no
        # longer any excuse for not editing.
        "start an exact-string edit",
        _turn(
            "In smart_ai_router/router.py the default min_reliability is 0.70. "
            "Change it to 0.75."
        ),
        expect="call", want_tool=("edit_file", "read_file", "grep"),
        want_args=("path",),
    ),
    # ── Restraint: prose is the right answer ─────────────────────────────────
    Case(
        "explain a pasted error",
        _turn(
            "I got this from a colleague: `TypeError: 'NoneType' object is not "
            "subscriptable`. In general terms, what causes that?"
        ),
        expect="prose",
    ),
    Case(
        "answer a design question",
        _turn(
            "Should a router that picks models keep its selection logic pure, or "
            "is it fine for it to read the database directly? Just your opinion."
        ),
        expect="prose",
    ),
    # ── Progress after a tool result ─────────────────────────────────────────
    Case(
        "continue after a successful read",
        _after_tool(
            "Add a one-line docstring to the helper in utils.py.",
            "call_1", "read_file", {"path": "utils.py"},
            'def clamp(v, lo, hi):\n    return max(lo, min(hi, v))\n',
        ),
        expect="call", want_tool=("edit_file",),
        want_args=("path", "old_string", "new_string"), forbid=("read_file",),
    ),
    Case(
        "recover from a failed read",
        _after_tool(
            "Show me what's in config/settings.yaml.",
            "call_2", "read_file", {"path": "config/settings.yaml"},
            "Error: ENOENT: no such file or directory",
        ),
        # Either look elsewhere or say it's missing — but not the same call again.
        expect="either", want_tool=("grep", "bash", "read_file"),
        forbid=(),
    ),
    Case(
        "stop when the work is done",
        _after_tool(
            "Run the test suite and tell me if it passes.",
            "call_3", "bash", {"command": "pytest -q"},
            "144 passed, 0 failed in 12.03s",
        ),
        expect="prose",
    ),
]


# ── Scoring ───────────────────────────────────────────────────────────────────
# Pure function of (case, response body) so the outcomes can be checked without
# spending money — see --selftest.

OUTCOMES = ("ok", "over_call", "missed_call", "wrong_tool", "bad_args", "stalled")


def score(case: Case, msg: dict) -> str:
    """One outcome label for one response `message` object."""
    calls = msg.get("tool_calls") or []

    if not calls:
        if case.expect in ("prose", "either"):
            return "ok"
        return "missed_call"

    if case.expect == "prose":
        return "over_call"

    first = calls[0].get("function") or {}
    name = first.get("name") or ""

    # A repeat of the call whose result is already in the transcript is a stalled
    # loop, not a wrong tool: the model had the answer and asked again.
    prior = [
        m["tool_calls"][0]["function"]
        for m in case.messages
        if m.get("role") == "assistant" and m.get("tool_calls")
    ]
    raw_args = first.get("arguments")
    if any(p.get("name") == name and p.get("arguments") == raw_args for p in prior):
        return "stalled"
    if name in case.forbid:
        return "stalled"

    if case.want_tool and name not in case.want_tool:
        return "wrong_tool"

    try:
        args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
    except (TypeError, ValueError):
        return "bad_args"
    if not isinstance(args, dict):
        return "bad_args"
    needed = tuple(case.want_args) + tuple(case.required_args.get(name, ()))
    if any(k not in args or args[k] in (None, "") for k in needed):
        return "bad_args"
    return "ok"


# ── The real corpus ───────────────────────────────────────────────────────────


def replay_cases(rows: list[dict]) -> list[Case]:
    """Turn captured turns (capture.py) into cases, using the incumbent as the
    reference for what the turn required.

    What a captured reference can and cannot settle:
      * **called / restraint** — yes. Claude called a tool on this turn or it
        answered in prose, and that is a real judgment about a real turn.
      * **args** — yes, and better than the synthetic cases: the required keys
        come from the schema the client actually sent, for whichever tool the
        candidate picks.
      * **which tool** — no. That the incumbent chose `read_file` does not make
        `grep` wrong (the first version of the synthetic corpus made exactly that
        mistake and marked all three Claudes down for it). `want_tool` is left
        empty, and the reference's choice goes in the case name to be eyeballed.
      * **progress** — yes, via the transcript: repeating a call whose result is
        already in the messages is a stall, checked by score() for free.
    """
    cases: list[Case] = []
    for i, row in enumerate(rows):
        tools = row.get("tools") or []
        required = {}
        for t in tools:
            fn = (t or {}).get("function") or {}
            name = fn.get("name")
            params = fn.get("parameters") or {}
            if name:
                required[name] = tuple(params.get("required") or ())
        ref = row.get("reference") or {}
        ref_calls = ref.get("tool_calls") or []
        ref_name = ((ref_calls[0].get("function") or {}).get("name")
                    if ref_calls else "")
        cases.append(Case(
            name=f"{i:03d} {row.get('lane', '?')} ref={ref_name or 'prose'}",
            messages=row.get("messages") or [],
            expect="call" if ref_calls else "prose",
            required_args=required,
            tools=tools or None,
        ))
    return cases


def _cost(spec: ModelSpec | None, usage: dict) -> float:
    """Dollars for one call, from the response's own token counts."""
    if spec is None or not usage:
        return 0.0
    p = usage.get("prompt_tokens") or 0
    c = usage.get("completion_tokens") or 0
    return (p * spec.cost_input + c * spec.cost_output) / 1_000_000


# ── Running ───────────────────────────────────────────────────────────────────


@dataclass
class Result:
    model: str
    counts: dict = field(default_factory=lambda: dict.fromkeys(OUTCOMES, 0))
    http_err: int = 0
    statuses: list[str] = field(default_factory=list)
    lat: list[float] = field(default_factory=list)
    cost: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(self.counts.values()) + self.http_err


async def bench(cr: CapabilityRouter, model: str,
                cases: list[Case], repeats: int) -> Result:
    spec = cr.get_model(model)
    base_url, api_key, real_model = _resolve_provider(model, cr)
    res = Result(model=model)
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else None
    url = f"{base_url.rstrip('/')}/chat/completions"

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        for case in cases:
            for _ in range(repeats):
                payload = {
                    "model": real_model,
                    "messages": case.messages,
                    "tools": case.tools or TOOLS,
                    "tool_choice": "auto",
                    "temperature": 0,
                    "max_tokens": 512,
                    "stream": False,
                }
                t0 = time.perf_counter()
                try:
                    resp = await client.post(url, json=payload, headers=headers)
                except httpx.HTTPError as exc:
                    res.http_err += 1
                    res.statuses.append(type(exc).__name__)
                    continue
                res.lat.append(time.perf_counter() - t0)
                if resp.status_code >= 400:
                    res.http_err += 1
                    res.statuses.append(f"{resp.status_code} {case.name}")
                    res.notes.append(f"HTTP {resp.status_code}  {resp.text[:120]}")
                    continue
                data = resp.json()
                res.cost += _cost(spec, data.get("usage") or {})
                try:
                    msg = data["choices"][0]["message"]
                except (KeyError, IndexError, TypeError):
                    res.http_err += 1
                    res.statuses.append(f"malformed {case.name}")
                    continue
                outcome = score(case, msg)
                res.counts[outcome] += 1
                if outcome != "ok":
                    res.notes.append(f"{outcome:<12}{case.name}")
    return res


def _candidates(cr: CapabilityRouter) -> tuple[list[str], list[str]]:
    """(admitted by the shipped name rule, admitted by a measured-agentic rule).

    Both trimmed to what a bakeoff can afford: the models the shipped lane
    actually picks, against the cheapest few that a measured rule would let in.
    """
    models = cr.all_models()
    by_name = [s.value for s in models if _orchestrator_capable(s)]
    measured = sorted(
        (s for s in models if s.tools and s.agentic >= AGENTIC_FLOOR),
        key=lambda s: s.cost_input,
    )
    return by_name, [s.value for s in measured]


def _pool_report(cr: CapabilityRouter) -> None:
    by_name, measured = _candidates(cr)
    models = {s.value: s for s in cr.all_models()}
    print(f"\nshipped rule — name contains 'claude': {len(by_name)} models")
    for v in sorted(by_name, key=lambda v: models[v].cost_input)[:12]:
        s = models[v]
        print(f"  {s.agentic:>6.3f} agentic  ${s.cost_input:>7.3f}/${s.cost_output:<7.3f}  {v}")
    print(f"\nmeasured rule — tools and agentic >= {AGENTIC_FLOOR}: {len(measured)} models")
    for v in measured[:12]:
        s = models[v]
        print(f"  {s.agentic:>6.3f} agentic  ${s.cost_input:>7.3f}/${s.cost_output:<7.3f}  {v}")
    only_name = [v for v in by_name if models[v].agentic < AGENTIC_FLOOR]
    print(f"\nadmitted by name but unmeasured or below the floor: {len(only_name)}")
    for v in sorted(only_name, key=lambda v: models[v].cost_input, reverse=True)[:8]:
        s = models[v]
        print(f"  {s.agentic:>6.3f} agentic  ${s.cost_input:>7.3f}/${s.cost_output:<7.3f}  {v}")


def _default_models(cr: CapabilityRouter) -> list[str]:
    """The incumbents plus the cheapest measured challengers, deduped."""
    by_name, measured = _candidates(cr)
    models = {s.value: s for s in cr.all_models()}
    incumbents = sorted(by_name, key=lambda v: models[v].cost_input)[:2]
    challengers = [v for v in measured if "claude" not in v][:3]
    out: list[str] = []
    for v in incumbents + challengers:
        if v not in out:
            out.append(v)
    return out


def _report(rows: list[Result], cases: list[Case], repeats: int) -> None:
    print("\n" + "=" * 108)
    print(f"{'model':<44}{'ok':>5}{'MISS':>6}{'over':>6}{'wrong':>7}"
          f"{'bad':>5}{'stall':>7}{'4xx':>5}{'p50':>8}{'$':>9}")
    # Sorted by the failures that execute something wrong (bad_args, stalled)
    # before the ones that merely waste a turn.
    for r in sorted(rows, key=lambda r: (r.counts["bad_args"] + r.counts["stalled"],
                                         r.http_err, -r.counts["ok"])):
        p50 = statistics.median(r.lat) if r.lat else 0.0
        print(f"{r.model:<44}{r.counts['ok']:>5}{r.counts['missed_call']:>6}"
              f"{r.counts['over_call']:>6}{r.counts['wrong_tool']:>7}"
              f"{r.counts['bad_args']:>5}{r.counts['stalled']:>7}{r.http_err:>5}"
              f"{p50:>7.2f}s{r.cost:>9.4f}")
    print(f"\n{len(cases)} cases x {repeats} repeats; "
          f"'ok' is out of {len(cases) * repeats}.")


def _selftest() -> None:
    """Scoring checks. Canned responses only — no network, no spend."""
    call_case = CASES[0]
    prose_case = next(c for c in CASES if c.expect == "prose")
    cont = next(c for c in CASES if c.forbid)

    def tc(name: str, args) -> dict:
        raw = args if isinstance(args, str) else json.dumps(args)
        return {"tool_calls": [{"function": {"name": name, "arguments": raw}}]}

    assert score(call_case, tc("read_file", {"path": "pyproject.toml"})) == "ok"
    assert score(call_case, tc("read_file", {})) == "bad_args"
    assert score(call_case, tc("read_file", {"path": ""})) == "bad_args"
    assert score(call_case, tc("read_file", "{not json")) == "bad_args"
    assert score(call_case, tc("grep", {"pattern": "x"})) == "wrong_tool"
    assert score(call_case, {"content": "I would read pyproject.toml"}) == "missed_call"
    assert score(prose_case, {"content": "Because the value was None."}) == "ok"
    assert score(prose_case, tc("read_file", {"path": "x.py"})) == "over_call"
    # The continuation cases: repeating the call already answered is a stall,
    # whether it is named in `forbid` or matched against the transcript.
    assert score(cont, tc("read_file", {"path": "utils.py"})) == "stalled"
    assert score(cont, tc("edit_file", {"path": "utils.py", "old_string": "a",
                                        "new_string": "b"})) == "ok"
    retry = next(c for c in CASES if c.name == "recover from a failed read")
    assert score(retry, tc("read_file", {"path": "config/settings.yaml"})) == "stalled"
    assert score(retry, tc("grep", {"pattern": "settings"})) == "ok"
    assert score(retry, {"content": "That file does not exist."}) == "ok"
    # Cost is measured from the response, not the price list.
    spec = ModelSpec(value="x", cost_input=3.0, cost_output=15.0)
    assert abs(_cost(spec, {"prompt_tokens": 1_000_000,
                            "completion_tokens": 0}) - 3.0) < 1e-9

    # ── Replay: a captured turn becomes a case with the incumbent as reference ──
    captured = [
        {
            "lane": "orchestrator",
            "messages": [{"role": "user", "content": "read setup.py"}],
            "tools": [{"type": "function", "function": {
                "name": "Read",
                "parameters": {"type": "object",
                               "properties": {"file_path": {"type": "string"}},
                               "required": ["file_path"]}}}],
            "reference": {"tool_calls": [{"function": {
                "name": "Read", "arguments": '{"file_path": "setup.py"}'}}],
                "content_len": 0},
        },
        {
            "lane": "orchestrator",
            "messages": [{"role": "user", "content": "what does DRY mean?"}],
            "tools": [],
            "reference": {"tool_calls": [], "content_len": 210},
        },
    ]
    r_call, r_prose = replay_cases(captured)
    assert r_call.expect == "call" and r_prose.expect == "prose"
    # Required keys come from the offered schema, so any tool is judged on its
    # own arguments — and the reference's *choice* of tool is never enforced.
    assert r_call.required_args == {"Read": ("file_path",)}
    assert r_call.want_tool == ()
    assert score(r_call, tc("Read", {"file_path": "setup.py"})) == "ok"
    assert score(r_call, tc("Read", {})) == "bad_args"
    assert score(r_call, tc("Grep", {"pattern": "x"})) == "ok"  # not the ref, still fine
    assert score(r_prose, {"content": "Don't repeat yourself."}) == "ok"
    assert score(r_prose, tc("Read", {"file_path": "x"})) == "over_call"
    print("selftest ok")


async def main() -> None:
    args = [a for a in sys.argv[1:]]
    if "--selftest" in args:
        _selftest()
        return

    cr = CapabilityRouter()
    if "--pool" in args:
        _pool_report(cr)
        return

    # The real corpus, when there is one: turns captured from live traffic beat
    # nine invented ones, and one repeat is enough when there are hundreds of
    # distinct turns rather than nine.
    if "--replay" in args:
        rows_in = _capture.load()
        if not rows_in:
            print(f"no captured turns in {_capture.capture_path()} — set "
                  f"'Capture tool-loop turns (%)' on the Settings page, drive "
                  f"some tool traffic, then re-run.")
            return
        cases, repeats = replay_cases(rows_in), 1
        print(f"corpus: {len(cases)} captured turns from "
              f"{_capture.capture_path()}")
    else:
        cases, repeats = CASES, REPEATS
        print(f"corpus: {len(CASES)} synthetic cases x {REPEATS} repeats — "
              f"see module docstring, and --replay")

    models = [a for a in args if not a.startswith("--")] or _default_models(cr)
    rows = []
    for m in models:
        print(f"\n=== {m}", flush=True)
        r = await bench(cr, m, cases, repeats)
        rows.append(r)
        c = r.counts
        print(f"  called    {c['ok'] + c['bad_args'] + c['wrong_tool']}/{r.total}"
              f"  (missed {c['missed_call']}, 4xx {r.http_err})")
        print(f"  args      {c['bad_args']} malformed")
        print(f"  restraint {c['over_call']} unwanted calls")
        print(f"  progress  {c['stalled']} stalled repeats")
        print(f"  cost      ${r.cost:.4f}")
        for n in r.notes[:8]:
            print(f"    {n}")
    _report(rows, cases, repeats)


if __name__ == "__main__":
    asyncio.run(main())
