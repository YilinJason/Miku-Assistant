# Capture controller milestone — accepted, still not wired to microphone sessions

Cursor authored controller/formal tests. Codex reviewed lifecycle, buffer bounds, cancellation, source exceptions, overflow and thread publication. Earlier draft's unsupervised fake runs were interrupted and verified exited; all acceptance evidence below uses the bounded supervisor. Controller imports/constructs without device/model/thread side effects.

- `input_v2_capture_contract_nov12as`:6independent fake lifecycle checks passed, including defaultNone, thread-launch close, strictTrue/nonbool returns, sameworker retry, startup timeout and stalepending completion. Failed previous gates af/aj/ao retained.
- `input_v2_capture_independent_nov12at`:297fake tests/18targetedmodules passed,14.241stesttime, WAV728→728 unchanged.
- `input_v2_capture_native_nov12an`: realCPU NS+Silero, FAKEblockingfilesource/virtualclock,25noise rejected and9fixedRVC controls admitted; emittedbytes match cleanedsourceindices. One sourceopen andsameworker close eachlifecycle.100additionalcycles private60,899,328→60,899,328bytes;134controllerweakrefs reclaimed, VADcontextreleased aftereachcapture, finalsessionweakrefgone. Reporttmp/capture_native_20261112_3ef1c3bc8d5e.json.
- R3 followup after native replay changed only close-state aggregation and overflow transaction/partial-failure ownership; finalfake/full regression covers these. No repeated unchanged native workload needed before wiring.

Controller queuecapacity2/dropoldestwhole; stale>3sec discarded; raw512sample/32ms frames, optionalWebRTC NS -> sharedCPU Silerocontext -> boundedsegmenter12sec/640msquiet. Stop requests boundedjoin0..2sec; nativeblockedclose/read remainsownedpending withoutreplacement; ordinaryclosefailures retryonSAMEworker and neverclaimsuccessuntilallcloseconfirmed+threadexit. SourceNone selectsWindowsdefaultdevice; genuineoverflowresetbounded3 withnewcontext/generation/reanchor. No actualmic/ASR/playback test wasrun.

Next: narrowSTT/assistant/VoiceSession lifecycle wiring, optionalcapturetimestampsinRecognitionResult, fake/nativefixedfileintegration. Do not claim this controller already changed current user's capture behavior. Formaldata/memory, modelenvironments/RVCvoice untouched; no commit/push.
