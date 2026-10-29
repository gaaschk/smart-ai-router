# Browser voice compatibility

The 🎙 Voice button combines browser speech recognition with speech synthesis. API
presence is necessary but does not prove that a recognition service works. Typed
chat remains available when voice fails. This guide does not certify old browser
versions; use a maintained browser with current security updates.

## Compatibility matrix

API information checked against [MDN recognition data](https://github.com/mdn/browser-compat-data/blob/main/api/SpeechRecognition.json)
and [MDN synthesis data](https://github.com/mdn/browser-compat-data/blob/main/api/SpeechSynthesis.json)
on 2026-10-29. These are API introduction versions, not tested router minimums.

| Browser/platform | Recognition API | Synthesis API | Router verification in this change |
| --- | --- | --- | --- |
| Chrome desktop / Android | Prefixed from 33; unprefixed from 139 | From 33 | Pending real microphone/playback check |
| Safari macOS / iOS | Prefixed; desktop from 14.1, mobile data mirrors Safari | Desktop from 7; mobile mirrors Safari | Pending |
| Edge desktop | Recognition data mirrors Chromium; engine availability must be checked | From 14 | Pending |
| Firefox desktop / Android | Preview in current data; do not assume enabled in stable builds | Desktop 49; Android 62 | Pending; expect text fallback where recognition is absent |
| Opera desktop / Android | Constructor marked unsupported; interface presence alone is unreliable | Desktop 21; Android unsupported | Pending; expect text fallback |
| Other Chromium builds / embedded webviews | Vendor/service dependent | Platform dependent | Not qualified |

## Requirements and troubleshooting

Use HTTPS or `http://localhost`, activate Voice with a click, and allow microphone
access only when you intend to speak. A LAN HTTP hostname is not localhost. The
UI reports the secure-context problem first, then missing recognition or playback.
It also times out if an exposed recognition engine never starts.

- **No recognition API:** use typed chat or another browser with a working engine.
- **Stuck on Starting:** check browser/service availability and connectivity. An
  exposed API does not guarantee access to the vendor's speech service.
- **Permission denied:** review the site's microphone permission and OS microphone
  access. Do not disable browser security protections.
- **No level movement:** check selected input, mute state, and OS audio settings.
- **No playback:** check volume/output device and installed language voices.
- **Wrong language or variable accuracy:** browser/OS language and engine support
  differ. Try a short phrase before a long session.
- **Assistant hears itself:** voice is half-duplex; recognition should stay paused
  during playback. Escape interrupts speech. Report a regression if it does not.

Browser recognition may send audio to the browser vendor. A local router model
does not imply local speech recognition. The separate admin-only 🔊 Talk feature
sends audio to a model provider and can incur charges; it is not a free fallback.

## Verification protocol and current evidence

For each desktop browser above and iOS Safari / Android Chrome, record exact
browser and OS versions, page origin, API presence, permission state, language,
recognition start/interim/final events, audible reply, interruption, and typed
fallback. Use synthetic phrases and a mock text-model backend. Do not run paid
model-native audio calls. Check secure localhost and unsupported-context warnings,
denied permission, a silent engine, and playback interruption. Obtain authorization
before any new microphone permission or transmission of recorded audio.

This change tests capability messages against the actual JavaScript source under
Node, alongside existing mocked voice lifecycle tests. Those tests are not browser
or microphone verification. On 2026-10-29 native browser inspection was blocked
because the Mac was locked; only the in-app browser was exposed. No cross-browser
pass or minimum supported router version is claimed. Issue #90 stays open until
this checklist is executed and its evidence is recorded.
