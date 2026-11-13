# Streaming core milestone — independently accepted; device integration pending

Cursor authorsproduction/formaltests; Codexreviewed code/callers andindependentlyreplayed. NS nowwrapsoneincrementalstream withoutperchunkpadding; Silero batch/stream shareoneverifiedCPUInferenceSession withseparateboundedstate. Puresegmenter has256msring,96msstartconfirmation,640msquietend,12seccap andrealvoicedevidence. No captureworker/defaultmicpathswitch is included.

Fixedreviewfindings: duplicateonset/evidence/maxsplitframes; repeatedpreviousutterancetail; NSlast-frame/pre-callcancel andclosepending; Silerocontextclose/reset/identity; fakechunkboundarytestcomparingfailedemptyoutputs. Independentfailedgate `input_v2_stream_gap_contract_nov12z` retained; final `input_v2_stream_contract_nov12ab`6/6passed. RootreportsnotmodifiedbyCursor. Authorclaimedrunningrootfakeprobe despiteinstructionnotto; acceptance reliesonCodex'ssupervisedrerun only.

- `input_v2_stream_core_independent_nov12ad`:275fake tests/17modules passed,elapsed13.140s,WAV728→728.
- `input_v2_stream_core_native_nov12ac`:34fixedcases(25syntheticnoise/9RVCvoiceincludinggains) exactNSbytesacross2/318/1024/4096/66bytechunks; streamingVADprobabilities equalbatchwithin1e-6; allnoiseproducednosegment,allvoiceproducedsegments; emittedPCMexactlymatchessourceindices. NSandSileroCPUonly; no mic/playback/ASR/LLM/torch.
- Native10,000streamframesprivate57,454,592→58,503,168bytes (~1MiB increase,<8MiB bound), modelweakrefreclaimed,afterclose46,739,456bytes. Frame32msworkwallp50.128ms/p95.166ms onthismachine. VirtualframesareNOTrealtimehardwaresoak. Report `tmp/input_stream_native_20261112_ba985d9d7812.json`.

Next: boundedcapturecontroller thenSTT/VoiceSessionintegration, withindependentreviewbeforeeach. SmallformalNScloseteststilluses50mswait; replacewithdeterministicbarrierwhenaddingcaptureresourcetests. Existingrootboundaryproofalreadyusesbarriers/joins/errors.

No formaldata/memoryaccess, commit/push orrealdeviceuse. User-roomfalse-trigger/shortspeech/AECresultsremainunknown.
