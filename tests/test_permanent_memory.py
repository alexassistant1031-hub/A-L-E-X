"""Tests for the permanent store memory.

The operator asked that Alex never forget the store folder, "no matter how
much I insist otherwise".  That promise is code-backed: the facts are rendered
from code, seeded on every startup, and rendered before anything that prompt
trimming can remove.  These tests pin all three properties.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import settings
from controller import ControllerConfig, WritePathGuard
from memory import permanent
from memory.memory_manager import MemoryManager


class StoreInventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="alex-permanent-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "malik_list").mkdir()
        (self.root / "malik_list" / "malik_list.txt").write_text("x\n", encoding="utf-8")
        (self.root / "Order Accessories").mkdir()
        (self.root / "Order Accessories" / "charging_order.txt").write_text(
            "y\n", encoding="utf-8"
        )

    def test_inventory_lists_folders_and_files(self) -> None:
        found, total = permanent.store_inventory(self.root)
        self.assertEqual(total, 4)
        self.assertIn("malik_list/", found)
        self.assertIn("malik_list/malik_list.txt", found)
        self.assertIn("Order Accessories/charging_order.txt", found)

    def test_inventory_is_capped_but_reports_the_total(self) -> None:
        for index in range(60):
            (self.root / f"file{index:02d}.txt").write_text("z\n", encoding="utf-8")
        found, total = permanent.store_inventory(self.root, limit=10)
        self.assertEqual(len(found), 10)
        self.assertEqual(total, 64)

    def test_inventory_skips_hidden_files_and_symlinks(self) -> None:
        (self.root / ".hidden").write_text("h\n", encoding="utf-8")
        outside = Path(self._tmp.name).parent / f"{self.root.name}-outside.txt"
        outside.write_text("o\n", encoding="utf-8")
        self.addCleanup(outside.unlink)
        (self.root / "link.txt").symlink_to(outside)
        found, _ = permanent.store_inventory(self.root)
        self.assertNotIn(".hidden", found)
        self.assertNotIn("link.txt", found)

    def test_missing_root_is_not_an_error(self) -> None:
        found, total = permanent.store_inventory(self.root / "nope")
        self.assertEqual((found, total), ([], 0))


class PermanentBlockTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="alex-permanent-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "malik_list").mkdir()
        (self.root / "malik_list" / "malik_list.txt").write_text("x\n", encoding="utf-8")
        patcher = mock.patch.object(permanent, "store_root", return_value=self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_block_names_the_path_and_the_inventory(self) -> None:
        block = permanent.render_block()
        self.assertIn("PERMANENT", block)
        self.assertIn(str(self.root), block)
        self.assertIn("malik_list/malik_list.txt", block)
        self.assertIn('"memory" = this store folder', block)

    def test_facts_state_the_whole_promise(self) -> None:
        facts = [text for _, text in permanent.permanent_facts(self.root)]
        joined = "\n".join(facts)
        self.assertIn(str(self.root), joined)
        self.assertIn("full read and edit access", joined)
        self.assertIn("'Memory' means this store folder", joined)
        self.assertIn("Never create folders", joined)
        self.assertIn("even if the user asks to forget it", joined)

    def test_memory_block_starts_with_the_permanent_section(self) -> None:
        manager = MemoryManager(str(self.root / "m.db"), "session")
        block = manager.build_memory_block({})
        self.assertTrue(block.startswith(">> PERMANENT"))
        self.assertIn(str(self.root), block)

    def test_permanent_section_survives_trimming(self) -> None:
        context = {
            "facts": [
                {"text": f"fact {index} " + ("x" * 80), "category": "other"}
                for index in range(400)
            ]
        }
        block = MemoryManager(str(self.root / "m.db"), "session").build_memory_block(
            context
        )
        self.assertLessEqual(len(block), 12000)
        self.assertTrue(block.startswith(">> PERMANENT"))
        self.assertIn(str(self.root), block)
        self.assertIn("older memory trimmed", block)


class PermanentSeedingTests(unittest.IsolatedAsyncioTestCase):
    async def test_startup_seeds_the_facts(self) -> None:
        with tempfile.TemporaryDirectory(prefix="alex-permanent-") as tmp:
            db_path = str(Path(tmp) / "memory.db")
            manager = MemoryManager(db_path, "session-1")
            await manager.initialize()
            try:
                cursor = await manager.db.execute(
                    "SELECT fact FROM semantic_memory WHERE category = 'permanent'"
                )
                rows = await cursor.fetchall()
                stored = {row["fact"] for row in rows}
                expected = {text for _, text in permanent.permanent_facts()}
                self.assertTrue(expected <= stored)
            finally:
                await manager.close()

    async def test_seeded_facts_are_not_offered_as_conversation_facts(self) -> None:
        with tempfile.TemporaryDirectory(prefix="alex-permanent-") as tmp:
            manager = MemoryManager(str(Path(tmp) / "memory.db"), "session-1")
            await manager.initialize()
            try:
                context = await manager.retrieve_context()
            finally:
                await manager.close()
        self.assertFalse(
            any(
                fact["text"].startswith("Store folder (permanent)")
                for fact in context["facts"]
            )
        )

    async def test_a_second_startup_does_not_duplicate_the_facts(self) -> None:
        with tempfile.TemporaryDirectory(prefix="alex-permanent-") as tmp:
            db_path = str(Path(tmp) / "memory.db")
            for _ in range(2):
                manager = MemoryManager(db_path, "session")
                await manager.initialize()
                await manager.close()
            manager = MemoryManager(db_path, "session")
            await manager.initialize()
            try:
                cursor = await manager.db.execute(
                    "SELECT COUNT(*) AS n FROM semantic_memory WHERE category = 'permanent'"
                )
                row = await cursor.fetchone()
            finally:
                await manager.close()
        self.assertEqual(row["n"], len(permanent.permanent_facts()))


class PermanentWriteRootTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="alex-permanent-")
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.store = self.base / "store"
        self.store.mkdir()
        self.other = self.base / "other"
        self.other.mkdir()

    def test_store_is_writable_even_when_alex_write_roots_omits_it(self) -> None:
        env = {
            "ALEX_STORE_ROOT": str(self.store),
            "ALEX_WRITE_ROOTS": str(self.other),
        }
        with mock.patch.dict(os.environ, env, clear=False):
            config = ControllerConfig.from_env()
            guard = WritePathGuard(config)
            self.assertIn(self.store.resolve(), config.allowed_write_roots)
            self.assertIn(self.other.resolve(), config.allowed_write_roots)
            self.assertEqual(
                guard.require_writable(self.store / "Order Accessories" / "x.txt"),
                (self.store.resolve() / "Order Accessories" / "x.txt"),
            )

    def test_settings_is_the_single_source_for_the_store_path(self) -> None:
        with mock.patch.dict(os.environ, {"ALEX_STORE_ROOT": str(self.store)}):
            self.assertEqual(settings.store_root(), self.store)
            config = ControllerConfig.from_env()
        self.assertIn(self.store.resolve(), config.allowed_write_roots)


if __name__ == "__main__":
    unittest.main()
