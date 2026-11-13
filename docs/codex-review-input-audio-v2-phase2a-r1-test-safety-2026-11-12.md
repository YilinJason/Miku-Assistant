# R1 follow-up — first-import test must stay fake

Cursor was interrupted during drafting, before authorization to run its unfinished test. The draft `test_main_sets_thread_env_on_first_cli_app_import` delegates intercepted `src.cli.app` to the real importer, then calls real `main_mod.main()`. This can start the application. Do not execute this draft.

Complete original R1 plus this correction before running the fake gate: intercept `src.cli.app` and RETURN a `types.ModuleType`/fake module with a no-op tracked `main`; never delegate that import to the real importer. Assert the four thread keys on interception and that fake CLI main was invoked. Block imports of numpy/onnxruntime and real application modules in that test. Restore altered sys.modules entries exactly (including main), not merely deleting originals. Preserve existing environment restoration.

Add a module-level real Microphone constructor/context blocker to STT integration tests as well as the existing ORT blocker; legitimate tests inject FakeMic. No native sessions/devices or app startup. The existing scoped supervisor is the only permitted test runner.

After all R1 fixes are complete, run the documented fake module set with unique label `input_v2_integration_r1_cursor_nov12b`; stop for independent review. Do not enter Phase2b or edit Codex review documents.
