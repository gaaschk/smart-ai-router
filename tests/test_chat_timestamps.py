"""Each transcript turn shows when it was sent — the formatting of that stamp.

Only fmtMsgTime is testable here: whether the stamp *appears* is one line of
template string in renderChatMsg, but what it says has three cases that are easy
to get silently wrong, and two of them render as text a reader would not
recognize as a bug in the code:

  * a stored turn (`ts` from the API) must read as the reader's local time
  * a live turn (no `ts` yet) must fall back to now rather than the epoch —
    `new Date("")` is Invalid Date, and `new Date(0)` is 1970
  * an unparseable string must show itself, not "Invalid Date"

Node runs the shipped function directly via the harness in
test_voice_speech_text, so there is no second copy of it to drift.
"""
import json

from tests.test_voice_speech_text import _js_function, _run, pytestmark  # noqa: F401


def _fmt(*stamps) -> list:
    """fmtMsgTime() of each argument, run in node with a fixed timezone.

    TZ is pinned so the expected local time is a constant rather than whatever
    the machine running the suite is set to.
    """
    harness = _js_function("fmtMsgTime") + "\nconsole.log(JSON.stringify(%s));" % (
        "[" + ",".join(f"fmtMsgTime({json.dumps(s)})" for s in stamps) + "]"
    )
    return _run(harness, env={"TZ": "America/New_York"})


def test_a_stored_utc_stamp_is_shown_in_local_time():
    """18:30 UTC is 14:30 in New York, and the date must come along.

    The store writes UTC; the reader is not in UTC. Showing the raw string would
    put every afternoon message an offset away from when it happened — and around
    midnight, on the wrong day.
    """
    shown, = _fmt("2026-09-09T18:30:00+00:00")
    assert "9/9/2026" in shown
    assert "2:30:00 PM" in shown


def test_a_live_turn_with_no_stamp_yet_reads_as_now():
    """A message being typed has not been persisted, so it has no ts.

    The failure this guards is not an empty stamp but a wrong one: both
    `new Date("")` and `new Date(0)` are worse than useless here.
    """
    shown, = _fmt("")
    assert "Invalid" not in shown
    assert "1970" not in shown
    # A year, at minimum — the format itself is the locale's business.
    assert "20" in shown


def test_an_unparseable_stamp_shows_itself():
    shown, = _fmt("not a date")
    assert shown == "not a date"
