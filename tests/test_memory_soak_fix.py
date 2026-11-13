# -*- coding: utf-8 -*-
"""真实语音 soak 返修：单字事实、失败关闭召回与 skip_reason allowlist。"""
from __future__ import annotations

import json
import logging
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, List, Optional

from src.memory.fact_units import enumerate_safe_fact_units, render_claim
from src.memory.ingestion import MemoryIngestionService, normalize_ingestion_skip_reason
from src.memory.lexical_recall import (
    LexicalMemoryRecall,
    content_lexical_tokens,
    is_generic_preference_query,
)
from src.memory.models import MemoryCategory, NewMemory
from src.memory.ollama_extractor import OllamaMemoryExtractor
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


def _turn(user: str, turn_id: str = "t1") -> MemoryTurnEvent:
    return MemoryTurnEvent(
        turn_id=turn_id,
        user_text=user,
        assistant_text="好的",
        outcome=MemoryTurnOutcome.SUCCESS,
    )


class F1SingleCharAndRememberTests(unittest.TestCase):
    def test_single_char_positive_units(self) -> None:
        cases = [
            ("我喜欢茶", "用户喜欢茶"),
            ("我喜欢猫", "用户喜欢猫"),
            ("请记住我喜欢茶", "用户喜欢茶"),
        ]
        for user, rendered in cases:
            with self.subTest(user=user):
                units = enumerate_safe_fact_units(user)
                self.assertEqual(len(units), 1, user)
                self.assertEqual(units[0].text, user)
                self.assertEqual(render_claim(units[0].claim), rendered)

    def test_single_char_safety_zero(self) -> None:
        cases = [
            "我喜欢什么",
            "我喜欢饮品",
            "我喜欢茶吗",
            "如果我喜欢茶",
            "别人说我喜欢茶",
            "我不确定我喜欢茶",
            "我喜欢茶是假的",
        ]
        for user in cases:
            with self.subTest(user=user):
                self.assertEqual(enumerate_safe_fact_units(user), ())

    def test_remember_prefixes_yield_one_candidate(self) -> None:
        cases = [
            "请记住我喜欢香草甜筒",
            "记住我喜欢香草甜筒",
            "你要记住我喜欢香草甜筒",
            "我想告诉你我喜欢香草甜筒",
            "我跟你说一下我喜欢香草甜筒",
            "请记住：我喜欢香草甜筒",
            "记住，我喜欢香草甜筒",
            "请记住 我喜欢香草甜筒",
        ]
        for user in cases:
            with self.subTest(user=user):
                units = enumerate_safe_fact_units(user)
                self.assertEqual(len(units), 1, user)
                self.assertEqual(units[0].text, user)
                self.assertEqual(user[units[0].start : units[0].end], units[0].text)
                self.assertEqual(render_claim(units[0].claim), "用户喜欢香草甜筒")

    def test_slot_sentences(self) -> None:
        u1 = enumerate_safe_fact_units("我喜欢的冰激凌是香草甜筒")
        self.assertEqual(len(u1), 1)
        self.assertEqual(render_claim(u1[0].claim), "用户喜欢的冰激凌是香草甜筒")
        u2 = enumerate_safe_fact_units("我最喜欢的冰淇淋口味是香草")
        self.assertEqual(len(u2), 1)
        self.assertEqual(render_claim(u2[0].claim), "用户最喜欢的冰淇淋口味是香草")
        u3 = enumerate_safe_fact_units("我喜欢的饮品是抹茶拿铁")
        self.assertEqual(len(u3), 1)
        self.assertEqual(render_claim(u3[0].claim), "用户喜欢的饮品是抹茶拿铁")
        self.assertEqual(u3[0].claim.slot, "饮品")

    def test_pollution_and_ambiguous_zero(self) -> None:
        cases = [
            "别人说我喜欢香草甜筒",
            "如果我喜欢香草甜筒",
            "别以为我喜欢香草甜筒",
            "我不确定我喜欢香草甜筒",
            "我说我喜欢香草甜筒",
            "你觉得我喜欢香草甜筒吗？",
            "我喜欢香草甜筒是假的",
        ]
        for user in cases:
            with self.subTest(user=user):
                self.assertEqual(enumerate_safe_fact_units(user), ())


class F2FailClosedRecallTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        db = Path(self._tmp.name) / "m.sqlite3"
        self.store = SqliteMemoryStore(db)
        self.addCleanup(self.store.close)
        self.recall = LexicalMemoryRecall(self.store)

    def _seed_mixed(self) -> dict[str, str]:
        """混合临时库：slot 饮品、slot 冰激凌、无 slot 抹茶、猫、香草甜筒。"""
        mapping = {
            "drink_slot": "用户喜欢的饮品是抹茶拿铁",
            "ice_slot": "用户喜欢的冰激凌是香草甜筒",
            "matcha_bare": "用户喜欢抹茶拿铁",
            "cat": "用户喜欢猫",
            "cone_bare": "用户喜欢香草甜筒",
        }
        for text in mapping.values():
            self.store.add(
                NewMemory(
                    text=text,
                    category=MemoryCategory.PREFERENCE,
                    importance=0.9,
                )
            )
        return mapping

    def test_mixed_matrix_exact_hits(self) -> None:
        m = self._seed_mixed()

        def texts(query: str) -> set[str]:
            return {h.record.text for h in self.recall.recall(query, limit=10).hits}

        self.assertEqual(texts("我喜欢猫吗"), {m["cat"]})
        self.assertNotIn(m["cat"], texts("我喜欢茶吗"))
        self.assertNotIn(m["cone_bare"], texts("我喜欢茶吗"))
        self.assertNotIn(m["matcha_bare"], texts("我喜欢茶吗"))

        drink_hits = texts("我喜欢什么饮品")
        self.assertEqual(drink_hits, {m["drink_slot"]})
        self.assertNotIn(m["cat"], drink_hits)
        self.assertNotIn(m["cone_bare"], drink_hits)
        self.assertNotIn(m["matcha_bare"], drink_hits)
        self.assertNotIn(m["ice_slot"], drink_hits)

        food_hits = texts("我喜欢什么食物")
        self.assertEqual(food_hits, set())

        ice_hits = texts("我喜欢的冰激凌是什么")
        self.assertEqual(ice_hits, {m["ice_slot"]})
        self.assertNotIn(m["matcha_bare"], ice_hits)
        self.assertNotIn(m["drink_slot"], ice_hits)

        cone_hits = texts("我喜欢什么甜筒")
        self.assertEqual(cone_hits, {m["ice_slot"], m["cone_bare"]})
        self.assertNotIn(m["matcha_bare"], cone_hits)

        matcha_hits = texts("我想喝抹茶拿铁")
        self.assertIn(m["matcha_bare"], matcha_hits)
        self.assertIn(m["drink_slot"], matcha_hits)

        generic = texts("我喜欢什么")
        self.assertEqual(generic, set(m.values()))

    def test_single_char_query_not_generic(self) -> None:
        self.assertFalse(is_generic_preference_query("我喜欢猫吗"))
        self.assertFalse(is_generic_preference_query("我喜欢茶吗"))
        self.assertTrue(is_generic_preference_query("我喜欢什么"))
        self.assertFalse(is_generic_preference_query("我喜欢什么饮品"))
        tokens_cat = content_lexical_tokens("我喜欢猫吗")
        self.assertIn("c:猫", tokens_cat)
        tokens_tea = content_lexical_tokens("我喜欢茶吗")
        self.assertIn("c:茶", tokens_tea)

    def test_ice_cream_query_misses_matcha_latte(self) -> None:
        self.store.add(
            NewMemory(
                text="用户喜欢抹茶拿铁",
                category=MemoryCategory.PREFERENCE,
                importance=0.9,
            )
        )
        for q in ("我喜欢的冰激凌是什么", "我喜欢什么甜筒"):
            with self.subTest(q=q):
                result = self.recall.recall(q, limit=5)
                self.assertEqual(result.hits, ())

    def test_ice_cream_record_hits_ice_queries(self) -> None:
        self.store.add(
            NewMemory(
                text="用户喜欢的冰激凌是香草甜筒",
                category=MemoryCategory.PREFERENCE,
                importance=0.9,
            )
        )
        for q in ("我喜欢的冰激凌是什么", "我喜欢什么甜筒"):
            with self.subTest(q=q):
                result = self.recall.recall(q, limit=5)
                self.assertEqual(len(result.hits), 1)
                self.assertIn("香草甜筒", result.hits[0].record.text)

    def test_drink_query_slot_only_and_matcha_content(self) -> None:
        self.store.add(
            NewMemory(
                text="用户喜欢抹茶拿铁",
                category=MemoryCategory.PREFERENCE,
                importance=0.9,
            )
        )
        self.store.add(
            NewMemory(
                text="用户喜欢的饮品是抹茶拿铁",
                category=MemoryCategory.PREFERENCE,
                importance=0.9,
            )
        )
        drink = self.recall.recall("我喜欢什么饮品", limit=5)
        self.assertEqual(len(drink.hits), 1)
        self.assertEqual(drink.hits[0].record.text, "用户喜欢的饮品是抹茶拿铁")
        result2 = self.recall.recall("我想喝抹茶拿铁", limit=5)
        texts = {h.record.text for h in result2.hits}
        self.assertIn("用户喜欢抹茶拿铁", texts)

    def test_weak_tag_alone_does_not_cross_object(self) -> None:
        self.store.add(
            NewMemory(
                text="用户喜欢抹茶拿铁口味",
                category=MemoryCategory.PREFERENCE,
                importance=0.9,
            )
        )
        result = self.recall.recall("我喜欢什么口味的冰激凌", limit=5)
        texts = [h.record.text for h in result.hits]
        self.assertNotIn("用户喜欢抹茶拿铁口味", texts)

    def test_generic_preference_fallback(self) -> None:
        self.store.add(
            NewMemory(
                text="用户喜欢抹茶拿铁",
                category=MemoryCategory.PREFERENCE,
                importance=0.9,
            )
        )
        self.assertTrue(is_generic_preference_query("我喜欢什么"))
        self.assertFalse(is_generic_preference_query("我喜欢的冰激凌是什么"))
        result = self.recall.recall("我喜欢什么", limit=5)
        self.assertEqual(len(result.hits), 1)

    def test_english_recall_not_regressed(self) -> None:
        self.store.add(
            NewMemory(
                text="用户喜欢吃拉面",
                category=MemoryCategory.PREFERENCE,
                importance=0.9,
            )
        )
        self.store.add(
            NewMemory(
                text="user likes ramen noodles",
                category=MemoryCategory.PREFERENCE,
                importance=0.8,
            )
        )
        en = self.recall.recall("I want ramen please", limit=5)
        self.assertTrue(any("ramen" in h.record.text.lower() for h in en.hits))
        zh = self.recall.recall("今天想吃拉面吗", limit=5)
        self.assertTrue(any("拉面" in h.record.text for h in zh.hits))


class F3SkipReasonAllowlistTests(unittest.TestCase):
    def test_normalize_skip_reason_allowlist(self) -> None:
        self.assertEqual(normalize_ingestion_skip_reason("no_safe_fact_unit"), "no_safe_fact_unit")
        self.assertEqual(normalize_ingestion_skip_reason("eligibility_skip"), "eligibility_skip")
        self.assertEqual(normalize_ingestion_skip_reason(""), "")
        self.assertEqual(normalize_ingestion_skip_reason(None), "")
        self.assertEqual(normalize_ingestion_skip_reason("SECRET_USER_BODY"), "unknown")
        self.assertEqual(normalize_ingestion_skip_reason(123), "unknown")

        class Boom:
            def __str__(self) -> str:
                raise AssertionError("__str__ must not be called")

        self.assertEqual(normalize_ingestion_skip_reason(Boom()), "unknown")

    def test_malicious_skip_reason_never_logged(self) -> None:
        secret = "SECRET_USER_BODY"

        class BoomStr:
            called = False

            def __str__(self) -> str:
                BoomStr.called = True
                return secret

        class FakeDetailed:
            def __init__(self, skip_reason: object) -> None:
                self.items: tuple = ()
                self.local_candidates = 0
                self.selected = 0
                self.skip_reason = skip_reason

        class FakeExtractor:
            def __init__(self, skip_reason: object) -> None:
                self._skip = skip_reason

            def extract(self, event: MemoryTurnEvent):
                raise AssertionError("extract() should not be used when extract_detailed exists")

            def extract_detailed(self, event: MemoryTurnEvent) -> FakeDetailed:
                return FakeDetailed(self._skip)

        for raw in (secret, BoomStr()):
            with self.subTest(raw=type(raw).__name__):
                BoomStr.called = False
                with TemporaryDirectory() as tmp:
                    store = SqliteMemoryStore(Path(tmp) / "m.sqlite3")
                    service = MemoryIngestionService(
                        store, FakeExtractor(raw), queue_capacity=4
                    )
                    service.start()
                    try:
                        logger = logging.getLogger("src.memory.ingestion")
                        with self.assertLogs(logger, level="INFO") as captured:
                            self.assertTrue(
                                service.submit(_turn("别人说我喜欢香草甜筒", turn_id="sec"))
                            )
                            self.assertTrue(service.wait_until_idle(2.0))
                        joined = "\n".join(captured.output)
                        self.assertNotIn(secret, joined)
                        self.assertIn("reason=unknown", joined)
                        self.assertFalse(BoomStr.called)
                    finally:
                        service.close(drain=True, timeout=2.0)
                        store.close()

    def test_fixed_reason_codes_still_visible(self) -> None:
        with TemporaryDirectory() as tmp:
            store = SqliteMemoryStore(Path(tmp) / "m.sqlite3")
            transport = _RecordingTransport()
            extractor = OllamaMemoryExtractor(transport=transport)
            service = MemoryIngestionService(store, extractor, queue_capacity=4)
            service.start()
            try:
                logger = logging.getLogger("src.memory.ingestion")
                with self.assertLogs(logger, level="INFO") as captured:
                    ok = service.submit(_turn("别人说我喜欢香草甜筒", turn_id="z1"))
                    self.assertTrue(ok)
                    self.assertTrue(service.wait_until_idle(2.0))
                joined = "\n".join(captured.output)
                self.assertIn("accepted=0", joined)
                self.assertIn("no_safe_fact_unit", joined)
                self.assertIn("local_candidates=0", joined)
                self.assertNotIn("别人说我喜欢", joined)
                self.assertNotIn("香草甜筒", joined)
                self.assertEqual(transport.payloads, [])
            finally:
                service.close(drain=True, timeout=2.0)
                store.close()

    def test_remember_prefix_creates_with_counts(self) -> None:
        with TemporaryDirectory() as tmp:
            store = SqliteMemoryStore(Path(tmp) / "m.sqlite3")
            user = "请记住我喜欢香草甜筒"
            units = enumerate_safe_fact_units(user)
            self.assertEqual(len(units), 1)
            transport = _RecordingTransport(
                [
                    _ok(
                        [
                            {
                                "unit_id": units[0].unit_id,
                                "importance": 0.8,
                                "confidence": 0.9,
                            }
                        ]
                    )
                ]
            )
            extractor = OllamaMemoryExtractor(transport=transport)
            service = MemoryIngestionService(store, extractor, queue_capacity=4)
            service.start()
            try:
                logger = logging.getLogger("src.memory.ingestion")
                with self.assertLogs(logger, level="INFO") as captured:
                    self.assertTrue(service.submit(_turn(user, turn_id="z2")))
                    self.assertTrue(service.wait_until_idle(2.0))
                joined = "\n".join(captured.output)
                self.assertIn("accepted=1", joined)
                self.assertIn("local_candidates=1", joined)
                self.assertIn("created=1", joined)
                self.assertNotIn(user, joined)
                rows = store.list_memories(limit=10)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0].text, "用户喜欢香草甜筒")
            finally:
                service.close(drain=True, timeout=2.0)
                store.close()

    def test_content_tokens_ignore_frame(self) -> None:
        tokens = content_lexical_tokens("我喜欢的冰激凌是什么")
        self.assertTrue(
            any(t.startswith("cb:冰激") or t == "cb:淇淋" for t in tokens)
            or any("冰" in t for t in tokens)
        )
        self.assertNotIn("cb:喜欢", tokens)


if __name__ == "__main__":
    unittest.main()
