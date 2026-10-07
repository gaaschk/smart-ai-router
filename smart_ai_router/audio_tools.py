"""Admin-only ElevenLabs voice design and MP3 tools, run on the agent worker thread.

Private designed voices use ElevenLabs directly: OpenRouter's speech endpoint
does not provide voice design or guaranteed access to an account's private voices.
Credentials remain in the server environment, never in the shell or tool arguments.
"""
from __future__ import annotations

import base64
import json
import os
import re
from datetime import datetime, timezone
from uuid import uuid4

import httpx

from smart_ai_router.workspace import resolve_in_workspace

_BASE = "https://api.elevenlabs.io/v1"
_MAX_AUDIO = 10_000_000


def _text(args: dict, name: str, minimum: int, maximum: int) -> str:
    value = args.get(name)
    if not isinstance(value, str) or not minimum <= len(value.strip()) <= maximum:
        raise ValueError(f"{name} must contain {minimum}–{maximum} characters")
    return value.strip()


def _id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value):
        raise ValueError("Invalid voice or preview ID")
    return value


def _post(path: str, body: dict, *, audio: bool = False):
    key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    if not key:
        raise ValueError("Custom voice tools require ELEVENLABS_API_KEY on the server. "
                         "An OpenRouter key cannot create private ElevenLabs voices.")
    with httpx.Client(timeout=httpx.Timeout(120.0, connect=15.0)) as client:
        response = client.post(f"{_BASE}/{path}", headers={"xi-api-key": key},
                               json=body, params={"output_format": "mp3_44100_128"} if audio else None)
    if response.status_code != 200:
        detail = response.text.replace(key, "[redacted]")[:500]
        raise ValueError(f"ElevenLabs returned {response.status_code}: {detail}")
    if audio:
        if response.headers.get("content-type", "").split(";", 1)[0].lower() not in ("audio/mpeg", "audio/mp3"):
            raise ValueError("ElevenLabs returned a non-audio response")
        if not 0 < len(response.content) <= _MAX_AUDIO:
            raise ValueError("ElevenLabs returned empty or oversized audio")
        return response.content
    return response.json()


def _metadata(user: str, kind: str, identity: str):
    return resolve_in_workspace(user, f"audio/{kind}/{_id(identity)}.json")


def _store_metadata(user: str, kind: str, identity: str, data: dict):
    path = _metadata(user, kind, identity)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _download(user: str, path: str, data: bytes, register_file) -> str:
    target = resolve_in_workspace(user, path)
    if target.suffix.lower() != ".mp3":
        raise ValueError("Audio output path must end in .mp3")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    identity = register_file(data, target.name, "audio/mpeg")
    return f"[{target.name}](/v1/files/{identity}/content)"


def execute_audio_tool(user: str, name: str, args: dict, register_file) -> str:
    if user != "admin":
        return "Error: Audio tools are limited to the admin key."
    try:
        if name == "list_audio_voices":
            directory = resolve_in_workspace(user, "audio/voices")
            voices = [json.loads(resolve_in_workspace(user, f"audio/voices/{p.name}").read_text())
                      for p in sorted(directory.glob("*.json"))] if directory.exists() else []
            preview_dir = resolve_in_workspace(user, "audio/previews")
            previews = [json.loads(resolve_in_workspace(user, f"audio/previews/{p.name}").read_text())
                        for p in preview_dir.glob("*.json")] if preview_dir.exists() else []
            previews = sorted((p for p in previews if not p.get("voice_id")),
                              key=lambda p: (p["created_at"], p["preview_number"]))
            return json.dumps({"voices": voices, "previews": previews})
        if name == "design_voice":
            description = _text(args, "description", 20, 1000)
            if register_file is None:
                raise ValueError("Audio downloads are unavailable for this request")
            result = _post("text-to-voice/design", {
                "voice_description": description, "model_id": "eleven_ttv_v3",
                "auto_generate_text": True,
            })
            previews = result.get("previews") or []
            if not previews or len(previews) > 3:
                raise ValueError("ElevenLabs returned no previews or too many previews")
            decoded = []
            for preview in previews:
                encoded = preview["audio_base_64"]
                if len(encoded) > _MAX_AUDIO * 4 // 3 + 4:
                    raise ValueError("Voice preview exceeds the audio size limit")
                data = base64.b64decode(encoded, validate=True)
                if not data or preview.get("media_type") not in ("audio/mpeg", "audio/mp3"):
                    raise ValueError("ElevenLabs returned an invalid MP3 preview")
                decoded.append((_id(preview["generated_voice_id"]), data))
            output = []
            created_at = datetime.now(timezone.utc).isoformat()
            for number, (generated_id, data) in enumerate(decoded, 1):
                preview_id = uuid4().hex
                link = _download(user, f"audio/previews/{preview_id}.mp3", data, register_file)
                _store_metadata(user, "previews", preview_id, {
                    "generated_voice_id": generated_id, "description": description,
                    "preview_id": preview_id, "preview_number": number,
                    "created_at": created_at, "download": link,
                })
                output.append({"preview_id": preview_id, "download": link})
            return json.dumps({"previews": output, "preview_text": result.get("text", ""),
                               "next_step": "Ask the user which preview to save, unless they asked you to choose."})
        if name == "save_voice":
            preview_id = _id(_text(args, "preview_id", 1, 100))
            voice_name = _text(args, "name", 1, 100)
            metadata = _metadata(user, "previews", preview_id)
            preview = json.loads(metadata.read_text(encoding="utf-8"))
            if preview.get("voice_id"):
                return json.dumps({key: preview[key] for key in ("voice_id", "name", "description")})
            voice = _post("text-to-voice", {"voice_name": voice_name,
                "voice_description": preview["description"],
                "generated_voice_id": _id(preview["generated_voice_id"])})
            voice_id = _id(voice["voice_id"])
            saved = {"voice_id": voice_id, "name": voice_name, "description": preview["description"]}
            _store_metadata(user, "voices", voice_id, saved)
            _store_metadata(user, "previews", preview_id, {**preview, **saved})
            return json.dumps(saved)
        if name == "generate_audio":
            text = _text(args, "text", 1, 5000)
            voice_id = _id(_text(args, "voice_id", 1, 100))
            path = args.get("path") or f"audio/speech-{uuid4().hex}.mp3"
            target = resolve_in_workspace(user, path)
            if target.suffix.lower() != ".mp3":
                raise ValueError("Audio output path must end in .mp3")
            if register_file is None:
                raise ValueError("Audio downloads are unavailable for this request")
            data = _post(f"text-to-speech/{voice_id}", {"text": text, "model_id": "eleven_v3"}, audio=True)
            return "Created audio: " + _download(user, path, data, register_file)
        return f"Error: Unknown audio tool {name!r}"
    except httpx.HTTPError as exc:
        return f"Error: ElevenLabs request failed ({type(exc).__name__}). Try again shortly."
    except (ValueError, OSError, KeyError, TypeError) as exc:
        return f"Error: {exc}"
