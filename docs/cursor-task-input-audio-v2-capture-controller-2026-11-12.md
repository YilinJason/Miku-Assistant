# Bounded capture controller — implement only, no runtime wiring yet

Read AGENTS/skills/safety, streamingcoreacceptance and continuousdesign docs. Preserve dirtywork; Cursorauthorsproduction/tests, Codexreviews/independentreplay. No realdevices/app/models/pip/cloud/cleanup/data/memory/commits. No STT/CLIwiring inthisslice; don'tdeclarecurrentusercaptureupgradeduntilnextintegrationaccepted.

## Concreteowner

New small `src/voice/input_capture.py`: one `ContinuousInputCapture` owns exactlyONEthread, incrementalNS, acontext frominjectedSileroowner, puresegmenter, deque(capacity2). Constructor andmoduleimport doNOT importPyAudio/numpy/ORT/startthreads/opendevices. Start/close/status/read_segment publicnarrowAPI. Singleflightstart; startupfailed/closingowner cannotrestart/duplicate. Close setsstop+notifiesqueueandboundedjoins(max2sec), neverfreesnativefromcallingthread; Falsemeansretaincontrollerandretry. Markcomplete onlywhenthreadactuallyexitedANDsource+NS+contextcleanupconfirmed. No daemon-as-cleanupclaim; daemonthreadpermissibleonlyiftrackedpending (unresponsivePortAudio cannotbeforcefreedsafelyinprocess). Constructor/close/start races testedwithbarriers; don'tspawnhelperthreadsperclose/read/utterance.

Injectsourcefactory/clock andDSPfactories ONLYforinternaltests, notuserarbitrarycode/config. Defaultsource: tiny16kmono signed16 PyAudioadapter,512sample readframes32ms, explicitmicrophoneindex suppliedfromvalidatedsettings. PyAudio importedinsideworker only; allcreate/open/read/stop/close/terminate executedonownedthread. Use rawPyAudio.read(exception_on_overflow=True) ratherthan SRwrapperwhichhidesoverflows. Openingpartialfailure stillclosesalreadycreatedPyAudio. FixedPCMgeometry, no inputdeviceautosearchloops/no outputdevice/playback.

Defaultworker maywarmexistingVADowner withonezero512frame BEFOREopeninginput (smallCPUgraphstartup, nostoredrecording), thencreateonecontext; noASR/TTS/RVC/LLMspawn. UseacceptedWebRtcNoiseStream ifNSselectedwebrtc, explicitnonebypasses. Process512rawsamples -> NS(withresidual) -> reblockcleaned512samples (atmost511residual) -> oneVADcallperframe -> segmenter; nofullprefixreplay/duplicatebatchVAD. Defaultsourceblocksforoneframe, fakecanblockuntilreleased; b''/wronggeometry meansdevicefailure, no tightretryspin.

Sourceoverflow: clearcurrentsegment, NS/VADcontext/residual, incrementgeneration/reanchorclock, dropqueuedold-generationsegments. Atmost3resetspercapture thenstabledeviceerror, nobusyloop. Errors/cancel/close cannotpublishlatequeueentries. Onstopexitdiscardunfinishedcurrentutterance (don'tgenerateoldcommandsafterexit).

## Buffers/timing/results

Completed queuecapacity2, dropoldestWHOLEsegment onfull withdropcounter, noPCMorfulltextlogs. `read_segment(timeout, cancel_event)` waitscondition/event withfinitepositivebudget, maxwaitslices50msforcancel; timeoutdistinctfromdevice/notready/cancel. Stalesegments >3sec sincecaptureend dropped ratherthanreturnlatecommand. Validatearguments beforeopeninganything. Controllerstop stopswaiters promptly. No busyloopwhenquiet/stale/failure.

CapturedSegment frozencontainsPCM+sampleevidence,start/endmonotonic captureestimates, sessionid/generation (no usertext). Anchorclockbeforefirstframe orfirstreadendminus32ms; defineestimation/hardwarelatencylimitsaccurately. Reanchoronoverflow, don'ttreatASRreturntimeascaptureend. Queueitems don'tretainthewholecontroller/history.

Segmenterconfigcompatibility: allow four validatedInputAudioSettings admissionvalues(threshold,minvoiced,minconsecutive,ratio) atconstruction, keepingcurrentdefaults; streamingmustnotbypassexplicithigherthresholds. Endhysteresisboundedbelowstartthreshold; keep640msend/12secmax; no newlargeconfigurationframework. Noadaptiveenergyboost/AGC.

## Cleanupfailure

Nativeblockedread/close: callerjoinreturnsFalsewithownerretained, statuspending, no replacementthread/mic. Nativecleanupreturnserror: don'tsetcompleteTrueorlose reference. Keepthread/resourcesinexplicitcleanup-pendingstate; boundedclose retry maywake SAMEownerthreadtoretrycleanup, noCPUspin/unboundedautomaticretry orcross-threadfree. Keepmechanismsminimalandcommentwhy. Afterallresourcesreclaimed thendone/threadexit ->complete. Queueclearedonclose, nativeprocessors/contextreleasedinworkerfinallywithoverallbudget, VADOWNERsessionnotclosedbycapture (STTownerwillcloselater).

## Tests

tests.test_input_capture (allfakePyAudio/Mic/ORT/APM blocked). Oneopenacrossmultipleutterances, queue2dropoldest, stalenessfakeclock, 10kidleframesbounded, noASR/nodiskPCM, overflowresetandboundedfailure, closewhileopen/read/nativeDsp/cleanupfail; startupcancel/latepublish/singleflight, waiterwakeup, retrycompletion. Capturethreaderrorspropagatedasstabletypedstatus; testworkerexceptionsnotlost. Everytest finallyrelease/join/assertnotalive. No50ms sleepsforordering; replaceexistingNoiseStreamclose50mswaittestwithdeterministicbarrier/errorcapturewhiletouchingtests.

SelfcheckexistingboundedsupervisormainPython312, label `input_v2_capture_cursor_nov12ae`, modules tests.test_input_capture tests.test_input_audio_streaming tests.test_input_segments tests.test_noise_suppression tests.test_silero_stt_integration tests.test_silero_vad_adapter tests.test_input_audio tests.test_stt_contract tests.test_numeric_bootstrap, timeout90. No realnative/device/source. STOPforreview; noCLI/assistant/STTintegrationyet. DocumentpublicAPI/limitationsinexistingstreamingADR, notnewgenericframework.
