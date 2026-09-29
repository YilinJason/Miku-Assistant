"""出处契约回归：委托本地可信单元边界（最终方案）。

旧的 evidence_quote / 模型 text 路径已移除；本文件保留关键极性与兼容入口检查，
完整验收矩阵见 ``tests/test_memory_trusted_boundary.py``。
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, List, Optional

from src.memory.eligibility import should_skip_extraction
from src.memory.errors import MemoryExtractionResponseError
from src.memory.fact_units import enumerate_safe_fact_units, render_claim
from src.memory.ingestion import MemoryIngestionService
from src.memory.ollama_extractor import (
    OllamaMemoryExtractor,
    build_extraction_format_schema,
)
from src.memory.provenance import (
    looks_like_pure_memory_query,
    validate_candidate_user_evidence,
)
from src.memory.service_models import MemoryTurnEvent, MemoryTurnOutcome
from src.memory.sqlite_store import SqliteMemoryStore


class _RecordingTransport:
    def __init__(self, responses: Optional[List[Any]] = None) -> None:
        self.responses = list(responses or [])
        self.payloads: List[dict] = []

    def post_chat(self, payload: dict[str, Any]) -> Any:
        self.payloads.append(payload)
        if not self.responses:
            raise AssertionError("unexpected extractor HTTP call")
        return self.responses.pop(0)


def _ok(items: list) -> dict:
    return {
        "done": True,
        "message": {"content": json.dumps(items, ensure_ascii=False)},
    }


def _turn(user: str, assistant: str = "好", turn_id: str = "t") -> MemoryTurnEvent:
    return MemoryTurnEvent(
        turn_id=turn_id,
        user_text=user,
        assistant_text=assistant,
        outcome=MemoryTurnOutcome.SUCCESS,
    )


class ProvenanceEligibilityTests(unittest.TestCase):
    def test_pure_memory_query_skips(self) -> None:
        user = "你知道我的测试代号和最喜欢的饮品吗？"
        self.assertTrue(looks_like_pure_memory_query(user))
        self.assertTrue(should_skip_extraction(_turn(user, "代号 Nightingale")))
        transport = _RecordingTransport([_ok([{"unit_id": 0}])])
        result = OllamaMemoryExtractor(transport=transport).extract_detailed(
            _turn(user, "代号 Nightingale")
        )
        self.assertTrue(result.skipped_locally)
        self.assertEqual(transport.payloads, [])

    def test_mixed_self_claim_with_question_still_extractable(self) -> None:
        user = "我最喜欢桂花乌龙，你喜欢什么？"
        self.assertFalse(looks_like_pure_memory_query(user))
        self.assertFalse(should_skip_extraction(_turn(user, "我喜欢水")))
        units = enumerate_safe_fact_units(user)
        self.assertEqual([u.text for u in units], ["我最喜欢桂花乌龙"])


class EvidenceCompatibilityTests(unittest.TestCase):
    def test_compat_validate_requires_exact_unit_and_render(self) -> None:
        user = "我的测试代号是银杏4729"
        self.assertTrue(
            validate_candidate_user_evidence(
                user_text=user,
                fact_text="用户的测试代号是银杏4729",
                evidence_quote="我的测试代号是银杏4729",
            )
        )
        self.assertFalse(
            validate_candidate_user_evidence(
                user_text=user,
                fact_text="用户的测试代号是银杏4729",
                evidence_quote="银杏",
            )
        )
        self.assertFalse(
            validate_candidate_user_evidence(
                user_text=user,
                fact_text="用户的测试代号是银杏4729",
                evidence_quote=None,
            )
        )

    def test_polarity_units(self) -> None:
        self.assertEqual(enumerate_safe_fact_units("我不喜欢抹茶拿铁")[0].claim.polarity, False)
        self.assertEqual(enumerate_safe_fact_units("我喜欢抹茶拿铁")[0].claim.polarity, True)
        self.assertEqual(
            render_claim(enumerate_safe_fact_units("我不喜欢抹茶拿铁")[0].claim),
            "用户不喜欢抹茶拿铁",
        )


class ExtractorUnitIdTests(unittest.TestCase):
    def test_payload_candidates_and_schema(self) -> None:
        transport = _RecordingTransport([_ok([])])
        OllamaMemoryExtractor(transport=transport).extract(_turn("我喜欢乌龙茶"))
        body = json.loads(transport.payloads[0]["messages"][1]["content"])
        self.assertEqual(body["candidates"][0]["text"], "我喜欢乌龙茶")
        schema = build_extraction_format_schema(4)
        self.assertEqual(schema["items"]["required"], ["unit_id"])

    def test_model_text_field_rejected(self) -> None:
        transport = _RecordingTransport(
            [_ok([{"unit_id": 0, "text": "用户是黑客"}])]
        )
        with self.assertRaises(MemoryExtractionResponseError):
            OllamaMemoryExtractor(transport=transport).extract(_turn("我喜欢乌龙茶"))

    def test_valid_selection_writes(self) -> None:
        user = "我叫林小舟"
        transport = _RecordingTransport([_ok([{"unit_id": 0, "confidence": 0.8}])])
        with TemporaryDirectory() as tmp:
            store = SqliteMemoryStore(Path(tmp) / "n.sqlite3")
            service = MemoryIngestionService(
                store,
                OllamaMemoryExtractor(transport=transport),
                owns_store=False,
            )
            service.start()
            try:
                self.assertTrue(service.submit(_turn(user, turn_id="n")))
                self.assertTrue(service.wait_until_idle(timeout=2.0))
                self.assertEqual(
                    [r.text for r in store.list_memories(limit=5)],
                    ["用户叫林小舟"],
                )
            finally:
                service.close(drain=True, timeout=2.0)
                store.close()

    def test_structural_invalid_all_items_raises(self) -> None:
        transport = _RecordingTransport(
            [_ok([{"unit_id": 0, "category": "preference"}])]
        )
        with self.assertRaises(MemoryExtractionResponseError):
            OllamaMemoryExtractor(transport=transport).extract(_turn("我喜欢乌龙茶"))


class IdAuditStillImported(unittest.TestCase):
    """占位：完整 id 审计仍在 test_id_audit；此处确认导入无环。"""

    def test_import_id_audit(self) -> None:
        from src.memory import id_audit  # noqa: F401


if __name__ == "__main__":
    unittest.main()
