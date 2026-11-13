# Continuous capture wiring milestone — independently accepted

Cursor authored code/formal tests; Codex source review and independent integration closed serial regression, partialbegin cleanup, unsafe exitresult, shutdown/stop publication, mutablecancel view, self-lock and double-preparer accidental shutdown. Failed drafts/probes retained; no actualdevice/modelstack run.

- `input_v2_wiring_contract_nov12bd`:6 independent fake checks pass, includingtwo concurrentpreparers ->onecontrollerstart/no accidentalclose.
- `input_v2_wiring_independent_nov12be`:318fake tests/19modules pass,elapsed15.468s,WAV728→728.
- `input_v2_wiring_native_nov12ba`: REALSTT/controller/CPU NS+Silero withFAKEfilesource+FAKEASR:25noise neverreachASR,1fixedNahidacontrol admitted, exactcleanedsegmentbytes/sampletimestampsverified, no secondNS/VAD/legacymic, modeexit whilefakeASRblocked dropslateSUCCESS.27captureweakrefs reclaimed, finalVADsessiongone andnocapturethread. Reporttmp/capture_wiring_native_20261112_7eda04156d8f.json. R2afterthisreplay onlyfixedsecondpublicationgate/format/docs, coveredindependentlyby6checks/fullfake.
- Scopedgitdiffcheckpassed(onlyLF/CRLFwarnings). No commit/push, formaldata/memoryuntouched, ASR/RVC/Cosy environments/models/voicepreserved.

Default `capture_mode: continuous` appliesonlyexplicitduplexbegin+Paraformer+Silero+enabled/noinjectedprovider. BackgroundworkerexistsONLYwhilethatvoicesessionactive; ordinaryserial/diagnosticpaths keepacceptedcompatibility. `capture_mode: utterance` explicitfallback. Oneinputworker,queue2,12secutterances,640msquietend,3secstale; no automaticrawfallback onruntimefailure. EndFalse retainsresources andCLIreturnsunsafe_pause. Capturedtimestampsarehostsampleclockestimates, nothardwaretimestamps/AECproof.

Next small timing correction: usecapture interval ratherthanASR-returnwallclock toretainreplyactivityechoreference. This is text/timing mitigation, NOT actualplayedPCMreference, acousticAEC, speakerverification orresume-after-false-interruption. Those needseparatelyscopedwork/roomvalidation; don'tclaimPersonaPlexfullstackreplicated.
