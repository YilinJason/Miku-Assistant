# Capture R2 — precise remaining lifecycle defects

Independent first3checks nowpass: `input_v2_capture_contract_nov12ai`. Extended5check gate `input_v2_capture_extra_contract_nov12aj` confirms:

1. close during Thread.start publication raises `RuntimeError: cannot join thread before it is started`.
2. source.close returning1/dict is accepted andownerthreadexits, droppingunconfirmednativeownership.

Fix these and the source-reviewed issues below before any wiring/native tests.

## A. Atomic thread launch ownership

Track an explicitstarting flag/token underlifecyclegate beforepublishingthread. Thread.start staysoutsidegate; close(timeout0) returnsFalsepending whilelaunchinflight, neverjoinunstartedthread. Secondstart refusesexistingattempt. Onstartfinish/failure finalizeflag undergate, checkstop and cleanup state; latestart mustnotrecord. Thread.startfailure has noresources and mustnotleavefalselypendingforever. Don'tholdgateacrossablockableThread.start (reviewerprobe intentionallyblocksit).

Derivepending/completion fromownedlaunch/thread/resources; do notlet starttimeout/error overwritecleanupsuccess flagafterworkeralreadyfinished. Closebeforestart =>True, closeafterfailedcleanedstart =>True; neverTruewhilelaunchinflight/nativeowned. Keepstatecountsmall.

## B. LiteralTrue and no TypeError retry

EveryCaptureSource/NS/context adapter.close mustreturnliteralTrue toreleaseitsreference;1/dict/None/exceptionstaypending. `_adapter_close` currentlyreturn`is not False` andretriesTypeError withdifferentkwargs; removeboth. Allcurrentinternaladapters support `close()`; callonce, ordinaryexceptionFalse, noautomaticretryofinternalTypeError. ForboundedDSPclosekwargs normalizeonce byknowninterface ratherthantry-again-on-error. Pyaudio's NATIVEcloseNone conversionbelongsinsidePyAudioCaptureSource only.

Cleanupretry event has lostwake: currentloopsetspendingthen `.clear()` caneraseconcurrentcaller.close signal. Clearretryevent BEFORE eachcleanupattempt, thenattempt, thenifpendingwaitwithoutclearing; requestduringattemptremainsset. Allresourcesattemptedindependently eachpass; literalTruesclearrefs, no CPUspin. Retainlocals onworker; callerclose bounded.

## C. Stop/queue/timing correctness

`_enqueue_segment` andread checkstop/generation beforeseparatelylockingqueue: close/resetcanraceandletlateaudioappearafterqueueclear. Coordinatechecks withlifecycle+queueconsistentlockorder, recheckstop/currentgeneration underqueueownership; readmustnotreturnqueuedsegmentafterstop. No nestedreverse-orderlocks. Afterread_frame returns, checkstop beforeNS/nativework. Passselfstop toNS/VAD calls. Beforemarkingrunning checkstop undergate. Rejectnotenabledsettings upfront. Timinganchor FIRSTrawread-endminus32ms correctlyimplemented—preserve.

Warm currentlyusesstreamcontext.process(zero), pollutingrealstream recurrentstate. Warmowner.score_pcm(zero) first, THENcreatefreshcontext. Overflow reset shouldcloseoldcontext confirmedTrue andcreatefreshcontext; do NOT warm artificialzero intotheactivecontext. Failedclose staysownedforcleanup, noreplacementonfailure. Resetanchor/segment/residual/oldqueue/generation together. Stopatresetlimit BEFOREcreatingnewnativeobjects justtodiscardthem.

## D. Tests and concise quality

Do not use `pad_forever` tightloop (explicitlyforbidden). Scriptedsourceend mustBLOCKonEvent; callerfinallyreleaseit andclose/join. FakeVAD returnsprobabilitiesbyframe tags (zeroquiet), so6speech+20quiet emits withnativefreebehavior. No tests asserting 'atleast1' whencontractrequires2, no staleNone+'ok' allowed. No rawunittest/rootprobes. Add exact threadlaunchbarrier, literalTrue variants and TypeErrorcallcount1, pendingretry wake, stopafterread/no lateenqueue, configuredadmission thresholds andPyAudioclose/terminatefailuretests. Restoreallnativeblockers. Replace50msNScloseorderingtestwithbarriers asrequested earlier, notnewteststhatrepeattherace.

SegmenterAdmissionConfig currentlyonlychecksfinite/relative thresholds; validateallfieldtypes/ranges/ratios/minconsecutive<=minvoiced withstableValueError, or restrictconstructiontoalreadyvalidatedInputAudioSettings withoutpromisingunvalidatedpublicconfig. Supportthreshold0..1 anddocumentthreshold0 cannotfindquiet (maxdurationstillbounds).

Only selfcheck documented9fake modules viaexistingboundedsupervisor, newlabel `input_v2_capture_r2_cursor_nov12ak`, timeout90. MainPython312. No rootprobes/models/devices/app/pip/data/memory/otherphases. STOPforreview.
