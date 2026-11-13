# A final unit correction (small scope)

R3 implementation review found one concrete time-unit bug; do not add other features or B.

`compute_paraformer_listen_phrase_limit_sec` currently computes frame_duration = CHUNK / (SAMPLE_RATE * SAMPLE_WIDTH). SpeechRecognition CHUNK and Microphone.stream.read size are SAMPLE FRAMES, not bytes. Correct expression is CHUNK / SAMPLE_RATE (matches SR._listen seconds_per_buffer). Bytes-per-sample must NOT change recording time allowance. Keep explicit positive geometry validation; reject invalid/nonfinite non_speaking_duration and invalid budget rather than manufacturing a budget that exceeds protocol.

Add width-invariance test (same chunk/rate, width 1 vs 2 -> same time budget) and actual SR.listen + virtual AudioSource pre-roll/speech tests at 8k/16k/44.1k/48k (no mic), converting returned AudioData to 16k mono16 to assert <=480000. The virtual stream must return size SAMPLE FRAMES * width bytes; prefix ~.6s silence then speech; bounded read count and timeout. This validates end-to-end capture budget, not only the helper's own formula.

Also ensure `validate_paraformer_pcm_bytes` rejects non-bytes (e.g. None, str, bytearray) with a stable invalid-type reason before len/native VAD/client access; AudioData.get_raw_data returns bytes and no implicit coercion is needed. Test missing/invalid values plus geometry, no worker dispatch. Keep rejected input statuses stable and cancellation precedence.

Strengthen tests/test_input_audio.py isolation with setUpModule/tearDownModule patching the REAL speech_recognition.Microphone.__init__ and __enter__ to raise on accidental real-device path; the existing per-test full fake class replacements should override these safely. Real Recognizer/AudioSource virtual tests remain intact. Don't patch the SR methods under test or introduce actual PyAudio calls. This catches future tests forgetting OfflineMicrophoneTestCase.

Run same explicit scoped fake modules with new unique label voice_input_cursor_r4_nov5i after these small fixes. Preserve earlier reports; no native reviewer probe, devices, models, app or memory. Update ADR source-frame units wording if needed, STOP for independent final review.
