"""
memory/memory_manager.py
Core memory engine for Alex — handles capture, storage, consolidation, retrieval.
"""

import asyncio
import datetime
import json
import logging
import os
import random
import re
import uuid
from difflib import SequenceMatcher
from typing import Optional

import aiosqlite

logger = logging.getLogger("alex.memory")


# ─────────────────────────────────────────────────────────────────────────────
# SQL SCHEMAS
# ─────────────────────────────────────────────────────────────────────────────

_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS episodic_memory (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT    NOT NULL,
    timestamp    TEXT    NOT NULL,
    role         TEXT    NOT NULL CHECK(role IN ('user', 'alex')),
    content      TEXT    NOT NULL,
    consolidated INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_epi_session     ON episodic_memory(session_id);
CREATE INDEX IF NOT EXISTS idx_epi_consol      ON episodic_memory(consolidated);

CREATE TABLE IF NOT EXISTS semantic_memory (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    fact           TEXT    NOT NULL,
    category       TEXT    NOT NULL DEFAULT 'other',
    importance     INTEGER NOT NULL DEFAULT 5 CHECK(importance BETWEEN 1 AND 10),
    source_session TEXT    NOT NULL,
    created_at     TEXT    NOT NULL,
    last_accessed  TEXT    NOT NULL,
    access_count   INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_sem_importance ON semantic_memory(importance DESC);
CREATE INDEX IF NOT EXISTS idx_sem_accessed   ON semantic_memory(last_accessed DESC);

CREATE TABLE IF NOT EXISTS session_index (
    session_id TEXT    PRIMARY KEY,
    started_at TEXT    NOT NULL,
    ended_at   TEXT,
    turn_count INTEGER NOT NULL DEFAULT 0,
    summary    TEXT,
    mood       TEXT,
    topics     TEXT
);

CREATE TABLE IF NOT EXISTS procedural_memory (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT    NOT NULL,
    steps          TEXT    NOT NULL,
    trigger_phrase TEXT,
    source_session TEXT    NOT NULL,
    created_at     TEXT    NOT NULL,
    use_count      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS preference_rules (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    rule           TEXT    NOT NULL,
    importance     INTEGER NOT NULL DEFAULT 7,
    source_session TEXT    NOT NULL,
    created_at     TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS failure_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_message   TEXT    NOT NULL,
    tool           TEXT    NOT NULL,
    reason         TEXT    NOT NULL,
    args_json      TEXT,
    session_id     TEXT    NOT NULL,
    created_at     TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_fail_session ON failure_log(session_id);
CREATE INDEX IF NOT EXISTS idx_fail_created ON failure_log(created_at DESC);
"""

# ─────────────────────────────────────────────────────────────────────────────
# CONSOLIDATION PROMPT
# ─────────────────────────────────────────────────────────────────────────────

CONSOLIDATION_PROMPT = """\
You are Alex's memory consolidator. Analyze the conversation and extract EVERY \
detail that Alex should permanently remember about the user and their life.

Output ONLY valid JSON — no markdown fences, no explanation, no preamble. \
Start your response with '{' and end with '}'.

{
  "summary": "2-3 sentences describing what happened in this conversation",
  "mood": "one word: happy | sad | excited | serious | playful | mixed | neutral",
  "topics": ["topic1", "topic2"],
  "facts": [
    {
      "text": "concise fact in present tense (e.g. User's name is Arjun)",
      "category": "preference|person|event|topic|emotion|relationship|habit|goal|other",
      "importance": 8
    }
  ],
  "procedures": [
    {
      "name": "short name e.g. morning routine",
      "trigger_phrase": "when user says X",
      "steps": "step 1, then step 2, ..."
    }
  ],
  "preferences": [
    {
      "rule": "e.g. Don't type without asking when Cursor is focused",
      "importance": 8
    }
  ]
}

EXTRACTION RULES:
1. Extract EVERYTHING — names, nicknames, ages, locations, family members, pets,
   jobs, hobbies, likes, dislikes, opinions, plans, dreams, fears, jokes shared,
   things user asked Alex to remember, stories told, emotions expressed.
2. Be AGGRESSIVE. Over-extract rather than under-extract. Even small details matter.
3. Handle Hindi, English, and Japanese. Extract facts from ALL languages present.
4. Importance scale:
   - 10: Name, identity, critical relationships (parent, partner, best friend)
   - 8-9: Major preferences, life events, important goals
   - 6-7: Regular preferences, habits, people in their life
   - 4-5: Context details, topics discussed
   - 1-3: Minor passing details
5. If user said "remember this" or "don't forget", set importance >= 9.
6. Write facts as short, clear statements, not sentences from the transcript.
7. Deduplicate — include each fact only once.
8. If user's name was stated, ALWAYS include it as importance 10.
9. Extract procedures when user describes repeatable workflows (deploy, backup, morning routine).
10. Extract preference rules when user states how Alex should behave (don't type without asking, always search first, etc.).

CONVERSATION TRANSCRIPT:
{transcript}
"""


def _similarity(a: str, b: str) -> float:
    """Return 0.0–1.0 similarity between two strings (case-insensitive)."""
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _clean_json(raw: str) -> str:
    """Strip markdown fences and leading/trailing garbage from LLM JSON output."""
    raw = raw.strip()
    # Remove ```json ... ``` or ``` ... ```
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    # Find the first { and last }
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end != -1 and end > start:
        return raw[start : end + 1]
    return raw


# ─────────────────────────────────────────────────────────────────────────────
# MEMORY MANAGER
# ─────────────────────────────────────────────────────────────────────────────

class MemoryManager:
    """
    Handles all persistent memory for Alex.

    Usage:
        mem = MemoryManager("memory/alex_memory.db", session_id)
        await mem.initialize()
        # ... session runs, capture_turn() called ...
        await mem.consolidate_session()
        await mem.close()
    """

    SIMILARITY_THRESHOLD = 0.82  # facts above this are considered duplicates

    def __init__(self, db_path: str, session_id: str) -> None:
        self.db_path = db_path
        self.session_id = session_id
        self.db: Optional[aiosqlite.Connection] = None
        self._captured_turns: list[tuple[str, str]] = []
        self._pending_tasks: list[asyncio.Task] = []
        self._closed = False

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def initialize(self) -> None:
        """Open DB connection, create tables, register this session."""
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        self.db = await aiosqlite.connect(self.db_path)
        self.db.row_factory = aiosqlite.Row
        await self.db.executescript(_SCHEMA)
        now = _now()
        await self.db.execute(
            "INSERT OR IGNORE INTO session_index (session_id, started_at) VALUES (?, ?)",
            (self.session_id, now),
        )
        await self.db.commit()
        logger.info("Memory DB ready at %s (session %s)", self.db_path, self.session_id[:8])

    async def close(self) -> None:
        """Close DB connection. Safe to call multiple times."""
        if self._closed:
            return
        self._closed = True
        if self.db:
            try:
                await self.db.close()
            except Exception:
                pass
            self.db = None

    async def __aenter__(self) -> "MemoryManager":
        await self.initialize()
        return self

    async def __aexit__(self, *_) -> None:
        await self.close()

    # ── Turn Capture ──────────────────────────────────────────────────────────

    async def capture_turn(self, role: str, content: str) -> None:
        """
        Save one conversation turn to episodic memory.
        Safe to call even if DB is closed (will silently skip).
        """
        if self._closed or self.db is None:
            return
        content = content.strip()
        if not content:
            return

        # Deduplicate consecutive identical turns (echo artifacts)
        if self._captured_turns and self._captured_turns[-1] == (role, content):
            return

        self._captured_turns.append((role, content))
        try:
            await self.db.execute(
                "INSERT INTO episodic_memory (session_id, timestamp, role, content) "
                "VALUES (?, ?, ?, ?)",
                (self.session_id, _now(), role, content),
            )
            await self.db.commit()
        except Exception as e:
            logger.warning("capture_turn failed: %s", e)

    def schedule_capture(self, role: str, content: str) -> None:
        """
        Thread-safe, fire-and-forget version of capture_turn.
        Use this from synchronous event callbacks.
        """
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                task = loop.create_task(self.capture_turn(role, content))
                self._pending_tasks.append(task)
                task.add_done_callback(self._pending_tasks.remove)
        except Exception as e:
            logger.warning("schedule_capture failed: %s", e)

    async def drain_pending(self) -> None:
        """Wait for all in-flight capture tasks to finish before consolidation."""
        if self._pending_tasks:
            await asyncio.gather(*self._pending_tasks, return_exceptions=True)
        # Extra yield to let any last callbacks complete
        await asyncio.sleep(0.1)

    # ── Consolidation ─────────────────────────────────────────────────────────

    async def consolidate_session(self) -> None:
        """
        Run post-session consolidation:
        1. Build transcript from captured turns
        2. Try Gemini Flash (text) to extract facts + summary
        3. Fall back to local extraction if API is unavailable/quota exceeded
        4. Upsert facts into semantic_memory
        5. Write session summary to session_index
        """
        if not self._captured_turns:
            logger.info("No turns to consolidate for session %s", self.session_id[:8])
            # Don't pollute long-term memory with empty sessions.
            await self.db.execute(
                "DELETE FROM session_index WHERE session_id = ?", (self.session_id,)
            )
            await self.db.commit()
            return

        logger.info(
            "Consolidating session %s (%d turns)...",
            self.session_id[:8],
            len(self._captured_turns),
        )

        transcript = self._build_transcript()

        result = await self._call_consolidation_llm(transcript)
        if result is None:
            logger.info("AI consolidation unavailable — using local fallback")
            result = self._local_consolidation(transcript)

        await self._save_facts(result.get("facts", []))
        await self._save_procedures(result.get("procedures", []))
        await self._save_preferences(result.get("preferences", []))
        await self._close_session_record(
            summary=result.get("summary", ""),
            mood=result.get("mood", ""),
            topics=result.get("topics", []),
        )
        await self._mark_turns_consolidated()
        logger.info("Consolidation complete for session %s", self.session_id[:8])

    def _build_transcript(self) -> str:
        lines = []
        for role, content in self._captured_turns:
            label = "User" if role == "user" else "Alex"
            lines.append(f"{label}: {content}")
        return "\n".join(lines)

    def _local_consolidation(self, transcript: str) -> dict:
        """
        Local fallback consolidation — works without any API calls.
        Extracts basic facts using regex patterns and heuristics.
        """
        user_turns = [c for r, c in self._captured_turns if r == "user"]
        all_text = ". ".join(t.rstrip(".!?") for t in user_turns)

        now_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M")
        turn_count = len(self._captured_turns)
        summary = f"Conversation with {turn_count} messages on {now_str}."
        if user_turns:
            last = user_turns[-1][:80]
            summary += f" Last thing user said: \"{last}\""

        facts = []
        seen_texts = set()

        def add_fact(text: str, category: str, importance: int):
            key = text.lower().strip()
            if key and key not in seen_texts:
                seen_texts.add(key)
                facts.append({"text": text, "category": category, "importance": importance})

        _STOP = {"and", "i", "my", "the", "a", "an", "to", "for", "with", "in", "on", "at", "of", "it", "is", "me", "you"}

        def _clean_word(w: str) -> str:
            w = w.strip().rstrip(".,!?:;")
            return w if w.lower() not in _STOP else ""

        # Name extraction
        name_patterns = [
            r"(?:my name is|i'm|i am|call me)\s+(\w+)",
            r"(?:name'?s)\s+(\w+)",
        ]
        for pat in name_patterns:
            m = re.search(pat, all_text, re.IGNORECASE)
            if m:
                name = m.group(1).strip().rstrip(".,!?")
                if name.lower() not in ("alex", "user"):
                    add_fact(f"User's name is {name}", "person", 10)

        # Preferences: like/love/enjoy/hate/dislike
        pref_matches = re.finditer(
            r"(?:i|user)\s+(?:really\s+)?(like|love|enjoy|adore|hate|dislike|don't\s+like|can't\s+stand)\s+(\w+(?:\s+\w+)?)",
            all_text, re.IGNORECASE,
        )
        for m in pref_matches:
            verb = m.group(1).lower()
            thing = _clean_word(m.group(2))
            if thing:
                if verb in ("hate", "dislike", "don't like", "can't stand"):
                    add_fact(f"User dislikes {thing}", "preference", 7)
                else:
                    add_fact(f"User likes {thing}", "preference", 7)

        # Possessions: I have a/an/my
        poss_matches = re.finditer(
            r"(?:(?:i have|i've got)\s+(?:a|an|my)?\s*|my\s+)(\w+(?:\s+\w+)?(?:named?\s+\w+)?)",
            all_text, re.IGNORECASE,
        )
        for m in poss_matches:
            thing = m.group(1).strip().rstrip(".,!?")
            cleaned = _clean_word(thing.split()[0])
            if cleaned and cleaned not in ("name",) and len(thing) > 2 and not thing.lower().startswith("name is"):
                add_fact(f"User has {thing}", "other", 6)

        # Location
        loc_matches = re.finditer(
            r"(?:i live in|i'm from|i am from|i am located in|i stay in)\s+(\w+)",
            all_text, re.IGNORECASE,
        )
        for m in loc_matches:
            loc = _clean_word(m.group(1))
            if loc and loc.lower() not in ("it", "here", "there"):
                add_fact(f"User is from {loc}", "other", 7)

        # Age
        age_matches = re.finditer(
            r"(?:i'?m?|i am)\s+(\d+)\s*(?:years?\s*old|yo)",
            all_text, re.IGNORECASE,
        )
        for m in age_matches:
            add_fact(f"User is {m.group(1)} years old", "person", 8)

        # Explicit remember requests
        if re.search(r"(?:remember|don't forget|note that|keep in mind)", all_text, re.IGNORECASE):
            sentences = re.split(r'[.!?]+', all_text)
            for sent in sentences:
                if re.search(r"(?:remember|don't forget)", sent, re.IGNORECASE):
                    sent = sent.strip()
                    if sent and len(sent) > 10:
                        add_fact(sent[:120], "other", 9)

        # Save user statements as topic facts if we have very few facts
        if len(facts) < 3:
            for turn in user_turns:
                turn = turn.strip()
                if len(turn) > 15 and turn.lower() not in [f["text"].lower() for f in facts]:
                    add_fact(turn[:100], "topic", 3)
                    if len(facts) >= 5:
                        break

        # Mood detection
        mood = "neutral"
        happy_words = ["happy", "great", "awesome", "nice", "love", "wonderful", "amazing", "good"]
        sad_words = ["sad", "bad", "terrible", "awful", "hate", "horrible", "depressed"]
        excited_words = ["excited", "amazing", "incredible", "wow", "awesome", "cool"]
        text_lower = all_text.lower()
        if any(w in text_lower for w in excited_words):
            mood = "excited"
        elif any(w in text_lower for w in happy_words):
            mood = "happy"
        elif any(w in text_lower for w in sad_words):
            mood = "sad"

        return {
            "summary": summary,
            "mood": mood,
            "topics": [f["category"] for f in facts[:5]],
            "facts": facts,
        }

    async def _call_consolidation_llm(self, transcript: str) -> Optional[dict]:
        """Call Gemini Flash (text API) to consolidate the transcript.

        Gemini occasionally returns 503/429 under load. A single failed call
        used to drop straight to the weak local fallback, so retry briefly
        with backoff before giving up.
        """
        from google import genai
        from google.genai import types as gtypes

        attempts = 3
        delays = (0.75, 2.0)
        last_error = "unknown error"

        api_key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY", "")
        client = genai.Client(api_key=api_key)
        prompt = CONSOLIDATION_PROMPT.replace("{transcript}", transcript)

        for attempt in range(1, attempts + 1):
            try:
                response = await client.aio.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=prompt,
                    config=gtypes.GenerateContentConfig(
                        temperature=0.1,
                        max_output_tokens=8192,
                    ),
                )
                raw = response.text or ""
                return json.loads(_clean_json(raw))

            except json.JSONDecodeError as e:
                last_error = f"JSON parse failed: {e}"
            except Exception as e:
                last_error = str(e) or e.__class__.__name__

            if attempt < attempts:
                wait = delays[min(attempt - 1, len(delays) - 1)] + random.uniform(0, 0.3)
                logger.warning(
                    "Consolidation attempt %d/%d failed (%s) — retrying in %.1fs",
                    attempt,
                    attempts,
                    last_error[:120],
                    wait,
                )
                await asyncio.sleep(wait)

        logger.error(
            "Consolidation LLM call failed after %d attempts: %s", attempts, last_error
        )
        return None

    async def _save_facts(self, facts: list[dict]) -> None:
        """Upsert facts into semantic_memory with fuzzy deduplication."""
        if not facts:
            return

        # Load all existing facts for fuzzy comparison
        cursor = await self.db.execute("SELECT id, fact, importance FROM semantic_memory")
        existing = await cursor.fetchall()

        now = _now()
        for fact_data in facts:
            fact_text = (fact_data.get("text") or "").strip()
            if not fact_text:
                continue
            category = fact_data.get("category", "other")
            importance = max(1, min(10, int(fact_data.get("importance", 5))))

            # Fuzzy dedup: find any existing fact that's very similar
            matched_id = None
            matched_importance = 0
            for row in existing:
                if _similarity(fact_text, row["fact"]) >= self.SIMILARITY_THRESHOLD:
                    matched_id = row["id"]
                    matched_importance = row["importance"]
                    break

            try:
                if matched_id is None:
                    # New fact — insert
                    await self.db.execute(
                        """INSERT INTO semantic_memory
                           (fact, category, importance, source_session,
                            created_at, last_accessed, access_count)
                           VALUES (?, ?, ?, ?, ?, ?, 1)""",
                        (fact_text, category, importance,
                         self.session_id, now, now),
                    )
                    # Add to local cache so next facts in this batch also deduplicate
                    new_id = (await (await self.db.execute("SELECT last_insert_rowid()")).fetchone())[0]
                    existing.append({"id": new_id, "fact": fact_text, "importance": importance})
                else:
                    # Existing fact — bump importance if higher, update access
                    new_importance = max(matched_importance, importance)
                    await self.db.execute(
                        """UPDATE semantic_memory
                           SET importance = ?, last_accessed = ?, access_count = access_count + 1
                           WHERE id = ?""",
                        (new_importance, now, matched_id),
                    )
            except Exception as e:
                logger.warning("Failed to save fact '%s': %s", fact_text[:40], e)

        await self.db.commit()

    async def _close_session_record(
        self,
        summary: Optional[str],
        mood: str = "",
        topics: list = None,
    ) -> None:
        if not summary:
            date_str = _now()[:10]
            summary = f"Conversation with {len(self._captured_turns)} messages on {date_str}."
        topics_json = json.dumps(topics or [], ensure_ascii=False)
        now = _now()
        await self.db.execute(
            """UPDATE session_index
               SET ended_at = ?, turn_count = ?, summary = ?, mood = ?, topics = ?
               WHERE session_id = ?""",
            (now, len(self._captured_turns), summary, mood, topics_json, self.session_id),
        )
        await self.db.commit()

    async def _mark_turns_consolidated(self) -> None:
        await self.db.execute(
            "UPDATE episodic_memory SET consolidated = 1 WHERE session_id = ?",
            (self.session_id,),
        )
        await self.db.commit()

    # ── Retrieval ─────────────────────────────────────────────────────────────

    async def retrieve_context(self) -> dict:
        """
        Fetch everything Alex needs to remember at the start of a session:
        - Last 5 session summaries
        - Top 50 facts (by importance, then recency)
        - Last 12 turns from the most recent previous session
        """
        ctx: dict = {
            "session_summaries": [],
            "facts": [],
            "recent_turns": [],
            "procedures": [],
            "preferences": [],
            "failures": [],
        }
        if self.db is None:
            return ctx
        try:
            ctx["session_summaries"] = await self._get_session_summaries(limit=5)
            ctx["facts"] = await self._get_top_facts(limit=50)
            ctx["recent_turns"] = await self._get_recent_turns(limit=12)
            ctx["procedures"] = await self._get_procedures(limit=10)
            ctx["preferences"] = await self._get_preferences(limit=15)
            ctx["failures"] = await self._get_recent_failures(limit=8)
        except Exception as e:
            logger.error("retrieve_context failed: %s", e)
        return ctx

    async def _get_session_summaries(self, limit: int) -> list[dict]:
        cursor = await self.db.execute(
            """SELECT session_id, started_at, summary, mood, topics
               FROM session_index
               WHERE session_id != ? AND summary IS NOT NULL AND summary != ''
               ORDER BY started_at DESC
               LIMIT ?""",
            (self.session_id, limit),
        )
        rows = await cursor.fetchall()
        result = []
        for row in rows:
            try:
                topics = json.loads(row["topics"]) if row["topics"] else []
            except Exception:
                topics = []
            result.append({
                "date": (row["started_at"] or "")[:10],
                "summary": row["summary"] or "",
                "mood": row["mood"] or "",
                "topics": topics,
            })
        return result

    async def _get_top_facts(self, limit: int) -> list[dict]:
        cursor = await self.db.execute(
            """SELECT fact, category, importance, last_accessed
               FROM semantic_memory
               ORDER BY importance DESC, last_accessed DESC
               LIMIT ?""",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [
            {
                "text": row["fact"],
                "category": row["category"],
                "importance": row["importance"],
            }
            for row in rows
        ]

    async def _get_recent_turns(self, limit: int) -> list[dict]:
        """Get the last N turns from the most recent completed session."""
        cursor = await self.db.execute(
            """SELECT session_id FROM session_index
               WHERE session_id != ? AND summary IS NOT NULL AND summary != ''
               ORDER BY started_at DESC LIMIT 1""",
            (self.session_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return []
        prev_session_id = row["session_id"]

        cursor = await self.db.execute(
            """SELECT role, content FROM episodic_memory
               WHERE session_id = ?
               ORDER BY id DESC LIMIT ?""",
            (prev_session_id, limit),
        )
        rows = await cursor.fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

    # ── Failure logging (decision layer) ────────────────────────────────────

    async def record_failure(
        self,
        user_message: str,
        tool: str,
        reason: str,
        args: dict | None = None,
    ) -> None:
        if self._closed or self.db is None:
            return
        try:
            await self.db.execute(
                """INSERT INTO failure_log
                   (user_message, tool, reason, args_json, session_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    user_message[:500],
                    tool,
                    reason[:500],
                    json.dumps(args or {}, ensure_ascii=False),
                    self.session_id,
                    _now(),
                ),
            )
            await self.db.commit()
        except Exception as e:
            logger.warning("record_failure failed: %s", e)

    async def _save_procedures(self, procedures: list[dict]) -> None:
        if not procedures:
            return
        now = _now()
        for proc in procedures:
            name = (proc.get("name") or "").strip()
            steps = (proc.get("steps") or "").strip()
            if not name or not steps:
                continue
            trigger = (proc.get("trigger_phrase") or "").strip()
            try:
                await self.db.execute(
                    """INSERT INTO procedural_memory
                       (name, steps, trigger_phrase, source_session, created_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (name, steps, trigger, self.session_id, now),
                )
            except Exception as e:
                logger.warning("Failed to save procedure '%s': %s", name[:30], e)
        await self.db.commit()

    async def _save_preferences(self, preferences: list[dict]) -> None:
        if not preferences:
            return
        now = _now()
        for pref in preferences:
            rule = (pref.get("rule") or "").strip()
            if not rule:
                continue
            importance = max(1, min(10, int(pref.get("importance", 7))))
            try:
                await self.db.execute(
                    """INSERT INTO preference_rules
                       (rule, importance, source_session, created_at)
                       VALUES (?, ?, ?, ?)""",
                    (rule, importance, self.session_id, now),
                )
            except Exception as e:
                logger.warning("Failed to save preference: %s", e)
        await self.db.commit()

    async def _get_procedures(self, limit: int) -> list[dict]:
        cursor = await self.db.execute(
            """SELECT name, steps, trigger_phrase FROM procedural_memory
               ORDER BY use_count DESC, created_at DESC LIMIT ?""",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [
            {"name": r["name"], "steps": r["steps"], "trigger": r["trigger_phrase"] or ""}
            for r in rows
        ]

    async def _get_preferences(self, limit: int) -> list[dict]:
        cursor = await self.db.execute(
            """SELECT rule, importance FROM preference_rules
               ORDER BY importance DESC, created_at DESC LIMIT ?""",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [{"rule": r["rule"], "importance": r["importance"]} for r in rows]

    async def _get_recent_failures(self, limit: int) -> list[dict]:
        cursor = await self.db.execute(
            """SELECT user_message, tool, reason, created_at FROM failure_log
               ORDER BY created_at DESC LIMIT ?""",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [
            {
                "user_message": r["user_message"],
                "tool": r["tool"],
                "reason": r["reason"],
                "date": (r["created_at"] or "")[:10],
            }
            for r in rows
        ]

    # ── Context Block Builder ─────────────────────────────────────────────────

    def build_memory_block(self, context: dict) -> str:
        """
        Format retrieved context as a structured memory block
        to prepend to Alex's system instructions.
        """
        lines = [
            "+========================================+",
            "|       ALEX'S PERSISTENT MEMORY         |",
            "+========================================+",
            "",
        ]

        summaries = context.get("session_summaries", [])
        if summaries:
            lines.append(">> PREVIOUS SESSIONS")
            for s in summaries:
                mood = f" [{s['mood']}]" if s.get("mood") else ""
                lines.append(f"  {s['date']}{mood}: {s['summary']}")
            lines.append("")
        else:
            lines.append(">> This is our first conversation - no previous memory.")
            lines.append("")

        facts = context.get("facts", [])
        if facts:
            lines.append(">> EVERYTHING ALEX KNOWS ABOUT THE USER")
            category_order = [
                "person", "preference", "relationship",
                "goal", "habit", "emotion", "event", "topic", "other",
            ]
            category_labels = {
                "person": "People & Identity",
                "preference": "Likes & Dislikes",
                "relationship": "Relationships",
                "goal": "Goals & Dreams",
                "habit": "Habits & Routines",
                "emotion": "Emotions & Feelings",
                "event": "Events & Experiences",
                "topic": "Topics of Interest",
                "other": "Other Details",
            }
            grouped: dict[str, list[str]] = {}
            for f in facts:
                cat = f.get("category", "other")
                grouped.setdefault(cat, []).append(f["text"])

            for cat in category_order:
                if cat in grouped:
                    lines.append(f"  [{category_labels.get(cat, cat.title())}]")
                    for text in grouped[cat]:
                        lines.append(f"    - {text}")
            lines.append("")

        recent = context.get("recent_turns", [])
        if recent:
            lines.append(">> WHERE WE LEFT OFF (last conversation ending)")
            for t in recent:
                label = "User" if t["role"] == "user" else "Alex"
                lines.append(f"  {label}: {t['content']}")
            lines.append("")

        prefs = context.get("preferences", [])
        if prefs:
            lines.append(">> THE USER'S RULES FOR HOW I SHOULD BEHAVE")
            for p in prefs:
                lines.append(f"  - {p['rule']}")
            lines.append("")

        procedures = context.get("procedures", [])
        if procedures:
            lines.append(">> LEARNED WORKFLOWS (use when triggered)")
            for proc in procedures:
                trigger = f" (when: {proc['trigger']})" if proc.get("trigger") else ""
                lines.append(f"  [{proc['name']}]{trigger}: {proc['steps']}")
            lines.append("")

        failures = context.get("failures", [])
        if failures:
            lines.append(">> PAST MISTAKES — DO NOT REPEAT")
            for f in failures[:5]:
                lines.append(
                    f"  - Wrong tool '{f['tool']}' for: \"{f['user_message'][:60]}\" "
                    f"→ {f['reason'][:80]}"
                )
            lines.append("")

        lines.append("Use ALL of the above naturally in conversation.")
        lines.append("Never say 'according to my memory' - just know it.")
        lines.append("=" * 42)

        result = "\n".join(lines)
        # Cap instruction size to prevent LiveKit speech timeout on interruption
        # (Gemini takes too long to process massive context blocks)
        MAX_MEMORY_CHARS = 12000
        if len(result) > MAX_MEMORY_CHARS:
            result = result[:MAX_MEMORY_CHARS] + "\n\n[Memory truncated — too large]"
        return result


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()