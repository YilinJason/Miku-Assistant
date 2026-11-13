# Capture wiring — make accepted bounded controller usable in duplex voice mode

Read AGENTS/skills/safety, controller/streaming/NS acceptance docs and current callers. Preserve dirtytree; Cursorauthorscode/formaltests, Codexreviewsindependently. No app/device/realmodels/pip/cloud/cleanup/data/memory/gitmutations. Only documentedfakegate. Do not edit/run Codexrootprobes. Complete this slice thenSTOP; actualplayback-reference/AEC/interruptrecovery isNEXTslice.

## Config and scope

InputAudioSettings add strict `capture_mode`: `continuous` default, `utterance` compatibility. Missinglegacykeydefaultscontinuous; explicitnull/bool/unknownreject. Configdefault/YAML/docs consistent. Actualcontinuouscapture applies ONLY explicitduplexvoicesessions, localparaformer, guardenabled, Silero backend, realproviderNone. Serialtext/interactivepaths and injectedproviders remaincompatible; explicitutterance/backendwebrtc/disabledguard useacceptedlegacycapture, no backgroundrecording. No mic/model/thread starts atimport/constructor/defaulttextstartup.

## STTEngine lifetime and consumption

Add narrowpublic `start_continuous_input()->(bool,str)`, `stop_continuous_input()->bool` withownedcontrollerreference and small lifecycle coordination. Unsupported/compatibilityconfiguration returnsTrue withno controller; actualsetup/startfailure returnsFalse/stablehint, NOTsilentfallbacktorawmic. ValidateexistingsealedParaformerpermission/readiness beforecapture. UseacceptedVADowner; controllerdoesnotspawnASR/TTS/RVC/LLM.

Atmostonecontroller perSTT; coordinatecreation/publication/start withshutdown/stop. No lateowneraftershutdown; pendingstart/close keptowned andblocksreplacement. Controllerconstructorpure; do notholdSTTlifecyclelock acrosscontroller.start/close/nativewait. Startpublication visiblebefore starting thread so stopcanalwaysfindowner; concurrentstop/start cannotdropanunstartedownerandlaterstartit. Return literalTrue onlyconfirmedclosed controller; keepFalse/errorref forretry. Modeexit stop isnotpermanentSTTshutdown, so cleanreenter maycreatefreshcontroller; failedstop forbidsreenter untilretrycompletes.

`recognize(continuous=True)` whilecontrollerowned consumesread_segment, NOT `_recognizer.listen`/newmic. Ifcontrollerpending/error returnNOT_READY/DEVICE_ERROR, neveropenlegacymicasfallback. Maintainexisting `_engine_lock`/recording semantics sooneASRconsumer. SegmentalreadyNS/VADadmitted; send exactsegmentPCM to `_transcribe_paraformer_pcm(...apply_acoustic_guard=False)` toavoiddoubleprocessing. Geometrycheckstillmandatory; recheckownedidentity/session/cancel/closing beforeandafterASR. Rejected/stale/noise neverreachesASR/LLM/tools/memory.

Expose READONLY capturecancelview/property fromcontroller (`is_set` view, notmutatingbusinesscodeprivateEvent) andcombinewithcaller+STTshutdown forASRrequest. A modeexit whileASRpending mustcancel/dropthatresult evenwithoutpermanentengine_shutdown. DirecttrustedAudioData diagnostics retainexistingbehavior.

Shutdown closes/stops captureFIRST beforeownedVAD, thenexistingParaformer/tempfiles. Ifcapturepending, returnFalse/statusincomplete/retainref, don'tfreeVADunderlivecapture. `get_shutdown_status/is_shutdown_complete` includeanycapture/start/retiring ownership evenifParaformerNone. Noearlysuccess fromold `_permanently_closed` flag whileownedcapture remains. Stop helperfailures don'tskipotherownedresources exceptdependencies thatmustwaitforcapture safety.

## Assistant and VoiceSession boundaries

Assistant public `begin_voice_input_session()->(bool,str)` / `end_voice_input_session()->bool` delegatesoptionalSTTmethods; oldinjectedSTTmissingmethods compatibilityTrue/noaction. CLIbusinesscodeusesassistantpublicAPI only, notSTTprivatefields.

VoiceSession.run forduplex attemptsbegin once; beginfailure cleanlyreportsandreturnsstt_error, no fallbackparallelmic. A finally alwaysendsattemptedcapture onnormalexit/modeswitch/STTerror/replyexception/KeyboardInterrupt, includingpartialstartfailure. IfendnotliteralTrue/raises returnunsafe_pause/stablehint, preserveownerforassistant.shutdown retry. Avoidreturn-in-finally swallowingexceptions: storeoutcome andcleanupresult clearly. Serialmode doesnotbeginbackgroundcapture. Oldfakeassistant withoutnewmethods remainscompatible. Don'tsilentlychange stopkeywords/LLM/tool/memory behavior.

## Capture timing plumbing (no echo-policy rewrite yet)

RecognitionResult optional pair `capture_start_mono`/`capture_end_mono` defaultNone. BothorNone, finite nonnegative numbers(excludebool), end>=start; stablevalidation. Successfactorybackwardcompatible; coercionmustpreserveandvalidatepair. SetonlywhenactualCapturedSegment used; don'tinventtimestamps onlegacymic/providers. Assistantcoercion keepsfields. Non-success shouldnotcarryusertext; don'tallowinvalidpair tosuccess. ThesearehostsampleclockESTIMATES, NOT precisehardwaretimestamps/AECproof. Logonlybackend/capturemode/NS fixedsummary andtimings/counts, no PCM/text/userpaths.

Do not change `_run_duplex` echo decision toguessplayback fromthese timestamps yet. Followupslice willtie actualplayedreferences/time. No claim 'fullyreal-time' justfromremoving1.5secSRpause; newsegmenter640msquiet isprovisional.

## Tests and docs

tests.test_capture_wiring (allMic/PyAudio/ORT/APM/nativefactoriesblocked). Covercontrollercreation/startvsstop/shutdownpublication, pendingretry/nostartreplacement, ASRconsumptionnotSRlisten, exactcleanedbytes/noadditionalNS/VAD, ASRmodeexitcancel/lateresultdrop, configurecompat paths, diagnosticsbypass, captureclosebeforeVAD evenwithoutASR, CLIall exitpaths includingbegin/endfailure andserial/providercompat. Deterministicbarriers, captureeverythreadexception, finallyrelease/join/assertnotalive. TestswithlegacyFakeSTT cannotaccidentallyopencontroller ormodels. Addtimingpaircorruption/coerciontests. Configvalidationhappensbeforeassemblysideeffects.

UpdateaffectedREADME/VOICE/streamingADR andhandoffonly, clearlyseparatefixedfile/fakeproof fromactualroomtest. Providequick configfallback `capture_mode: utterance` and usermanualteststeps butdon'tlaunch. No unrelatedrefactor.

SelfcheckMAINPython312 existingboundedsupervisorlabel `input_v2_wiring_cursor_nov12au`, modules tests.test_capture_wiring tests.test_input_capture tests.test_input_audio_streaming tests.test_input_segments tests.test_noise_suppression tests.test_numeric_bootstrap tests.test_silero_stt_integration tests.test_silero_vad_adapter tests.test_input_audio tests.test_stt_contract tests.test_local_asr_final_switch tests.test_paraformer_races_round2 tests.test_assembly_rollback tests.test_assembly_lazy_defaults tests.test_app_voice_activation tests.test_voice_input_gate tests.test_voice_session tests.test_mode_assembly_round2 tests.test_voice_lazy_exports, timeout90. Unique labels forreruns. STOPforCodexreview/nativefixedfilewiringreplay; no followupslice.
