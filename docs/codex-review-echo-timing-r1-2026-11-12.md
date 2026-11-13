# Echo timing review — code direction accepted, final docs/tests polish

The smallcapture-intervalhelper correctlyseparatestextgatetimefromcurrentreplycancellation, keepslegacyfallback anddoesnotintroducePCM/AECclaims. Noalgorithmrewrite requested.

Beforefinalindependentgate:

1. VOICEwording currentlysaysutterance`recognize` anddiagnosticsdefaultdon'trepeatguard, whichsuggestsmicutteranceguardisbypassed. Stateunambiguously: bothmiccapturepathsapplyNS/VADonce; directtrustedAudioData/diagnosticdefaultbypassesmicguard.
2. Manualguide mustcontainactualcopyablePowerShellstartup: `Set-Location 'D:\Miku_Assistant_LLM'` then `& 'C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe' -X utf8 .\main.py --config .\tmp\manual-voice-test-20261105\config.yaml --mode voice`. Codexconfirmedthisisolatedconfigexists, engineparaformer, missinginput_audiokeysinheritnewdefaults, memoryDBintmpnotformaldata/memory. Do notreadDBcontents orwriteconfig. Explainoneinstance/waitnormalshutdownbefore restart, noactivate.ps1/executionpolicychange. Useactualmodephrases关闭语音模式/开启语音模式 forinteractiveflow; iflaunchvoice exitswholeapp, reentryusescommandagain. Remove“风险自负”/vague“线程感” wording. Optionallowvolume-speakercomparison hasknownAECmissinglimitation, noextrawarningflow. Giveconcretereportfields timings/falsecount/residentmemorybefore-after, don'tclaimTaskManageraloneprovesnoleak. Fallbackyamlfieldatrightnesting voice.stt.input_audio.capture_mode: utterance.
3. Formaltimingtestscript exhaustedshouldraiseAssertionError ratherthanreturnSILENCE_TIMEOUTforever (avoidhotloopifregressionlosesexit). AddpublicVoiceSessionfakecase: exactSAMEassistanttextspokenbyuserAFTERactivitywindow+grace isacceptedasanewturn; doneoldreplyneverinterrupted. Existingoverlapcasecorrectlyrejects. No native/device work.

Selfchecknewtimingmodule +all19wiringmodules viaexistingboundedsupervisor,label`input_v2_echo_timing_r1_cursor_nov12bg`,timeout90. No rawtests/rootprobes/app/device/models/pip/data/memory/otherphase. STOP; Codexwillindependentlyverifyfinaluserpath.
