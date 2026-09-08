"""The parts of voice mode that can be run without a microphone, exercised in node.

Voice lives in the browser, so the microphone, synthesis and permissions can't be
tested here. What can is the text that gets read aloud, and the state machine that
decides when the microphone is open — and that machine is where the bugs were:

  * a swallowed exception from recognition.start() left the button saying
    "Listening…" with nothing behind it and nothing that would ever restart it
  * the microphone re-armed as soon as the question ended, so it was open through
    the whole reply and heard the assistant through the speakers

Both are invisible from outside: the page looks armed and simply never answers.

The text transforms matter for a different reason — they decide whether voice is
pleasant or unbearable:

  * stripForSpeech — what a reply sounds like when read aloud. Verbatim Markdown
    dictates fenced code, pipe tables and URLs, which is the fastest way to make
    someone turn voice off and never turn it back on.
  * the chunk boundary in speakStreamed — the reply is spoken while it is still
    arriving, so it has to split on sentence ends. Split anywhere else and the
    listener hears half a phrase, a pause, then the rest.

Extracting the functions out of the single-file UI and running them under node is
the whole harness. It needs no bundler, no jsdom, and no second copy of the logic
to drift from the one that ships — the assertions run against the source of truth.
Skipped when node isn't installed rather than failing: it's the only test here
that needs a second runtime.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_UI = Path(__file__).resolve().parents[1] / "smart_ai_router/api/ui/index.html"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not installed"
)


def _js_function(name: str) -> str:
    """The source of one top-level `function name(...) {...}` from the UI file.

    Brace-counted rather than regex-matched to the closing brace, because these
    functions contain braces inside regex literals and template strings.
    """
    src = _UI.read_text()
    for decl in (f"\nfunction {name}(", f"\nasync function {name}("):
        start = src.find(decl)
        if start != -1:
            break
    assert start != -1, f"no top-level function {name} in {_UI.name}"
    # The body's opening brace, not a destructured parameter's — `function f(a,
    # { flush = false } = {})` has two braces before the body starts, and counting
    # from the first one returns the parameter list as if it were the function.
    params_end = src.index(")", start)
    depth, j = 0, src.index("{", params_end)
    while True:
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                break
        j += 1
    return src[start:j + 1]


def _run(harness: str) -> list:
    """Run JS that console.logs one JSON array, and return it."""
    out = subprocess.run(
        ["node", "-e", harness], capture_output=True, text=True, timeout=30,
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def _speech_of(*texts: str) -> list:
    harness = _js_function("stripForSpeech") + "\nconsole.log(JSON.stringify(%s));" % (
        "[" + ",".join(f"stripForSpeech({json.dumps(t)})" for t in texts) + "]"
    )
    return _run(harness)


# ── What gets read aloud ────────────────────────────────────────────────────────

def test_code_blocks_are_summarized_not_dictated():
    """"open brace, def, colon" is not listening — and the code is on screen.

    A voice mode that reads a fenced block character by character is worse than no
    voice mode, because the listener has to sit through it to reach the sentence
    after it.
    """
    said, = _speech_of("Here it is:\n```python\ndef f(x):\n    return x + 1\n```\nThat's all.")
    assert "def f" not in said
    assert "code omitted" in said
    # The prose on both sides survives — the point is to skip the code, not the answer.
    assert "Here it is" in said and "That's all" in said


def test_an_unclosed_code_block_is_still_skipped():
    """Mid-stream, every code block is unclosed for a while.

    Because speech starts before the reply finishes, the common case is a fence
    with no partner yet. Matched to end-of-string, or the first half of every code
    block gets read out loud as it arrives.
    """
    said, = _speech_of("Try this:\n```js\nconst x = 1;")
    assert "const x" not in said
    assert "code omitted" in said


def test_link_urls_are_dropped_and_their_text_kept():
    """"h-t-t-p-s colon slash slash" is the least useful thing a voice can say."""
    said, = _speech_of("See [the WNBA site](https://www.wnba.com/teams) for the list.")
    assert "wnba.com" not in said
    assert "the WNBA site" in said


def test_markdown_punctuation_is_not_pronounced():
    said, = _speech_of("## Summary\n**Bold** and _italic_ and `inline`.\n> quoted")
    for ch in "#*_`>":
        assert ch not in said
    assert "Summary" in said and "Bold" in said and "inline" in said


def test_tables_are_not_read_cell_by_cell():
    """A table read aloud is a stream of pipes. It's on screen; skip it."""
    said, = _speech_of("Teams:\n| Team | Year |\n| --- | --- |\n| Valkyries | 2025 |\nDone.")
    assert "Valkyries" not in said
    assert "Teams" in said and "Done" in said


def test_empty_and_plain_text_survive_unchanged():
    empty, plain = _speech_of("", "The capital of France is Paris.")
    assert empty == ""
    assert plain == "The capital of France is Paris."


# ── Where the stream is cut for speaking ────────────────────────────────────────

def _chunks(deltas: list[str]) -> list:
    """Replay a stream through speakStreamed and collect what got spoken.

    speechSynthesis is stubbed to record utterances, which is the only thing being
    asserted: this is about *when* text is handed over, not about audio.
    """
    harness = f"""
      {_js_function("stripForSpeech")}
      {_js_function("speakStreamed")}
      let _voiceOn = true, _spokenUpTo = 0;
      const spoken = [];
      function pickVoice() {{ return null; }}
      global.SpeechSynthesisUtterance = function (t) {{ this.text = t; }};
      global.window = {{ speechSynthesis: {{ speak: (u) => spoken.push(u.text) }} }};
      let acc = '';
      for (const d of {json.dumps(deltas)}) {{ acc += d; speakStreamed(acc); }}
      speakStreamed(acc, {{ flush: true }});
      console.log(JSON.stringify(spoken));
    """
    return _run(harness)


def test_speech_starts_before_the_reply_is_finished():
    """The reason for chunking at all.

    Waiting for the last token puts the whole generation — seconds, or a minute on
    a long answer — in front of the first sound. The first sentence has to go out
    while the second is still being written.
    """
    spoken = _chunks(["The WNBA has 13 teams. ", "A 14th debuts in 2025. ", "That's it."])
    assert len(spoken) > 1
    assert spoken[0].startswith("The WNBA has 13 teams.")


def test_a_sentence_is_never_split_across_utterances():
    """Cut mid-phrase, the listener hears "The WNBA has 13" … "teams."

    Deltas arrive a few characters at a time and have nothing to do with sentence
    boundaries, so the split has to be found in the accumulated text.
    """
    spoken = _chunks(["The ", "WNBA ", "has ", "13 ", "teams. ", "More ", "soon."])
    for utterance in spoken:
        stripped = utterance.strip()
        assert stripped.endswith((".", "!", "?")), f"cut mid-sentence: {utterance!r}"


def test_everything_is_spoken_exactly_once():
    """No repeats, no gaps: both are audible and both sound like a bug.

    Tracked by an index into the cleaned text, which is the part that could drift
    as later deltas change how earlier text strips.
    """
    deltas = ["Hello there. ", "How are you? ", "Fine, thanks!"]
    assert "".join(_chunks(deltas)).replace(" ", "") == "".join(deltas).replace(" ", "")


def test_the_last_fragment_is_spoken_even_without_final_punctuation():
    """A reply cut off by the output ceiling has no closing period.

    Without the flush the tail is silently dropped — the listener would hear a
    truncated answer truncated a second time, and have no way to tell.
    """
    spoken = _chunks(["The bounty hunter drew his"])
    assert "".join(spoken).strip() == "The bounty hunter drew his"


# ── When the microphone is open ─────────────────────────────────────────────────

def _mic(script: str) -> list:
    """Drive startListening() against a fake SpeechRecognition.

    Everything the browser supplies is stubbed to the minimum the function
    touches. `mics` records one entry per recogniser the page constructs, which is
    the thing under test: how many times, and when, the microphone is opened.

    Timers are faked and advanced by `tick(ms)`. The code under test waits 150ms to
    re-arm and five seconds to give up on a recogniser that never opened, and
    sleeping through either would trade seconds of suite time for a less certain
    result — a real clock makes "the timer hasn't fired yet" and "the timer did
    nothing" the same observation.
    """
    harness = f"""
      {_js_function("setVoiceButton")}
      {_js_function("stopVoice")}
      {_js_function("startListening")}
      let _voiceOn = false, _voiceBusy = false, _rec = null;
      function _vlog() {{}}
      function stopMeter() {{}}
      const mics = [], alerts = [], sends = [], statuses = [];
      // Fake clock. `now` only ever moves when a test says so, so a callback that
      // ran is a callback the code asked to run at a time the test reached.
      let now = 0;
      const timers = [];
      global.setTimeout = (fn, ms) => timers.push({{ fn, at: now + (ms || 0) }});
      global.clearTimeout = (id) => {{ if (timers[id - 1]) timers[id - 1].dead = true; }};
      function tick(ms) {{
        now += ms;
        for (const t of timers.slice()) {{
          if (!t.dead && t.at <= now) {{ t.dead = true; t.fn(); }}
        }}
      }}
      // The status strip is the feature under test in several of these: it is the
      // only channel the page has for "it can hear you" and "it cannot".
      function setVoiceStatus(text, problem) {{ statuses.push([text || '', !!problem]); }}
      const said = () => statuses.map(s => s[0]).join(' | ');
      let startThrows = null;
      function sendChat() {{ sends.push(1); }}
      function _Rec() {{
        this.started = false;
        this.start = () => {{ if (startThrows) throw startThrows; this.started = true; }};
        this.stop = () => {{ this.onend && this.onend(); }};
        this.abort = () => {{}};
        mics.push(this);
      }}
      global.alert = (m) => alerts.push(m);
      global.navigator = {{ language: 'en-US' }};
      // One stub for both lookups the code makes: the textarea (.value) and the
      // button (.classList/.textContent). The label isn't what's under test.
      const el = {{ value: '', textContent: '', classList: {{ toggle: () => {{}} }} }};
      global.document = {{ getElementById: () => el }};
      global.window = {{ speechSynthesis: {{ cancel: () => {{}} }} }};
      function heard(rec, text, isFinal) {{
        const results = [[{{ transcript: text }}]];
        results[0].isFinal = isFinal;
        rec.onresult({{ results }});
      }}
      {script}
    """
    return _run(harness)


def test_a_microphone_that_will_not_start_says_so_instead_of_pretending():
    """The reported symptom: toggled on, stays on, never responds.

    start() throws for reasons the page can't see — OS-level microphone
    permission, a browser that refuses outside a gesture. Swallowed, the button
    keeps claiming it is listening and no callback will ever fire to correct it,
    because no recogniser was ever opened.
    """
    out, = _mic("""
      _voiceOn = true;
      startThrows = new Error('not allowed');
      startThrows.name = 'InvalidStateError';
      startListening();
      console.log(JSON.stringify([{ on: _voiceOn, alerts: alerts.length, rec: _rec }]));
    """)
    assert out["alerts"] == 1, "failure was swallowed"
    assert out["on"] is False, "voice still claims to be armed"
    assert out["rec"] is None


def test_the_microphone_stays_shut_for_the_whole_assistant_turn():
    """Otherwise the assistant hears itself and answers its own reply.

    Recognition ends the instant the question does — long before there is any
    speech to detect — so "is it speaking yet?" is the wrong question to gate
    re-arming on. The gate has to be the turn, not the audio.
    """
    out, = _mic("""
      _voiceOn = true;
      startListening();
      mics[0].onstart();
      heard(mics[0], 'how many teams are in the WNBA', true);
      // onend has already fired from stop(); give the re-arm timer its moment.
      tick(400);
      console.log(JSON.stringify([{ sent: sends.length, mics: mics.length, busy: _voiceBusy }]));
    """)
    assert out["sent"] == 1, "the final transcript was not sent"
    assert out["busy"] is True
    assert out["mics"] == 1, "the microphone reopened during the reply"


def test_the_microphone_comes_back_once_the_turn_is_released():
    """The other half: shut for the turn, open again after it.

    A guard that never lifts is the same bug in the other direction — one
    question, then a dead button that still says it's on.
    """
    out, = _mic("""
      _voiceOn = true;
      startListening();
      mics[0].onstart();
      heard(mics[0], 'hello', true);
      _voiceBusy = false;        // what endVoiceTurn does when speech drains
      startListening();
      console.log(JSON.stringify([{ mics: mics.length, started: mics[1].started }]));
    """)
    assert out["mics"] == 2
    assert out["started"] is True


def test_a_silent_turn_reopens_the_microphone_without_being_asked():
    """Nobody spoke: the recogniser closes on its own after a few seconds.

    If that didn't re-arm, pausing to think would end the conversation, and the
    button would sit on "Listening…" with the microphone shut.
    """
    out, = _mic("""
      _voiceOn = true;
      startListening();
      mics[0].onstart();
      mics[0].onerror({ error: 'no-speech' });
      mics[0].onend();
      tick(400);
      console.log(JSON.stringify([{ mics: mics.length, on: _voiceOn, alerts: alerts.length }]));
    """)
    assert out["mics"] == 2, "a quiet moment ended the loop"
    assert out["on"] is True
    assert out["alerts"] == 0, "silence was reported as an error"


def test_a_refused_microphone_turns_voice_off_and_explains():
    """Permission denial is permanent until the user changes something.

    Re-arming into it would loop forever on an error the page cannot fix, so this
    is the one case that has to stop and say what to go and do.
    """
    out, = _mic("""
      _voiceOn = true;
      startListening();
      mics[0].onstart();
      mics[0].onerror({ error: 'not-allowed' });
      tick(400);
      console.log(JSON.stringify([{ mics: mics.length, on: _voiceOn, said: alerts[0] || '' }]));
    """)
    assert out["mics"] == 1
    assert out["on"] is False
    assert "microphone" in out["said"].lower()
    # Naming where to fix it, because the browser prompt is only half the story on
    # macOS — the app needs the permission too, and that dialog appears once.
    assert "System Settings" in out["said"]


def test_it_says_it_can_hear_you_before_it_sends_anything():
    """The reported gap: "I don't even know if it can hear me."

    Interim results arrive well before the final one. Showing them is the only
    evidence a listener has that the microphone is working — otherwise a recogniser
    that is hearing fine but hasn't finalised yet looks exactly like a dead one.
    """
    out, = _mic("""
      _voiceOn = true;
      startListening();
      mics[0].onstart();
      heard(mics[0], 'how many teams', false);
      console.log(JSON.stringify([{ said: said(), sent: sends.length }]));
    """)
    assert "how many teams" in out["said"], "interim speech was never shown"
    assert out["sent"] == 0, "an unfinished sentence was sent"


def test_hearing_nothing_is_reported_instead_of_silently_retrying():
    """`no-speech` must not be invisible.

    Re-arming quietly is correct behaviour and terrible feedback: a microphone that
    hears nothing at all produces this error forever, and the page looked identical
    to one patiently waiting for you to start.
    """
    out, = _mic("""
      _voiceOn = true;
      startListening();
      mics[0].onstart();
      mics[0].onerror({ error: 'no-speech' });
      console.log(JSON.stringify([{
        problem: statuses[statuses.length - 1][1], said: said(), on: _voiceOn,
      }]));
    """)
    assert "Nothing heard" in out["said"]
    assert out["problem"] is True, "not flagged as a problem worth looking at"
    assert out["on"] is True, "silence ended the loop"


def test_a_failure_outlives_the_alert_that_announced_it():
    """An alert is gone the moment it's dismissed — before anyone can act on it.

    The instruction it carries ("check System Settings") takes longer to follow
    than the dialog survives, so it has to stay on the page too.
    """
    out, = _mic("""
      _voiceOn = true;
      startThrows = new Error('nope');
      startThrows.name = 'InvalidStateError';
      startListening();
      console.log(JSON.stringify([{
        said: statuses[statuses.length - 1][0],
        problem: statuses[statuses.length - 1][1],
        alerted: alerts[0] || '',
      }]));
    """)
    assert out["alerted"], "no alert at all"
    assert out["said"] == out["alerted"], "the alert text was not left on screen"
    assert out["problem"] is True


def _meter(script: str) -> list:
    """Drive startMeter() with getUserMedia stubbed to fail.

    Only the failure paths are covered: success needs an AudioContext and a real
    stream, and what matters here is that a refused microphone produces a sentence
    naming what to change rather than silence.
    """
    harness = f"""
      {_js_function("startMeter")}
      let _voiceStream = null, _voiceRaf = 0;
      function _vlog() {{}}
      global.document = {{ getElementById: () => null }};
      // defineProperty, not assignment: node ships its own read-only `navigator`,
      // so `global.navigator = …` is silently ignored and the stub never lands.
      Object.defineProperty(global, 'navigator', {{ configurable: true, value: {{
        mediaDevices: {{ getUserMedia: async () => {{
          const e = new Error('denied'); e.name = FAIL; throw e;
        }} }},
      }} }});
      {script}
    """
    return _run(harness)


def test_a_refused_microphone_names_the_two_places_to_fix_it():
    """On macOS the site permission is only half of it.

    The browser also needs the microphone at the OS level, and that prompt appears
    once and is easy to miss — which leaves recognition failing with no visible
    cause. A message that only says "denied" sends the reader to the wrong dialog.
    """
    out, = _meter("""
      const FAIL = 'NotAllowedError';
      startMeter().then(m => console.log(JSON.stringify([m])));
    """)
    assert "address bar" in out
    assert "System Settings" in out
    assert "NotAllowedError" in out, "the underlying error name was dropped"


def test_no_microphone_at_all_is_a_different_message():
    """"Allow it in the address bar" is useless advice when there's no device.

    Distinguishing them is the difference between a fix and a wild goose chase.
    """
    out, = _meter("""
      const FAIL = 'NotFoundError';
      startMeter().then(m => console.log(JSON.stringify([m])));
    """)
    assert "No microphone" in out
    assert "address bar" not in out


def test_a_recogniser_that_never_opens_is_caught_by_the_clock():
    """The reported symptom, exactly: stuck on "Starting…" with a moving level bar.

    Some browsers ship the interface with no engine behind it — start() returns
    normally and then nothing arrives, not onstart, not onerror, not onend. There is
    no flag to check and no event to wait for, so a timer is the only detector, and
    without it the page waits forever on something that will never happen.
    """
    out, = _mic("""
      _voiceOn = true;
      startListening();
      // No events at all: the whole point. Just let the clock run past the watchdog.
      tick(5400);
      console.log(JSON.stringify([{
        on: _voiceOn, said: alerts[0] || '', shown: statuses[statuses.length - 1][0],
      }]));
    """)
    assert out["on"] is False, "the page waited forever"
    assert "never started" in out["said"]
    # Names the class of cause, because "it didn't work" leaves nothing to try.
    assert "Brave" in out["said"]
    assert out["shown"] == out["said"], "the reason vanished with the alert"


def test_an_engine_that_answers_at_all_is_left_alone():
    """The watchdog must not fire on a browser that works.

    `onstart` is proof of life; so is an error, and so is a close. Any of them means
    the engine is there, and a five-second timer that stops a working recogniser
    mid-question would be worse than the bug it's for.
    """
    out, = _mic("""
      _voiceOn = true;
      startListening();
      mics[0].onstart();
      tick(5400);
      console.log(JSON.stringify([{ on: _voiceOn, alerts: alerts.length }]));
    """)
    assert out["on"] is True
    assert out["alerts"] == 0, "the watchdog fired on a live recogniser"


def test_recognition_starts_before_anything_is_awaited():
    """User activation does not survive an `await`.

    A recogniser started outside the click's own task is ignored *silently* — no
    throw, no error event, no onstart. Claiming the microphone first read better and
    was exactly that bug: the level bar moved, proving the mic worked, while the
    button sat on "Starting…" forever.

    Pinned by source order because there is no way to observe user activation from a
    test, and the failure it causes is indistinguishable from a dead browser.
    """
    src = _js_function("toggleVoice")
    assert src.index("startListening()") < src.index("await startMeter()"), \
        "recognition is started after an await, which drops user activation"
    # The meter is a diagnostic, not a gate: a refused mic is reported by recognition
    # too, and blocking on the meter would mean no voice wherever it isn't available.
    assert "stopVoice(problem)" not in src


def test_the_recogniser_ends_the_turn_on_a_pause_not_on_a_button():
    """`continuous` must stay false: that setting IS the end-of-question detector.

    With continuous = true the recogniser keeps the turn open waiting for a stop()
    that nothing calls, which reads as "it never responds".
    """
    src = _js_function("startListening")
    assert "rec.continuous = false" in src
    assert "rec.interimResults = true" in src
