# WebRTC NS slice — accepted, continuous capture not implemented by this slice

Cursor authored code/formal tests; Codex reviewed source/callers, required strict-null and NS diagnostics fixes, removed redundant per-frame conversions, then independently validated.

Changes: default `voice.stt.input_audio.noise_suppression: webrtc`, optional `none`; fixed local WebRTC NSlevel1+HP before Silero admission and samecleanedPCM to Paraformer. AEC/AGC disabled. No torch/GPU/thread/process/cloud/download added. Processor short-lived per independent utterance, released on success/error/cancel. Explicit readiness imports package without constructing processor. DirecttrustedAudioData/disabledguard remain compatible. MainPython312 installed verified official `pywebrtc-audio==0.2.0` wheel with --no-index/--no-deps; existingnumpy2.3.5 retained. ASR/RVC/Cosy environments untouched.

Independent evidence:

- `input_v2_ns_independent_nov12t`:252fake tests/15modules OK, elapsed13.094s, WAV728→728, no devices/nativeprocessing.
- `input_v2_ns_owned_native_nov12u`: actualmainbootstrap + realNS/Silero/STT + fakeASR,25/25syntheticnoise rejected,9/9fixedRVC controls admitted,34/34guardinputs changed byNS, exactcleanedbytes toASR verified, modelweakref reclaimed onshutdown. Report `tmp/silero_stt_native_20261112_30546456aac1.json`. Processprivate22,614,016→64,761,856bytes, afterclose56,123,392. Not a claim that libraries immediatelyreturnallallocatorpages.
- `input_v2_ns_lifecycle_nov12v`:501actualnativeDSP instances created/reclaimed (weakrefsNone), including cancellationduringfactory;500shortutterances private26,656,768→26,787,840bytes (~128KiB increase);200msutteranceNS wallp50.208ms/p95.257ms. Report `tmp/ns_reclaim_20261112_5a79bd8c7387.json`.
- Candidate10,000frame steadyprocessorprivateunchanged; fixedsyntheticstationarynoise attenuation criterion passed. Report `tmp/webrtc_ns_candidate_20261112_184fa1aab3d5.json`.
- Scopedgitdiffcheck passed (onlyLF/CRLFwarnings). No commit/push, formaldata/memoryuntouched, no mic/playback/ASR/LLM/TTS/RVC launch.

Limits: no actualroom/micaccuracy guarantee, no proof of speakeridentity, acousticAEC or semanticturnending. CurrentNS resets eachutterance and SRcapture stilluses1.5sec pause. Nextstep is streamingcore→boundedcapture→capture/playbacktiming, eachreviewed separately. See `docs/design-input-audio-continuous-2026-11-12.md`.
