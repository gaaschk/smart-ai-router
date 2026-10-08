"""
Model-native speech-to-speech voice — admin only, for now.

The 🎙 Voice button in the chat page does voice *in the browser*: the Web Speech
API transcribes the microphone, the router sees text, and speechSynthesis reads
the reply back. That costs nothing per turn and works with every model the router
can pick, including local Ollama ones. It also sounds like a screen reader,
because the model never hears you and never speaks.

This route is the other trade. Microphone audio goes to an audio-native model,
which answers *in audio* — real prosody, both directions, the thing that makes
ChatGPT's voice mode sound the way it does. What it costs is the router's whole
premise: only a handful of models on OpenRouter emit audio at all (as of writing,
`openai/gpt-audio` and `openai/gpt-audio-mini`; the two `lyria-3-*` entries are
music generation), so a voice turn cannot be routed to the cheapest qualified
model out of hundreds. It goes to the one model that can hold the conversation.

That is why this bypasses CapabilityRouter entirely instead of teaching it about
audio. Routing has nothing to decide here, and pretending otherwise would put a
classifier call in front of every spoken sentence for no benefit. It is also why
the route is admin-gated: a per-turn cost billed in audio tokens, on a model
nobody chose on price, is not something to expose to a self-serve or anonymous
visitor while the spend controls for it don't exist yet.

Tool use is deliberately NOT wired up. Both gpt-audio models advertise `tools`,
so agent mode and web search are reachable from here later — but a tool call is a
silent round trip in the middle of a spoken sentence, and covering that needs the
model to say "let me look that up" first. Separate problem, separate change.
"""
from __future__ import annotations

import base64
import binascii
import json
from typing import Any, AsyncIterator

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from smart_ai_router import settings as _settings
from smart_ai_router.api.proxy import _headers, _resolve_provider
from smart_ai_router.models import UsageRecord

voice_router = APIRouter()

# Streaming is not optional. OpenRouter only emits audio output when
# `stream: true`, and pcm16 is the only format worth asking for: a streamed wav
# or mp3 has to be reassembled whole before anything can play it, which puts back
# the seconds of dead air that doing this at all was meant to remove.
_OUT_FORMAT = "pcm16"

# What gpt-audio emits, and what the page must construct its AudioBuffers at.
# Reported to the browser in a header rather than hardcoded there, so changing
# the model doesn't leave the page resampling to a rate nobody sends any more.
_OUT_RATE = 24000

# Formats to accept for the captured microphone audio. The page sends wav because
# it encodes PCM itself: MediaRecorder's output is webm on Chrome and mp4 on
# Safari, and neither is on any provider's accepted-input list.
_INPUT_FORMATS = frozenset({"wav", "mp3", "pcm16"})

# 4MB of base64 ≈ 3MB of wav ≈ 31 seconds of 48kHz mono PCM, which is what a
# browser's default AudioContext records at (~94s if it opened at 16kHz). The page
# caps its own recordings well inside this; the cap is here for everything that
# isn't the page. A ceiling belongs on this route even though it is admin-only:
# the body is forwarded to a provider that bills per audio token, so a recording
# that never stopped is a money bug rather than merely a large request.
_MAX_AUDIO_B64 = 4_000_000

# Prior turns are replayed as text. Audio is not re-sent: it would be billed again
# on every subsequent turn of the same conversation, at audio-token rates, to tell
# the model something the transcript already says.
_MAX_HISTORY = 20


def _voice_model() -> str:
    model = _settings.get_str("voice_native_model").strip()
    if not model:
        raise HTTPException(
            status_code=422,
            detail="No native-voice model configured (Settings → Voice).",
        )
    return model


def _text_of(content: Any) -> str:
    """Flatten an OpenAI content value to plain text, dropping everything else.

    Prior turns arrive from the chat page, which builds multimodal content parts
    for uploads. An image part sent to an audio model is at best ignored and at
    worst a 400 mid-conversation, and the point of the history here is only to
    keep the model's memory of what was said.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            p["text"] for p in content
            if isinstance(p, dict) and p.get("type") == "text" and isinstance(p.get("text"), str)
        )
    return ""


def _history(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for m in raw[-_MAX_HISTORY:]:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role not in ("system", "user", "assistant"):
            continue
        text = _text_of(m.get("content")).strip()
        if text:
            out.append({"role": role, "content": text})
    return out


def _audio_of(body: dict[str, Any]) -> tuple[str, str]:
    """Validate and return (base64_audio, format). Raises 422 on anything else."""
    audio = body.get("audio")
    if not isinstance(audio, str) or not audio.strip():
        raise HTTPException(status_code=422, detail="Missing `audio` (base64).")
    if len(audio) > _MAX_AUDIO_B64:
        raise HTTPException(
            status_code=413,
            detail=f"Recording too long — the cap is {_MAX_AUDIO_B64} base64 "
                   f"characters, about 30 seconds of 48kHz mono audio.",
        )
    fmt = str(body.get("format") or "wav").lower()
    if fmt not in _INPUT_FORMATS:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported audio format {fmt!r}; "
                   f"expected one of {sorted(_INPUT_FORMATS)}.",
        )
    # Decode to confirm it *is* base64 before spending a provider call on it. The
    # upstream error for a malformed body is generic and arrives after the audio
    # tokens have been counted.
    try:
        base64.b64decode(audio, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(
            status_code=422, detail=f"`audio` is not valid base64 ({exc})."
        ) from None
    return audio, fmt


def _record(cr, request: Request, *, model: str, usage: dict | None,
            status: int) -> None:
    """Attribute the turn in the usage log (best-effort; never raises).

    Cost comes from the provider rather than from `cost_for()`: this model is
    billed in audio tokens at a different rate from its text tokens, so pricing it
    off the text rate would report a voice conversation as almost free. OpenRouter
    returns the real figure when the request asks for it.
    """
    try:
        u = usage or {}
        cr.record_usage(UsageRecord(
            kind="proxy",
            user=getattr(request.state, "user", "") or "",
            key_prefix=getattr(request.state, "key_prefix", "") or "",
            routed_model=model,
            modality="sound",
            domain="voice",
            complexity="native",
            prompt_tokens=int(u.get("prompt_tokens", 0) or 0),
            completion_tokens=int(u.get("completion_tokens", 0) or 0),
            cost_usd=float(u.get("cost", 0.0) or 0.0),
            status=status,
        ))
    except Exception:  # noqa: BLE001 — accounting must not break a finished turn
        pass


async def _relay(upstream: httpx.Response, cr, request: Request,
                 model: str) -> AsyncIterator[bytes]:
    """Forward the upstream SSE verbatim, sniffing the usage chunk on the way past.

    Verbatim because the page needs both halves of every delta — `audio.data` to
    play and `audio.transcript` to show — and re-encoding a base64 payload per
    chunk to hand over less would only add latency to the thing being streamed.
    """
    usage: dict | None = None
    async for line in upstream.aiter_lines():
        if line.startswith("data: "):
            payload = line[6:].strip()
            if payload and payload != "[DONE]":
                try:
                    obj = json.loads(payload)
                except ValueError:
                    obj = None
                if isinstance(obj, dict) and obj.get("usage"):
                    usage = obj["usage"]
        yield (line + "\n").encode()
    _record(cr, request, model=model, usage=usage, status=200)


@voice_router.post("/v1/voice")
async def voice_turn(request: Request):
    """One spoken turn: audio in, audio out, streamed.

    Body: {"audio": "<base64>", "format": "wav", "messages": [prior turns]}
    Returns the upstream SSE stream; see _relay for why it is passed through.
    """
    # "admin" is the env-key identity (SMART_ROUTER_API_KEYS) — the same meaning
    # of admin used by the files and conversations routes. A consequence worth
    # knowing: a router running with *no* keys configured has no admin identity at
    # all, so native voice is off there. Deliberate — "admin only" should not
    # quietly mean "everyone" on the deployment that never set a key.
    if (getattr(request.state, "user", "") or "") != "admin":
        raise HTTPException(
            status_code=403,
            detail="Model-native voice is limited to the admin key for now.",
        )

    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="Body must be a JSON object.")
    audio, fmt = _audio_of(body)
    model = _voice_model()
    cr = request.app.state.capability_router
    base_url, api_key, real_model = _resolve_provider(model, cr)

    messages = _history(body.get("messages"))
    messages.append({"role": "user", "content": [
        {"type": "input_audio", "input_audio": {"data": audio, "format": fmt}},
    ]})

    payload = {
        "model": real_model,
        "messages": messages,
        "modalities": ["text", "audio"],
        "audio": {"voice": str(body.get("voice") or "alloy"), "format": _OUT_FORMAT},
        "stream": True,
        "stream_options": {"include_usage": True},
        # Asks OpenRouter to report what the turn actually cost, which is the only
        # honest number available here — see _record.
        "usage": {"include": True},
    }

    client = httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=15.0))
    req = client.build_request(
        "POST", f"{base_url}/chat/completions",
        headers=_headers(api_key), json=payload,
    )
    try:
        upstream = await client.send(req, stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        raise HTTPException(
            status_code=502, detail=f"Voice model unreachable: {exc}"
        ) from None

    if upstream.status_code != 200:
        detail = (await upstream.aread()).decode(errors="replace")[:1000]
        await upstream.aclose()
        await client.aclose()
        _record(cr, request, model=model, usage=None, status=upstream.status_code)
        raise HTTPException(status_code=upstream.status_code,
                            detail=f"Voice model refused the turn: {detail}")

    async def body_iter() -> AsyncIterator[bytes]:
        try:
            async for chunk in _relay(upstream, cr, request, model):
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(
        body_iter(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-Voice-Model": model,
            # The page builds AudioBuffers at exactly this rate. Sent rather than
            # assumed so swapping the model can't silently leave playback
            # pitch-shifted.
            "X-Voice-Sample-Rate": str(_OUT_RATE),
        },
    )


# ── OpenAI-compatible passthroughs (/v1/audio/speech, /v1/audio/transcriptions)

# These mirror OpenRouter's OpenAI-compatible audio routes, which proxy to
# ElevenLabs (speech + scribe-v2). They are NOT routed — audio models are rare
# and there is no cost tier to pick between — so they go straight through to
# OpenRouter like /v1/voice. Also admin-gated for the same reason: every turn
# bills provider audio tokens, and the router has no spend ceiling specific to
# them yet.
_AUDIO_SPEECH_MODELS = ("elevenlabs/",)
_AUDIO_STT_MODELS = ("elevenlabs/",)


def _audio_model(setting_key: str, detail: str) -> str:
    """Read a required audio-model setting or 422 before any provider call."""
    model = _settings.get_str(setting_key).strip()
    if not model:
        raise HTTPException(status_code=422, detail=detail)
    return model


def _auth_headers(api_key: str) -> dict[str, str]:
    """Headers for a forwarded request — note the absence of Content-Type.

    _headers() in proxy.py pins Content-Type to application/json, which is what
    OpenRouter's /audio/transcriptions wants it NOT to be: that endpoint is
    multipart/form-data, and httpx only emits its boundary when the caller does
    not set the header. For /audio/speech the JSON path is taken by the caller
    passing json=..., which sets Content-Type itself, so it is safe to leave off
    here too.
    """
    return {
        "Accept-Encoding": "identity",
        "HTTP-Referer": "https://github.com/smart-ai-router/smart-ai-router",
        **({"Authorization": f"Bearer {api_key}"} if api_key else {}),
    }


@voice_router.post("/v1/audio/speech")
async def audio_speech(request: Request):
    """Text-to-speech passthrough (OpenAI-compatible shape).

    Mirrors https://platform.openai.com/docs/api-reference/audio/create-speech —
    the caller POSTs JSON {model, input, voice, response_format, speed} and the
    router forwards it to OpenRouter, streaming the audio bytes back verbatim.
    ElevenLabs voices and bracketed delivery tags (e.g. "[whispering]") are
    passed through untouched.
    """
    if (getattr(request.state, "user", "") or "") != "admin":
        raise HTTPException(
            status_code=403, detail="Audio endpoints are limited to the admin key for now."
        )

    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="Body must be a JSON object.")
    if not str(body.get("input", "")).strip():
        raise HTTPException(status_code=422, detail="Missing `input`.")

    model = _audio_model(
        "tts_model",
        "No text-to-speech model configured (Settings → Voice).",
    )
    cr = request.app.state.capability_router
    base_url, api_key, real_model = _resolve_provider(model, cr)
    forwarded = {**body, "model": real_model}

    client = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=15.0))
    req = client.build_request(
        "POST", f"{base_url}/audio/speech",
        headers=_auth_headers(api_key), json=forwarded,
    )
    try:
        upstream = await client.send(req, stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        raise HTTPException(status_code=502, detail=f"Audio model unreachable: {exc}") from None

    if upstream.status_code != 200:
        detail = (await upstream.aread()).decode(errors="replace")[:1000]
        await upstream.aclose()
        await client.aclose()
        _record(cr, request, model=model, usage=None, status=upstream.status_code)
        raise HTTPException(
            status_code=upstream.status_code,
            detail=f"Audio model refused the request: {detail}",
        )

    out_fmt = body.get("response_format") or "mp3"
    media_type = {
        "mp3": "audio/mpeg",
        "wav": "audio/wav",
        "opus": "audio/opus",
        "flac": "audio/flac",
        "aac": "audio/aac",
        "pcm": "audio/wave; codecs=audio/l16; rate=24000",
        "html5": "text/html",
    }.get(str(out_fmt).lower(), "application/octet-stream")

    async def body_iter() -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()
        _record(cr, request, model=model, usage=None, status=200)

    return StreamingResponse(
        body_iter(),
        media_type=media_type,
        headers={"X-Audio-Model": model, "Cache-Control": "no-cache"},
    )


@voice_router.post("/v1/audio/transcriptions")
async def audio_transcriptions(request: Request):
    """Speech-to-text passthrough (OpenAI-compatible shape).

    Mirrors https://platform.openai.org/docs/api-reference/audio/transcriptions —
    the caller POSTs a multipart/form-data body (file, model, prompt,
    response_format, temperature, language, ...) and the router forwards it
    verbatim to OpenRouter, returning the JSON transcription. ElevenLabs Scribe
    v2 adds speaker diarization in verbose_json responses.
    """
    if (getattr(request.state, "user", "") or "") != "admin":
        raise HTTPException(
            status_code=403, detail="Audio endpoints are limited to the admin key for now."
        )

    form = await request.form()
    upload = form.get("file")
    if not upload or not getattr(upload, "filename", None):
        raise HTTPException(status_code=422, detail="Missing `file`.")

    model = _audio_model(
        "stt_model",
        "No speech-to-text model configured (Settings → Voice).",
    )
    cr = request.app.state.capability_router
    base_url, api_key, real_model = _resolve_provider(model, cr)

    # Forward the multipart form. Content-Type is intentionally NOT set here so
    # httpx generates the correct multipart/form-data boundary; see _auth_headers.
    # `model` is always the router-resolved one, ignoring whatever the client sent,
    # just like /v1/chat/completions ignores the requested model name.
    files: dict[str, Any] = {"file": (upload.filename, upload.file, upload.content_type)}
    forwarded_form: dict[str, Any] = {"model": real_model}
    for k, v in form.multi_items():
        if k in ("file", "model"):
            continue
        forwarded_form[k] = v

    client = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=15.0))
    req = client.build_request(
        "POST", f"{base_url}/audio/transcriptions",
        headers=_auth_headers(api_key),
        files=files,
        data=forwarded_form,
    )
    try:
        upstream = await client.send(req)
    except httpx.HTTPError as exc:
        await client.aclose()
        raise HTTPException(status_code=502, detail=f"Audio model unreachable: {exc}") from None
    finally:
        await client.aclose()

    _record(cr, request, model=model, usage=None, status=upstream.status_code)
    if upstream.status_code != 200:
        detail = upstream.text[:1000]
        raise HTTPException(
            status_code=upstream.status_code,
            detail=f"Audio model refused the request: {detail}",
        )

    return JSONResponse(
        upstream.json(),
        headers={"X-Audio-Model": model},
    )


@voice_router.get("/v1/audio/models")
async def audio_models(request: Request):
    """Lightweight discoverability: which audio models this router can address.

    Admin-gated like the rest. Returns a tiny capability list, not a catalog —
    enough for a UI to populate a dropdown without guessing the provider format.
    """
    if (getattr(request.state, "user", "") or "") != "admin":
        raise HTTPException(status_code=403, detail="Admin key required.")

    out: list[dict[str, str]] = []
    for key in ("tts_model", "stt_model"):
        val = _settings.get_str(key).strip()
        if val:
            base, _, real_model = _resolve_provider(val, request.app.state.capability_router)
            out.append({
                "key": key,
                "value": val,
                "provider": base,
                "model": real_model,
            })
    return JSONResponse(out)
