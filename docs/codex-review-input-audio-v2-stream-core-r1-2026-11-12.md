# Streaming core R1 — correctness before device integration

Author gate197passes does not cover the required invariants. Concrete source issues:

## 1. Segmenter duplicates PCM and evidence

feed_frame appends currentrecord topreroll THEN `_open_segment` copiespreroll AND appends triggeragain. Three96ms positiveframes becomefour128ms inbuffer; four128ms positives becomefive160ms, and thresholdcases canincorrectlypass. Maxdurationcontinuation also copiesalreadyemittedpreroll. `_maybe_split_max` can returnNone forrejectedoldsegment aftermutatingstate, thenfeed appendscurrentrecordagain. start/endcounter is no longerconsistentwithactualsampleids.

Fix with simple explicit ownership: eachinputframe occurs exactlyonce inemitted timeline; whenopening copyoneboundedpreroll list withtriggeronlyonce. Whileactive do not retainpastframes forreuse inmaxsplit. Onmaxsplit currentframe startsfreshcontinuation, notoldpreroll, and isnotappendedagain evenifprevioussegmentrejected. Removeunused segment_start field and redundanttruncatefallback; enforceboundbeforeappend. Validate streamgeneration asnonnegativeint excludingbool; document/resetgap clocksemantics. Avoid unnecessarygeneralconfiguration.

Formaltests must encodeunique signed16 sampleid perframe andcompare exactconcatenation/timeline ofALLemissions+finish. Prove128ms/160msactualspeechdoesnotpass180msrequirement,192mspasseswithoutduplicates; maxsplitmorethan2segments withnooverlap/gap, rejectedmaxsegmentcase andtail-silenceboundary. Existingtestnamedwithout_dup onlycheckssize anddoesnotproveitsname.

## 2. NS stream has no synchronization or final rejection

process_chunk/finish canreturnsuccess ifcancel is setinside LASTnativeprocess. close() currentlyreturnsNone anddrops `_processor` whileanothernativeprocessisrunning; `_processing` booldoesnotprotectanything. Failed/cancelled calls leaveusablepartialcontinuity. Factorycanpublishafterclose. Implementone nonblockingoperationlock+closingevent, boundedclose0..2sec literalbool, holdprocessoruntilactiveopfinishes; closepending/retry, nofree/modelrevival. Recheckclose/cancel afterfactory andafterEVERYnativecall andbeforepublish. Error/cancelinvalidatestream; busycannotmutateactivecallstate. `.finish` flushresidualonceandendsstream (idempotentemptyfinishokay; nofurtherdataaccepted). Statebounded159samples, no perchunkpadding. BatchwrapperpreservesacceptedAPI/lastcancel/RAII.

## 3. Silero context close/reset races and owner identity

Context.close freesarraysandsetsclosed whileownerframeinflight; release_stream_context nonblockingfails silentlyleavingactiveclosedcontext permanentlyblockingreplacement. run completiondoesnotcheckctxclosing/invalidated andreturnsok. `.reset()` canraceinferenceorclearerrorinvalidated unsafely. Ownercreate checksclosingonlybeforelock, notinside; `_process_owned_stream_frame` doesn'tverify `_active_stream isctx`, acceptsmanualcontext/bytearray/wrongtypes anddoesnotinvalidateonearlycancel/geometryfailure. Routecontextoperations throughowner'sexistinglock; setctxclosingevent first soframepostcheckrejects; boundedcloseFalse whilebusyretainsreference/arrays, retryclosesandallowsreplacement. No directnativefreebyconcurrentctxmethod. Closed/invalidcontext cannotresurrect throughreset. Validateexactbytes/len1024 before native. Verifyidentityunderlock beforeload; rejectowner/contextclosing/cancel beforefinalsuccess, contextstatecommitonlywhenvalid. Ownercloseinvalidatesclearscontextunderownerlock. Shareframeinferencelogic asalreadyfactored, nosecondsession.

## 4. Tests currently false-positive

NoiseStreamTests._factory returnsclass, notinstance whenpassed`processor_factory=self._factory`; process raisesmissingargument. chunkboundarytest uses777bytes (odd/misaligned) then compares `.pcm` withoutasserting `.ok`. Both paths canerrorandcompareempty==empty. Fixinstancefactory; useevenarbitrarychunkboundaries; assertokEVERYcall/finish, nonemptyPCM, exactexpectedlengthandvalues. Usea statefulfakeprocessor soreset/perchunkpaddingchangesoutputandisdetected. Addfactory/lastframecancel, activeclose/retry andposterrorrejectiontests.

Silero `test_close_context_during_run` actuallycallsowner.close, ignoresjoinliveness/errors, releaseafterclosecanwaitfull2seconds. Replacewithdeterministicblocknativeinworker -> mainclose(timeout0) False/statuspending -> releaseinfinally/join/assertnotalive/errors -> retryTrue. Coverbothcontextcloseandownerclose separately plusreset/busy/nativeinvalidoutput. Allthreadexceptionscaptured. ExistingmoduleMic/ORT/APMblockers remain.

Keep scopeCOREonly; no microphoneintegration, mainmodels/devices/pip/data/memory. Run documentedfake12modules label `input_v2_stream_core_r1_cursor_nov12x` (unique onreruns), timeout90; STOPforCodexreview. Do not edit reviewerprobes/documents orclaimnativeequivalencebeforetested.
