# R1 urgent addendum — remove self-deadlock and unsafe abort helper

Codex paused the author/gate after observing a definite self-deadlock in the R1 draft: `with self._continuous_capture_lock:` then `_continuous_capture_epoch_matches(epoch)` acquires the SAME non-reentrant Lock. Gateax was interrupted; processinventory confirmsnoCursor/testleft. Don'tclaimitpassed. Withinheldgate compareepochfielddirectly; do not use RLock merelytohideunnecessarynestedlocking. Allnative/start/close waits outsidegate.

`_continuous_capture_abort_epoch(epoch)` is unsafe: it snapshotsCURRENTcapture (maybeanewer session), closesit, ignoresFalse/errors, thenunconditionallydropsreference. Removeit. Abort/cleanup musttargetthe EXACT publishedcontrollerreference, neverwhateveriscurrentlater. Useonesmallidentity-safeclosehelpersharedwithstop/shutdown. It mustcatchordinaryerrors ->False, retainexpectedcurrentref onFalse/nonbool/error, clearONLYidentity-matchedref onliteralTrue. Do notclose/clearnewsessionwhenolderstartobservesepochmismatch. Replacementisallowedonlyafteroldcontrollerconfirmedclosed, sothere shouldnotbeneedanunboundedretiringlist.

R1 draft's successafter`publish_ref.start()` mustrecheckepoch/globalclose/identity andonabort useexpectedownerretentionrule. Preserveall5originalreviewfixes andremainingrequirements. StopViewmustbewrappernotEvent; oldcontrollerFakeclose shouldsetitsstopEventtopermitrealisticcanceltests.

CLIloop extractiondirectiongood; ensureordinarybegin/loop exceptionsbecomestt_errorwhilecleanupalwaysruns; endfailureoverridesunsafe_pause anddoesn'tskipreplycleanup. No rawexception/PCM/textlogs. Avoidunusedholder/state ifsimplerpublicsessionwrapperworks.

Use NEWsupervisorlabel `input_v2_wiring_r1_cursor_nov12ay` (axpartialartifactsretained), same19fake modules, timeout90. No rootprobes/rawunittest/realmodels/devices/app/pip/data/memory/nextphase. STOPafterpass. ReadthisaddendumandR1reviewonlyasnecessary, don'tre-readmonthsofhand-offhistory.
