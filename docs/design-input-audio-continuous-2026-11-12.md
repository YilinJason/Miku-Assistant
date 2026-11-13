# Continuous input design — pending implementation after NS acceptance

User wants open-source-informed local improvements without large sustained resource costs. PersonaPlex demonstrates continuous audio timing and browser preprocessing; Miku retains separate Paraformer/LLM/TTS/RVC. This design is not a claim of implemented capture, acoustic AEC or user-room accuracy.

## Small, sequential slices

1. Complete NS utterance slice and its independent integration first.
2. Add/test streaming DSP+Silero state and deterministic bounded segmenter with fake frames/fixed files. Do not activate microphone yet. One CPU model/session shared by its owner, no per-frame session creation/replaying growing prefixes.
3. Add exactly one owned capture thread for duplex voice session, frame queue/segment queue bounded, session begin/end APIs and teardown; collect while ASR runs. Keep serial/provider compatibility. Activate only after independent lifecycle/resource replay.
4. Carry capture timestamps through recognition and use actual playback timing/text for echo decisions. Separate suspected speech from confirmed interruption. Acoustic AEC requires aligned output PCM reference and actual native processing; timing/text filtering alone is not AEC. Do not call unimplemented restoration resumable playback.

## Streaming contracts to finalize in next task

- Capture16k mono signed16. WebRTC NS consumes160sample10ms frames WITHOUT repeatedly padding each input chunk; buffer at most159 residualsamples. Silero consumes512sample32ms frames, state/context preserved within stream, at most511 residualsamples. No duplicated inference on rolling history. Batchscore behavior remains independent/reset.
- One thread owns DSP stream state. Per-frame VAD uses the same verified CPU session as batch, state arrays separately owned by stream, boundedconstantmemory; checked cancellation, busy/closing error and no post-close success. Reject unsupported geometry before native init. No torch/GPU/newprocesses or default rawrecording.
- Segmenter pure deterministic frame input/probability+monotonic sample clock. Retain256ms pre-roll, confirm >=96ms consecutive probabilities>=.6, end after640ms sustained quiet with lower hysteresis boundary (provisionally.45). Keep minvoiced180ms/consecutive90ms/fraction.12 final admission; short utterances tested rather than raising thresholds. Provisional values need fixedcorpus and actualroom A/B.
- An utterance is capped12sec including pre-roll, never exceedParaformer15sec protocol. Longcontinuousspeech splits at bound with clear sequence semantics. Idle ring buffers never grow. Completed-segment queue capacity2; onfull dropoldestwhole segment, countdrops/noPCMlog. Reject stalequeuedsegments by capture-end clock (provisional3sec max) rather than responding to old commands late.
- Structured segments include PCM, VAD evidence, start/end monotonic timestamps, session/generation identity; timestamps refer to capture, not recognition-return. A dropped/gapped input resets stream DSP/VAD+currentsegment, do not splice unrelated speech. No model-only text invents user evidence.
- Capture thread opens/read/closes microphone in the SAME owner thread; cancellation checked everyframe. Shutdown only setsstop and boundedjoins; never close/free live nativeinput from another thread. If native read/exit is stuck, retain owner, reportpending, do not launch replacement or pretend reclaimed. No unbounded thread pool/retry. Providerfake and serialbehavior remain available.
- Explicitbegin/end on duplex VoiceSession; exit/modeswitch/error/KeyboardInterrupt allstopcapture in finally. No backgroundrecording in textmode/aftervoiceexit. Assistant delegates public lifecycle methods; CLI does not poke adapterprivateflags. Engine shutdown firstendscapture then VAD, and remainsretryable ifcapturepending.

## Test boundaries

Fakeclock/source/nativefactory tests before device use. Deterministic barriers for construct/open/read/close cancellation and callbacksaftershutdown; everythreadexceptions propagated and finallyjoined. Fixedfile framed-vs-batch probability equivalence; NS chunkboundary invariance; syntheticnoise vsquiet/lowgain speech, no falseLLM/memory/tool calls. Longsynthetic capture/ASRstall tests for queue2/idlebuffers bounded and no native-state growth; onemicrophoneopen acrossmultipleutterances. Root may run bounded fixedfile CPU probes, user self-starts realmic test after phaseacceptance. Never equate fakeframe duration with realrealtime hardware latency.
