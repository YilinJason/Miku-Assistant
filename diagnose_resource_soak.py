#!/usr/bin/env python
"""离线资源 soak：baseline → fake workload → closed，不访问麦克风/扬声器/GPU。"""
from __future__ import annotations

import argparse
import gc
import json
import sys
import weakref
from pathlib import Path
from typing import Any, List

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.core.assistant import MikuAssistant
from src.core.dispatch_outcome import DispatchOutcome
from src.core.resource_soak import (
    SoakSample,
    collect_assistant_queue_depths,
    run_offline_soak_report,
    sample_resource_snapshot,
)
from src.core.router import IntentRouter


class _Cfg:
    def get(self, key: str, default: Any = None) -> Any:
        return default

    def set(self, key: str, value: Any) -> None:
        return None


class _Router:
    def dispatch_outcome(self, text: str):
        if text.startswith("fail"):
            return DispatchOutcome.module_unavailable("boom")
        return DispatchOutcome(text="ok", allow_tts=False, allow_character=False)


def main() -> int:
    parser = argparse.ArgumentParser(description="离线资源 soak（fake workload）")
    parser.add_argument("--turns", type=int, default=1000)
    parser.add_argument("--fails", type=int, default=100)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    samples: List[SoakSample] = []
    samples.append(SoakSample("baseline", sample_resource_snapshot(queue_depths={})))

    assistant = MikuAssistant.from_dependencies(
        config=_Cfg(),
        router=_Router(),  # type: ignore[arg-type]
    )
    refs = [weakref.ref(assistant)]

    for i in range(max(0, args.turns)):
        assistant.process_text_input(f"u{i}", use_tts=False)
    for i in range(max(0, args.fails)):
        assistant.process_text_input(f"fail{i}", use_tts=False)
        assistant.interrupt_active_output()

    depths = collect_assistant_queue_depths(assistant)
    samples.append(
        SoakSample("workload", sample_resource_snapshot(queue_depths=depths))
    )

    # 额外短生命周期助手：全部放入列表，关闭后再统一丢弃强引用
    ephemeral: List[MikuAssistant] = []
    for _ in range(5):
        created = MikuAssistant.from_dependencies(
            config=_Cfg(), router=IntentRouter([])
        )
        refs.append(weakref.ref(created))
        created.shutdown()
        ephemeral.append(created)

    assistant.shutdown()
    samples.append(
        SoakSample(
            "closed",
            sample_resource_snapshot(
                queue_depths=collect_assistant_queue_depths(assistant)
            ),
        )
    )

    # 终点采样完成后再删除强引用并强制回收，才能真实验收 weakref
    del assistant
    del ephemeral
    del created
    gc.collect()

    alive = sum(1 for r in refs if r() is not None)
    bounds_report = run_offline_soak_report(samples)
    bounds_ok = bool(bounds_report.get("ok"))
    reclaim_ok = alive == 0
    report = {
        **bounds_report,
        "bounds_ok": bounds_ok,
        "reclaim_ok": reclaim_ok,
        "weakrefs_alive_after_shutdown": alive,
        "ok": bounds_ok and reclaim_ok,
    }
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(
            f"ok={report['ok']} bounds_ok={bounds_ok} reclaim_ok={reclaim_ok} "
            f"observability={report['observability']} "
            f"weakrefs_alive={alive} samples={len(samples)}"
        )
        for s in samples:
            print(f"  {s.label}: {s.snapshot.as_dict()}")
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
