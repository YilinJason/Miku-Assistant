# C2b2b — App-owned local voice activation (DESIGN DRAFT, NOT RELEASED)

Do not execute this task until Codex marks real three-model coexist/short speech gate PASS. Default30 failed on 2026-10-18; CUDA transfer follow-up is under review. This document saves design, not implementation authority.

Read AGENTS/architecture-contracts/collaboration-review/engineering-quality and current assistant/assembly/CLI tests. Preserve dirty workspace and Edge/injected compatibility. Never real data/memory, mic/speaker/models/main, commits/push during implementation. User explicitly wants resource responsiveness and strict slice closure.

## Problem and ownership

Current assembly runs legacy warmup before App owns TTS. Current ensure_* returns True for any already-bound TTS, independent of Cosy readiness. Unpublished TTS cleanup best-effort drops the handle and shutdown may close shared TFM while loader is still warming. Do NOT call prepare_local_voice in assembly before binding ownership.

When explicitly selected Cosy, assembly constructs only; defer ALL model warmup for that engine until App activation after parts are bound. Existing Edge and injection remain compatible; from_dependencies must not create missing components. For Cosy all public activation paths (already assembled voice, TTS-only, both missing, existing TTS/missing STT) must observe ready state, no early fake success.

Use one existing loader single-flight lane, one cancel Event, one private inflight candidate owner before any effectful prepare, and bounded pending-retire owner (max1). State mapping idle/preparing/ready/failed/closing, explicit per-generation result if necessary. Existing TTS also must be owned throughout prepare. Perform build/warmup/shutdown/join outside lifecycle/condition locks; locks only inspect/update references. Never hold lifecycle then acquire loader mutex while another path has opposite order.

On shutdown: set closing + cancel, wake waiters immediately; reject late success/publication and new activation. Do not claim waiters cancelled means worker exited. Keep TTS/TFM owner while loader or retirement remains live/unknown; close cleaner/character/memory independently. Incomplete shutdown retry must reclaim retained candidate, re-check loading/published status, then close TFM; never mark non_memory_done while pending writer/loader. Do not create a cleanup thread per interruption. Candidate build itself can block: loading=True protects TFM even before constructor returns; constructor-return after closing must register/reclaim under same protocol.

A failed/cancelled prepare must not leave an unpublished model unowned. Retire outside lock, inspect Cosy shutdown status; missing/None/exception fails closed. Do not auto-retry a failed request, start multiple candidates, clear live registries, fallback cloud or change voice/model. Explicit tts retry only after previous candidate/worker quiescent; sticky RVC failure handling must be honest. Keep one current published object plus at most one retained failed candidate; reject allocation until retained retired. Existing owned/shared RVC rules remain consistent.

Synchronous ensure API remains bounded by stage budgets and reports failure while keeping text usable. Do not claim main CLI fully responsive during synchronous prepare; if nonblocking UX is required, design separate poll/result API and at most one owned loader task in subsequent slice rather than hide daemon threads here. Cancellation waiters must return <300ms after shutdown; blocked model lifecycle is bounded by existing process deadlines, not fabricated coordinator timeout.

## Contract tests required

Pure Event-controlled fake engines, no devices/subprocesses: assembly explicitly selected Cosy has zero warmup calls; bound voice Cosy cannot early-True; normal serialized prepare single-flight; missing STT transaction failure retires candidate; shutdown during constructor / Ollama / RVC / Cosy / publication; no preparation after closing; waiters wake promptly; success cannot publish after close; incomplete/missing status retains owner+TFM and retries cleanup later; one retained candidate blocks further allocation; failure reentry no unbounded threads; injected no-construction + Edge existing behavior intact; no locks held during blocked IO/shutdown. Test actual fake counts/events and TempFileManager closure order, no source-string matching.

Review production diff/callers then bounded related gate, exact miku WAV set invariant. Root independent probes before real small activation test; no prefetch/queue/playback/D soak until this closes.
