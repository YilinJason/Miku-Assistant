# Echo timing correction — narrow, no playback/AEC rewrite

ReadAGENTS/skills/safety andcapturewiringacceptance. ExistinginputNS+VAD+continuouscapturehasindependentproof. This small slice correctsoneknownlatencybug; NOT anacousticAEC/fullPersonaPlexbackend. Preserve dirtywork, Cursorwritesproduction/formaltests, Codexreviews. No devices/app/realmodels/pip/data/memory/gitmutations orrootprobes.

## Bug and bounded fix

`_run_duplex_loop` decides echo_tail from `now - active.finished_at` AFTERASRreturns. RecordingmayoverlapreplybutASR's0.64sec endpoint+decodeexceeds1secgrace, so matchingechoisacceptedasanewturn. NewRecognitionResult captureinterval isavailable; useit.

Give `_ReplyTask` a monotonic `started_at` beforethreadlaunch. Extractsmallpure/privatehelperthatcomputes whethera recognizedcapture overlaps reply ACTIVITY window: `[task.started_at, task.finished_at + grace]` forfinishedtask, or `[started_at, now/ongoing]` fornotdone. Captureintervaloverlap usesstart/endfromRecognitionResult; finitevalidatedpairalreadyguaranteed. Do notreplacecapturetimes withASRcompletionnow. A delayedresult whosecaptureoverlapped finishedreply stillgetsits existingtextreference evenwhencompletionarrivesaftergrace. A user's laterrepeatcapturedAFTERreplyfinish+grace mustnotbefilteredbyoldreference. CapturedBEFOREreplystartedmustnotmatchfuturegeneratedtext. Missingcapturetiming (oldprovider/utterance) preservescurrentwallclockfallback.

Separate concepts: echo/fillergate's`output_active` shouldusewhetherOUTPUTwaspotentiallyactiveDURINGcapture, whereasinterruptingcurrentlyrunningreplystillusesCURRENTtasknotdone. Don'ttreatoldcapturedactivityasproofcurrentreplyrequirescancel ifalreadydone. Gate rejectionneverstartsreply/LLM/tool/memory orinterruptsoutput. Existingexplicitcontrolwordpriority staysunchanged; do notaddblacklist, thresholdincrease, speakeridentityclaim ornewASRrequest.

Thiswindowincludesgenerationandplayback, andreferenceisgeneratedtasktext (existingbehavior). DocumentasCONSERVATIVEreplyactivityestimate, notactualplayedtext/PCM. No newhistoryqueue/tasks/backgroundmodel/dependency; no enlargedgrace tohidebug. Actualplayedreference/AEC andfalseinterruptresume remainfutureworkrequiringseparatedesignandroomaudio.

## Tests

Extendtests.test_capture_wiring ornewtests.test_voice_capture_timing withfakeclock/tasks/results. Cover delayedASRaftergracebutcaptureoverlap ->echo rejected/noreply; captureafterwindow ->normalrepeataccepted; capturebeforereply ->nospuriousecho; activecapture ->gateactive butactualcancelonlyifreplycurrentlyactive; legacytimestampsNonefallback; boundary/emptytext/currentdone. IntegrationfakeVoiceSession usesgatedrecognizer andspyreplyfactory, no native. NativeMic/PyAudio/ORT/APMblocked. Reuseexistingmatchedtextgate, no adhocstringfilter. Threadcleanup deterministic.

## Documentation consistency (mandatory)

README/VOICE/streamingADR stillcontradicttheactualwiring: VOICEsayssingleutteranceonly andcontinuousNSfuture, ADRtop/pending/no-background-threadstatementsstalewhileoneownedcapturethreadisnowwired. RewriteONLYaffectedsections toclearlystatecontinuousduplex vsutterancecompat, realworker/queues/640ms/3sec, exactCPUdependenciesandstartupbudget, localprivacy(norecordingdisk bydefault), usermanualrealroomtestpending, NOacousticAEC/voiceID. Explaincurrenttext/timingechofilterlimitation. Keepfuture3D/Questdecisionsout.

Provide `docs/manual-input-audio-v2-test-2026-11-12.md`: userstartsMAINPython312 main.py --modevoice(orpreviousisolatedconfig), headphonesinitially;2minquiet/fan/keyboard,normal/softshortChinese,3back-to-backquestionsduringdecode,exit/reenter/textmode andresource observation,thenoptional-speaker echotest. Don'tclaimpassuntiluserfeedback. No needinstall/policybypass; capturefallbackyamlminimal. Existingtmpmanualconfigmaymissingnewkeys; defaultsapply, don'ttouchformaldata/memory orwriteuserprivateconfigwithoutneed.

OnlyselfcheckexistingboundedsupervisorMAINPython312, label`input_v2_echo_timing_cursor_nov12bf`, modules newtimingtest(ifcreated) plusall19wiringmodules inpreviousdoc, timeout90. No rawtests/rootprobes. STOPforCodexreview. No otherfeature/actualplayer/TTS/RVCchanges.
