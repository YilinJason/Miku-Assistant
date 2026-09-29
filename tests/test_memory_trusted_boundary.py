"""最终可信边界：本地单元枚举 + unit_id 选择 + 本地 renderer 验收矩阵。"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, List, Optional

from src.memory.eligibility import should_skip_extraction
from src.memory.errors import MemoryExtractionResponseError
from src.memory.fact_units import (
    enumerate_safe_fact_units,
    render_claim,
)
from src.memory.ingestion import MemoryIngestionService
from src.memory.models import MemoryCategory
from src.memory.ollama_extractor import (
    OllamaMemoryExtractor,
    build_extraction_format_schema,
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


def _select_all(user: str) -> list[dict]:
    return [
        {"unit_id": u.unit_id, "confidence": 0.9, "importance": 0.8}
        for u in enumerate_safe_fact_units(user)
    ]


class LocalCandidateGenerationTests(unittest.TestCase):
    def test_two_legal_units_from_compound_sentence(self) -> None:
        user = "我的测试代号是银杏4729，我最喜欢桂花乌龙"
        units = enumerate_safe_fact_units(user)
        self.assertEqual(len(units), 2)
        self.assertEqual(units[0].text, "我的测试代号是银杏4729")
        self.assertEqual(units[1].text, "我最喜欢桂花乌龙")
        self.assertEqual(user[units[0].start : units[0].end], units[0].text)
        self.assertEqual(user[units[1].start : units[1].end], units[1].text)
        self.assertEqual(render_claim(units[0].claim), "用户的测试代号是银杏4729")
        self.assertEqual(render_claim(units[1].claim), "用户最喜欢桂花乌龙")

    def test_mixed_question_keeps_only_assertion(self) -> None:
        units = enumerate_safe_fact_units("我喜欢桂花乌龙，你呢？")
        self.assertEqual([u.text for u in units], ["我喜欢桂花乌龙"])

    def test_question_terminator_alone_yields_zero(self) -> None:
        self.assertEqual(enumerate_safe_fact_units("我叫林小舟？"), ())
        self.assertEqual(enumerate_safe_fact_units("我叫林小舟?"), ())

    def test_zero_candidates_for_pollution(self) -> None:
        cases = [
            "你知道我的测试代号和最喜欢的饮品吗？",
            "你觉得我喜欢抹茶拿铁吗？",
            "如果我喜欢抹茶拿铁",
            "别以为我喜欢抹茶拿铁",
            "别人说我喜欢抹茶拿铁",
            "我从没说过我喜欢抹茶拿铁",
            "我不觉得我喜欢抹茶拿铁",
            "我喜欢抹茶拿铁是假的",
            "我住在纽约是谣言",
            "我叫林小舟是假名",
            "我的测试代号是银杏4729是错的",
        ]
        for user in cases:
            with self.subTest(user=user):
                self.assertEqual(enumerate_safe_fact_units(user), ())

    def test_family_legals(self) -> None:
        cases = [
            ("我叫林小舟", "用户叫林小舟", MemoryCategory.USER_PROFILE),
            ("我喜欢抹茶拿铁", "用户喜欢抹茶拿铁", MemoryCategory.PREFERENCE),
            ("我不喜欢抹茶拿铁", "用户不喜欢抹茶拿铁", MemoryCategory.PREFERENCE),
            ("我住在纽约", "用户住在纽约", MemoryCategory.USER_PROFILE),
            ("我不住在纽约", "用户不住在纽约", MemoryCategory.USER_PROFILE),
            ("我认识小明", "用户认识小明", MemoryCategory.RELATIONSHIP),
            ("我不认识小明", "用户不认识小明", MemoryCategory.RELATIONSHIP),
        ]
        for user, rendered, category in cases:
            with self.subTest(user=user):
                units = enumerate_safe_fact_units(user)
                self.assertEqual(len(units), 1)
                self.assertEqual(render_claim(units[0].claim), rendered)
                from src.memory.fact_units import category_for_claim

                self.assertEqual(category_for_claim(units[0].claim), category)


class ExtractorTrustedBoundaryTests(unittest.TestCase):
    def test_schema_only_unit_id(self) -> None:
        schema = build_extraction_format_schema(4)
        self.assertEqual(schema["items"]["required"], ["unit_id"])
        self.assertEqual(
            set(schema["items"]["properties"]),
            {"unit_id", "importance", "confidence"},
        )
        self.assertFalse(schema["items"]["additionalProperties"])

    def test_no_http_when_no_local_units(self) -> None:
        transport = _RecordingTransport([_ok([{"unit_id": 0}])])
        result = OllamaMemoryExtractor(transport=transport).extract_detailed(
            _turn("你觉得我喜欢抹茶拿铁吗？")
        )
        self.assertTrue(result.skipped_locally)
        self.assertEqual(result.items, ())
        self.assertEqual(transport.payloads, [])

    def test_payload_sends_candidates_not_raw_user_only_model_text(self) -> None:
        user = "我喜欢乌龙茶"
        transport = _RecordingTransport([_ok([{"unit_id": 0}])])
        OllamaMemoryExtractor(transport=transport).extract(_turn(user))
        body = json.loads(transport.payloads[0]["messages"][1]["content"])
        self.assertEqual(body, {"candidates": [{"unit_id": 0, "text": "我喜欢乌龙茶"}]})
        self.assertNotIn("assistant_text", body)
        self.assertNotIn("user_text", body)

    def test_two_units_selected_write_rendered_facts(self) -> None:
        user = "我的测试代号是银杏4729，我最喜欢桂花乌龙"
        transport = _RecordingTransport([_ok(_select_all(user))])
        items = OllamaMemoryExtractor(transport=transport).extract(_turn(user))
        texts = {i.text for i in items}
        self.assertEqual(
            texts, {"用户的测试代号是银杏4729", "用户最喜欢桂花乌龙"}
        )

    def test_malicious_model_text_never_enters_extracted_memory(self) -> None:
        user = "我喜欢乌龙茶"
        transport = _RecordingTransport(
            [
                _ok(
                    [
                        {
                            "unit_id": 0,
                            "text": "用户是黑客代号 Nightingale",
                            "category": "user_profile",
                            "evidence_quote": "伪造",
                            "value": "黑客",
                        }
                    ]
                )
            ]
        )
        with self.assertRaises(MemoryExtractionResponseError):
            OllamaMemoryExtractor(transport=transport).extract(_turn(user))

    def test_unknown_bool_duplicate_out_of_range_ids(self) -> None:
        user = "我喜欢乌龙茶"
        # unknown / bool / duplicate: skip; all-invalid raises
        transport = _RecordingTransport(
            [_ok([{"unit_id": 99}, {"unit_id": True}, {"unit_id": 0}, {"unit_id": 0}])]
        )
        items = OllamaMemoryExtractor(transport=transport).extract(_turn(user))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].text, "用户喜欢乌龙茶")

    def test_assistant_hallucination_cannot_create_units(self) -> None:
        user = "随便聊聊天气吧"
        assistant = "你的代号是 Project Nightingale，爱喝抹茶拿铁，少糖多奶。"
        transport = _RecordingTransport([_ok([{"unit_id": 0}])])
        result = OllamaMemoryExtractor(transport=transport).extract_detailed(
            _turn(user, assistant)
        )
        self.assertTrue(result.skipped_locally or result.items == ())
        self.assertEqual(result.items, ())
        self.assertEqual(transport.payloads, [])


class IngestionTrustedBoundaryTests(unittest.TestCase):
    def test_legal_two_facts_land_in_store(self) -> None:
        user = "我的测试代号是银杏4729，我最喜欢桂花乌龙"
        transport = _RecordingTransport([_ok(_select_all(user))])
        with TemporaryDirectory() as tmp:
            store = SqliteMemoryStore(Path(tmp) / "ok.sqlite3")
            service = MemoryIngestionService(
                store,
                OllamaMemoryExtractor(transport=transport),
                owns_store=False,
            )
            service.start()
            try:
                self.assertTrue(service.submit(_turn(user, turn_id="ok")))
                self.assertTrue(service.wait_until_idle(timeout=2.0))
                texts = {r.text for r in store.list_memories(limit=10)}
                self.assertEqual(
                    texts, {"用户的测试代号是银杏4729", "用户最喜欢桂花乌龙"}
                )
            finally:
                service.close(drain=True, timeout=2.0)
                store.close()

    def test_zero_write_matrix(self) -> None:
        cases = [
            "你知道我的测试代号和最喜欢的饮品吗？",
            "你觉得我喜欢抹茶拿铁吗？",
            "如果我喜欢抹茶拿铁",
            "别以为我喜欢抹茶拿铁",
            "别人说我喜欢抹茶拿铁",
            "我从没说过我喜欢抹茶拿铁",
            "我不觉得我喜欢抹茶拿铁",
            "我喜欢抹茶拿铁是假的",
            "我住在纽约是谣言",
            "我叫林小舟是假名",
            "我的测试代号是银杏4729是错的",
            "我叫林小舟？",
        ]
        for user in cases:
            with self.subTest(user=user):
                transport = _RecordingTransport(
                    [_ok([{"unit_id": 0, "text": "用户喜欢抹茶拿铁"}])]
                )
                with TemporaryDirectory() as tmp:
                    store = SqliteMemoryStore(Path(tmp) / "z.sqlite3")
                    service = MemoryIngestionService(
                        store,
                        OllamaMemoryExtractor(transport=transport),
                        owns_store=False,
                    )
                    service.start()
                    try:
                        self.assertTrue(service.submit(_turn(user, turn_id="z")))
                        self.assertTrue(service.wait_until_idle(timeout=2.0))
                        self.assertEqual(store.count_memories(), 0)
                        self.assertEqual(transport.payloads, [])
                    finally:
                        service.close(drain=True, timeout=2.0)
                        store.close()

    def test_mixed_keeps_legal_sibling(self) -> None:
        user = "我叫林小舟，别以为我喜欢抹茶拿铁"
        units = enumerate_safe_fact_units(user)
        self.assertEqual([u.text for u in units], ["我叫林小舟"])
        transport = _RecordingTransport(
            [
                _ok(
                    [
                        {"unit_id": 0, "confidence": 0.9},
                        {"unit_id": 1, "confidence": 1.0},  # 越界，跳过
                    ]
                )
            ]
        )
        with TemporaryDirectory() as tmp:
            store = SqliteMemoryStore(Path(tmp) / "mix.sqlite3")
            service = MemoryIngestionService(
                store,
                OllamaMemoryExtractor(transport=transport),
                owns_store=False,
            )
            service.start()
            try:
                self.assertTrue(service.submit(_turn(user, turn_id="mix")))
                self.assertTrue(service.wait_until_idle(timeout=2.0))
                records = list(store.list_memories(limit=10))
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0].text, "用户叫林小舟")
            finally:
                service.close(drain=True, timeout=2.0)
                store.close()

    def test_duplicate_unit_selection_deterministic(self) -> None:
        user = "我喜欢咖啡，我喜欢咖啡"
        units = enumerate_safe_fact_units(user)
        self.assertGreaterEqual(len(units), 1)
        # 选择同一 id 两次：第二次跳过
        transport = _RecordingTransport(
            [_ok([{"unit_id": 0}, {"unit_id": 0}])]
        )
        items = OllamaMemoryExtractor(transport=transport).extract(_turn(user))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].text, "用户喜欢咖啡")

    def test_pure_query_skipped(self) -> None:
        user = "你知道我的测试代号和最喜欢的饮品吗？"
        self.assertTrue(should_skip_extraction(_turn(user, "代号 Nightingale")))


class SegmentAtomicityTests(unittest.TestCase):
    """标点 segment 先验收；同段并列 all-or-nothing；禁止裸「并」切分。"""

    _ZERO = [
        "我喜欢咖啡而且我住在纽约？",
        "我喜欢咖啡并且我住在纽约？",
        "我叫林小舟以及我住在纽约吗",
        "我叫林小舟以及我住在纽约呢",
        "我喜欢咖啡并不是真的",
        "我喜欢咖啡而且这只是举例",
        "我喜欢咖啡而且。",
        "我喜欢咖啡并且。",
        "我喜欢咖啡以及。",
        "我喜欢咖啡而且而且我住在纽约。",
    ]

    def test_zero_candidates_for_segment_context_and_partial_conj(self) -> None:
        for user in self._ZERO:
            with self.subTest(user=user):
                self.assertEqual(enumerate_safe_fact_units(user), ())

    def test_legal_conj_and_bing_in_value(self) -> None:
        from src.memory.fact_units import category_for_claim

        cases = [
            (
                "我喜欢咖啡而且我住在纽约。",
                [
                    ("我喜欢咖啡", "用户喜欢咖啡", "preference"),
                    ("我住在纽约", "用户住在纽约", "user_profile"),
                ],
            ),
            (
                "我喜欢咖啡并且我住在纽约。",
                [
                    ("我喜欢咖啡", "用户喜欢咖啡", "preference"),
                    ("我住在纽约", "用户住在纽约", "user_profile"),
                ],
            ),
            (
                "我叫林小舟以及我喜欢乌龙茶。",
                [
                    ("我叫林小舟", "用户叫林小舟", "user_profile"),
                    ("我喜欢乌龙茶", "用户喜欢乌龙茶", "preference"),
                ],
            ),
            (
                "我喜欢桂花乌龙，你呢？",
                [("我喜欢桂花乌龙", "用户喜欢桂花乌龙", "preference")],
            ),
            (
                "我喜欢并蒂莲。",
                [("我喜欢并蒂莲", "用户喜欢并蒂莲", "preference")],
            ),
            (
                "我喜欢咖啡而且我不喜欢红茶。",
                [
                    ("我喜欢咖啡", "用户喜欢咖啡", "preference"),
                    ("我不喜欢红茶", "用户不喜欢红茶", "preference"),
                ],
            ),
        ]
        for user, expected in cases:
            with self.subTest(user=user):
                units = enumerate_safe_fact_units(user)
                self.assertEqual(len(units), len(expected))
                for unit, (text, rendered, cat) in zip(units, expected, strict=True):
                    self.assertEqual(unit.text, text)
                    self.assertEqual(user[unit.start : unit.end], unit.text)
                    self.assertEqual(render_claim(unit.claim), rendered)
                    self.assertEqual(category_for_claim(unit.claim).value, cat)

    def test_conj_present_illegal_structure_zero_candidates(self) -> None:
        """连接词存在但一侧为空/重复时，禁止回退单事实贪婪解析。"""
        for user in (
            "我喜欢咖啡而且。",
            "我喜欢咖啡并且。",
            "我喜欢咖啡以及。",
            "我喜欢咖啡而且而且我住在纽约。",
        ):
            with self.subTest(user=user):
                self.assertEqual(enumerate_safe_fact_units(user), ())

    def test_conj_present_illegal_no_http_and_temp_sqlite(self) -> None:
        for user in (
            "我喜欢咖啡而且。",
            "我喜欢咖啡并且。",
            "我喜欢咖啡以及。",
            "我喜欢咖啡而且而且我住在纽约。",
        ):
            with self.subTest(path="extractor", user=user):
                transport = _RecordingTransport([_ok([{"unit_id": 0}])])
                result = OllamaMemoryExtractor(transport=transport).extract_detailed(
                    _turn(user)
                )
                self.assertEqual(result.items, ())
                self.assertEqual(transport.payloads, [])

            with self.subTest(path="ingestion", user=user):
                transport = _RecordingTransport([_ok([{"unit_id": 0}])])
                with TemporaryDirectory() as tmp:
                    store = SqliteMemoryStore(Path(tmp) / "conj-fb.sqlite3")
                    service = MemoryIngestionService(
                        store,
                        OllamaMemoryExtractor(transport=transport),
                        owns_store=False,
                    )
                    service.start()
                    try:
                        self.assertTrue(service.submit(_turn(user, turn_id="cfb")))
                        self.assertTrue(service.wait_until_idle(timeout=2.0))
                        self.assertEqual(store.count_memories(), 0)
                        self.assertEqual(transport.payloads, [])
                    finally:
                        service.close(drain=True, timeout=2.0)
                        store.close()

    def test_legal_conj_with_negated_like_child(self) -> None:
        from src.memory.fact_units import category_for_claim

        user = "我喜欢咖啡而且我不喜欢红茶。"
        units = enumerate_safe_fact_units(user)
        self.assertEqual(len(units), 2)
        self.assertEqual(units[0].text, "我喜欢咖啡")
        self.assertEqual(units[1].text, "我不喜欢红茶")
        self.assertEqual(render_claim(units[0].claim), "用户喜欢咖啡")
        self.assertEqual(render_claim(units[1].claim), "用户不喜欢红茶")
        self.assertEqual(category_for_claim(units[0].claim).value, "preference")
        self.assertEqual(category_for_claim(units[1].claim).value, "preference")
        for unit in units:
            self.assertEqual(user[unit.start : unit.end], unit.text)

    def test_zero_no_http_and_temp_sqlite(self) -> None:
        for user in self._ZERO:
            with self.subTest(path="extractor", user=user):
                transport = _RecordingTransport([_ok([{"unit_id": 0}])])
                result = OllamaMemoryExtractor(transport=transport).extract_detailed(
                    _turn(user)
                )
                self.assertEqual(result.items, ())
                self.assertEqual(transport.payloads, [])

            with self.subTest(path="ingestion", user=user):
                transport = _RecordingTransport([_ok([{"unit_id": 0}])])
                with TemporaryDirectory() as tmp:
                    store = SqliteMemoryStore(Path(tmp) / "sa.sqlite3")
                    service = MemoryIngestionService(
                        store,
                        OllamaMemoryExtractor(transport=transport),
                        owns_store=False,
                    )
                    service.start()
                    try:
                        self.assertTrue(service.submit(_turn(user, turn_id="sa")))
                        self.assertTrue(service.wait_until_idle(timeout=2.0))
                        self.assertEqual(store.count_memories(), 0)
                        self.assertEqual(transport.payloads, [])
                    finally:
                        service.close(drain=True, timeout=2.0)
                        store.close()

    def test_legal_conj_writes_two_via_ingestion(self) -> None:
        user = "我喜欢咖啡并且我住在纽约。"
        transport = _RecordingTransport([_ok(_select_all(user))])
        with TemporaryDirectory() as tmp:
            store = SqliteMemoryStore(Path(tmp) / "conj.sqlite3")
            service = MemoryIngestionService(
                store,
                OllamaMemoryExtractor(transport=transport),
                owns_store=False,
            )
            service.start()
            try:
                self.assertTrue(service.submit(_turn(user, turn_id="conj")))
                self.assertTrue(service.wait_until_idle(timeout=2.0))
                texts = {r.text for r in store.list_memories(limit=10)}
                self.assertEqual(texts, {"用户喜欢咖啡", "用户住在纽约"})
                body = transport.payloads[0]["messages"][1]["content"]
                import json as _json

                candidates = _json.loads(body)["candidates"]
                self.assertEqual(
                    [c["text"] for c in candidates],
                    ["我喜欢咖啡", "我住在纽约"],
                )
            finally:
                service.close(drain=True, timeout=2.0)
                store.close()


if __name__ == "__main__":
    unittest.main()
