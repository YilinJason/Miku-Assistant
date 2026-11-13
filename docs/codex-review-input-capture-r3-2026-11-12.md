# Capture R3 — final status aggregation and overflow transaction

Independent5fakechecks pass (`input_v2_capture_contract_nov12am`). RealCPU NS/VAD withFAKEblockingfilesource also passes34cases+100cycles: `input_v2_capture_native_nov12an`, reporttmp/capture_native_20261112_3ef1c3bc8d5e.json,134captureweakrefsreclaimed, repeatprivate60,899,328→60,899,328. Not adevice/ASRtest.

Onefurther independenttest `input_v2_capture_retry_contract_nov12ao` fails (6tests/1failure). Callerclose checksaliveTrue, workerfinishescleanup andclearspending, closerthenwritesoldpendingTrue; retryseesdeadthread+worker_cleanup_okTrue butpendingTrue andrefusessuccessforever. Fix aggregation: confirmedworker_cleanup_ok + no launchinflight + actualthreadexited/noowner mustpermitcompletion regardlessstalehint; clearpending onthatprovedstate. Pendinghint cannotvetoactualreclaim. StillFalsewhilethreadalive orcleanupunconfirmed. Coverstartfailureafterconcurrentclose too; nonewresources meansretrycloseTrue. Keepcompleteclaim tied torealexit, notmereflag.

Two source-reviewed overflow issues toclose before wiring:

- generationincrement/queueclear must be SAMElifecycle→queue transaction; currentlygenerationmodifiedoutsidelock while read_segment usescapturedgenerationunderlock, allowingoldgenerationsegmenttoreturnafteroverflow. Stop andqueueerrors remaincoordinated.
- overflowhelperallocatesnewNS/context then mayraise before returningupdatedrefs toworker, losingpartialresourceownership. Catchordinaryfactory/reseterrors locally andreturnfailureWITH latestownedreferences forfinallycleanup; confirmedclosedoldrefs canbeNone, don'tdropnewnativeobjects. IfoldcloseFalse, noreplacement. No newlifecyclethread or fallbackrawaudio.

Simplifyredundantstart/closebrancheswherepossible; don'taddanotherindependentstickyflag. Keep NS/VAD algorithms/defaults unchanged. Adddeterministicformalregressions; authorrootprobe remainsread-only/notrun. Same9fake modules only, label `input_v2_capture_r3_cursor_nov12ap`, timeout90. STOP; no wiring/devices/models/pip/data/memory or otherphases.
