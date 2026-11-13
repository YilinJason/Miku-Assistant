# Capture controller R1 — fresh Cursor author context

This controller is an UNACCEPTED, UNWIRED draft. Previous author session was interrupted after it launched two raw unittest commands outside the authorized bounded supervisor; both processes and Cursor were verified exited. Do not repeat raw tests, infinite fake PCM padding, or turn failures into permissive assertions. User requires Cursor author, Codex review and independent gates; no production changes by Codex. Preserve huge dirty workspace. Read AGENTS, relevant skills, safety and `docs/cursor-task-input-audio-v2-capture-controller-2026-11-12.md`; focus ONLY on this controller plus small segmenter config/tests. Existing accepted NS/Silero streaming core is documented in its acceptance file.

Independent reviewer fake gate `input_v2_capture_contract_nov12af` ran3tests: all3fail (2failures,1error). Root probe `tmp/codex_capture_contract_2026_11_12.py` is read-only to Cursor; DO NOT run/edit it. Codex will replay after your STOP.

## 1. Resource close failure must remain owned

Current worker finally ignores literalFalse from source/NS/contextclose, catches and hides exceptions, drops localreferences and exits. Outer close returnsTrue merely because threadexited, evenwhenpending flagtrue. PyAudio `_safe_teardown` swallowsstop/close/terminate failures and clearsreferences regardless. Root source.closeFalse repro shows workerexits and cannotretry cleanup.

Implement one explicit cleanup path retaining source/ns/context on SAMEworker stack until confirmedclosed. Attempt each independently (onefailure mustnot skipothers); clearlocalreference ONLYafterliteralTrue for adapterclose. If anythingremains, setcleanup-pending and wait on a retryEvent; next close wakes SAMEworker toretry. NO busyloop, no unbounded automaticretry, no helperthread perclose, no cross-thread nativefree. Caller close usesone0..2sec deadline and joins; Falsewhileworker/cleanup remains, retainthread/ref. Complete True ONLYafterallresourcesreleased and threadactuallyexited. Clearqueue atcloserequest evenifpending. A blockednative read/close stayspending; neverforcefree.

PyAudio adapter: if stream.stop throws but stream.close succeeds, stream canbecleared; ifstream.close throws retainit, don'tterminatePa untilstreamreclaimed. Ifterminate throws retainPa. Python nativeclose returnsNone-on-success; adapter.close converts noexception + allrefsNone toliteralTrue. No false-success teardown or dropping partiallyopenedPa. Test failures separately with fakePyAudio objects, no realdevice.

## 2. Start/close/state races

Default microphone_device_index=None MUSTwork (omit input_device_index or passNone toPyAudio); current constructorrejectsproductiondefault. Constructorstillnative/threadinert, settingsguardmustenabled+Silero.

Start currentlytimesoutbutdoesnotcancel, so lateopen startsrecording aftercallerwas toldfailure (rootproved). On starttimeout requeststop, keepownerpending, no late reads/publication; sourceopencannotbeforceinterrupted butworkerchecksstopimmediatelyafteritreturnsandcleansup. Checkstop before/after warm/factory/open andeachnativecall; pass selfstop view toNS/VAD. Move `_running` transition intoworker aftersuccessfulopen underlifecyclegate; start mustnotsetrunningTrue afterworkeralreadyfailed/exited/closebegan. One attempt percontroller; cannotspawnreplacement merelybecausethreadended inerror. `.close(timeout_sec=0)` isvalidnonblocking; rejectbool/NaN/outside0..2, don'tsilentlyclampinvalidvalues. Thread.start failure restoresstate withoutnewthreads. No lockheldacrossnative orjoin.

## 3. Wait/queue/error semantics

read_segment must returndevice/worker errors promptly (currentlyignoresboth andtimesoutforever), stop/closed cannotreturnqueuedoldsegments. Useactualmonotonicwallclock for waitdeadline; injectedcaptureclockforstaleness/timestampestimates only (a frozenfakecaptureclock mustnotmakea finitewaitinfinite). Queueconditioncheck/enqueue andstop/generationreject coordinated, no latepublishrace. Queuecap2/dropoldestwhole; statusreadsconsistent, no lock-orderdeadlocks. Notification/error wakeswaiters. No literal'reason ok' withNone segment.

Tests mustnot padforeverwithout blocking; scriptedfake endswithanEvent-controlledread. Emitrealvoiced/quietpatterns so6positive+20quiet actuallycloseutterance; constant.95 for40frames cannotemit. Usebarriers/fakeclock forordering, everytest finallyreleasesclosesjoinsandasserts noownedthreadalive. Add realORT/APM blockers aswellasPyAudio/Mic. Do nothidefailingtestswithinfiniteframes.

## 4. Stream continuity/timing/overflow

Anchor atFIRSTRAWread-endminus32ms BEFORENS/VAD, notfirstdecodedframe afterDSP delay. Afteroverflow resetanchor, segmenter, cleanedresidual AND freshNS/VADcontext; clearold-generationqueue. Checkeveryclose/resetresult; ifcannotreleaseoldcontext failclosedandretainownership, don'treplaceit. Only reset on genuinePyAudio inputoverflowerrno(paInputOverflowed); otherOSError isdevicefailure, not3automaticretries. Max3resets, no tightloop. Afterstop/failedVAD breakouterloop, notjustinnerframe loopwhilecontinuingrawreads. Rawexactbytes1024; nativeerrors neverenqueue.

Warm ownerbatch withzero512 BEFOREcreatingfreshcontext so syntheticwarmframe doesn'tpolluterealstreamstate. Injectedfakeowner canprovide score_pcm; no nativewarm outsideworker.

## 5. Admission config compatibility and test quality

admission_config_from_input_audio currentlyleavesendthreshold.45, invalidatingvalidconfiguredstart<=.45; compute boundedlowerhysteresis (includevalidthreshold0case explicitly). ValidateSegmenterAdmissionConfig fieldtypes/finiteness/ranges/crossfield constraints clearly, keep existingdefaults and honorhighermin/ratio. Avoid secondgenericconfigframework; prefer reusevalidatedInputAudioSettings values. Include defaultNone, nondefaultthreshold/min, stale/queue, 10kidleframesbounded, cleanupfailretries and latestarttimeout tests.

Replace old 50ms NScloseorderingtest with deterministicbarrier and capturedexceptions/finallyjoin asoriginaltaskrequired.

## Only permitted self-check

Use MAIN `C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 tmp/codex_scoped_test_gate_2026_11_02.py --label input_v2_capture_r1_cursor_nov12ag --modules tests.test_input_capture tests.test_input_audio_streaming tests.test_input_segments tests.test_noise_suppression tests.test_silero_stt_integration tests.test_silero_vad_adapter tests.test_input_audio tests.test_stt_contract tests.test_numeric_bootstrap --timeout 90`.

Unique labels forreruns; failedartifactsretained. No rawunittest/pytest, rootprobes, realdevices/models/app/pip/data/memory/cleanup/gitmutations. No STT/CLI wiring yet. STOP afterpass withactualfiles/gatereport/limitations. Do not edit Codex review docs.
