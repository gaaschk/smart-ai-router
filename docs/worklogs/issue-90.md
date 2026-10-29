# Issue 90 checkpoint

Base main82bdea301735798e9f127524da80bc25f726b34e. Added sourced API matrix,
troubleshooting, manual verification protocol, README/API links. Corrected voice
warnings to prioritize secure context and distinguish recognition from playback.
Five source-extracted Node regression cases cover secure/missing/supported states.
Native browser inspection blocked: Mac locked and automatic unlock unavailable.
No permissions changed or microphone/model calls made. Major-browser verification
remains an explicit acceptance gate; do not close #90 on mocked tests alone.

Validation:38 focused tests passed; full offline suite1350passed1deselected
(existing nested macOS sandbox restriction). No live provider or microphone calls.
