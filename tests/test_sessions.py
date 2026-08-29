from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codex_sm import sessions


THREAD_COLUMNS = """
    id TEXT,
    cwd TEXT,
    name TEXT,
    title TEXT,
    first_user_message TEXT,
    preview TEXT,
    model TEXT,
    source TEXT,
    tokens_used INTEGER,
    created_at INTEGER,
    updated_at INTEGER,
    rollout_path TEXT,
    git_branch TEXT,
    archived INTEGER NOT NULL DEFAULT 0
"""


class DatabaseTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.home = Path(self.tempdir.name)

    def create_database(
        self,
        rows: list[dict] | None = None,
        *,
        filename: str = "state_1.sqlite",
        columns: str = THREAD_COLUMNS,
    ) -> Path:
        path = self.home / filename
        con = sqlite3.connect(path)
        self.addCleanup(con.close)
        con.execute(f"CREATE TABLE threads ({columns})")
        for row in rows or []:
            names = list(row)
            placeholders = ", ".join("?" for _ in names)
            con.execute(
                f"INSERT INTO threads ({', '.join(names)}) VALUES ({placeholders})",
                [row[name] for name in names],
            )
        con.commit()
        return path


class SessionTests(unittest.TestCase):
    def make_session(self, **overrides) -> sessions.Session:
        values = {
            "id": "abc12345-rest-of-id",
            "cwd": "/work",
            "title": "A title",
            "model": "gpt-test",
            "source": "cli",
            "tokens": 42,
            "created_at": 100,
            "updated_at": 200,
            "rollout_path": "/tmp/rollout.jsonl",
            "git_branch": "main",
            "preview": "preview",
            "name": None,
        }
        values.update(overrides)
        return sessions.Session(**values)

    def test_dataclass_stores_fields_and_has_expected_defaults(self) -> None:
        session = self.make_session()

        self.assertEqual(session.id, "abc12345-rest-of-id")
        self.assertEqual(session.tokens, 42)
        self.assertEqual(session.rollout_path, "/tmp/rollout.jsonl")
        self.assertEqual(session.git_branch, "main")
        self.assertEqual(session.status, sessions.STATUS_READY)
        self.assertIsNone(session.summary)
        self.assertEqual(session.summary_state, "none")

    def test_short_id_is_text_before_first_hyphen(self) -> None:
        self.assertEqual(self.make_session().short_id, "abc12345")
        self.assertEqual(self.make_session(id="unhyphenated").short_id, "unhyphenated")
        self.assertEqual(self.make_session(id="").short_id, "")

    def test_age_for_missing_timestamp(self) -> None:
        self.assertEqual(self.make_session(updated_at=0).age, "?")

    def test_age_formats_seconds_minutes_hours_and_days(self) -> None:
        now = 1_000_000
        with mock.patch.object(sessions.time, "time", return_value=now):
            self.assertEqual(self.make_session(updated_at=now - 59).age, "59s")
            self.assertEqual(self.make_session(updated_at=now - 60).age, "1m")
            self.assertEqual(self.make_session(updated_at=now - 3_599).age, "59m")
            self.assertEqual(self.make_session(updated_at=now - 3_600).age, "1h")
            self.assertEqual(self.make_session(updated_at=now - 86_399).age, "23h")
            self.assertEqual(self.make_session(updated_at=now - 86_400).age, "1d")

    def test_age_clamps_future_timestamp_to_zero(self) -> None:
        with mock.patch.object(sessions.time, "time", return_value=100):
            self.assertEqual(self.make_session(updated_at=200).age, "0s")


class LoadSessionsTests(DatabaseTestCase):
    def test_returns_empty_list_when_no_database_exists(self) -> None:
        self.assertEqual(sessions.load_sessions(str(self.home)), [])

    def test_returns_empty_list_for_empty_threads_table(self) -> None:
        self.create_database()
        self.assertEqual(sessions.load_sessions(str(self.home)), [])

    def test_loads_fields_ignores_archived_rows_and_sorts_newest_first(self) -> None:
        missing_rollout = self.home / "does-not-exist.jsonl"
        self.create_database(
            [
                {
                    "id": "older-id",
                    "cwd": "/older",
                    "name": None,
                    "title": "Older title",
                    "first_user_message": "Older message",
                    "preview": "Older preview",
                    "model": "model-a",
                    "source": "cli",
                    "tokens_used": 12,
                    "created_at": 10,
                    "updated_at": 20,
                    "rollout_path": str(missing_rollout),
                    "git_branch": "dev",
                    "archived": 0,
                },
                {
                    "id": "newer-id",
                    "cwd": "/newer",
                    "name": "Preferred name",
                    "title": "Ignored title",
                    "first_user_message": "Ignored message",
                    "preview": "New preview",
                    "model": "model-b",
                    "source": "app",
                    "tokens_used": 99,
                    "created_at": 30,
                    "updated_at": 40,
                    "rollout_path": None,
                    "git_branch": None,
                    "archived": 0,
                },
                {"id": "archived-id", "title": "Hidden", "updated_at": 100, "archived": 1},
            ]
        )

        loaded = sessions.load_sessions(str(self.home))

        self.assertEqual([item.id for item in loaded], ["newer-id", "older-id"])
        newer, older = loaded
        self.assertEqual(newer.title, "Preferred name")
        self.assertEqual(newer.name, "Preferred name")
        self.assertEqual(newer.tokens, 99)
        self.assertEqual(newer.status, sessions.STATUS_READY)
        self.assertEqual(older.cwd, "/older")
        self.assertEqual(older.title, "Older title")
        self.assertEqual(older.rollout_path, str(missing_rollout))
        self.assertEqual(older.git_branch, "dev")
        # A missing or null rollout is a normal ready session, not an exception.
        self.assertEqual(older.status, sessions.STATUS_READY)

    def test_title_falls_back_through_available_fields(self) -> None:
        self.create_database(
            [
                {"id": "from-message", "first_user_message": "First message", "preview": "Preview", "archived": 0},
                {"id": "from-preview", "preview": "Only preview", "archived": 0},
                {"id": "empty-title", "archived": 0},
            ]
        )

        loaded = {item.id: item for item in sessions.load_sessions(str(self.home))}

        self.assertEqual(loaded["from-message"].title, "First message")
        self.assertEqual(loaded["from-preview"].title, "Only preview")
        self.assertEqual(loaded["empty-title"].title, "")

    def test_missing_optional_columns_receive_defaults(self) -> None:
        self.create_database(
            [{"id": "minimal", "archived": 0}],
            columns="id TEXT, archived INTEGER NOT NULL DEFAULT 0",
        )

        [loaded] = sessions.load_sessions(str(self.home))

        self.assertEqual(loaded.id, "minimal")
        self.assertEqual(loaded.cwd, "")
        self.assertEqual(loaded.title, "")
        self.assertEqual(loaded.model, "")
        self.assertEqual(loaded.source, "")
        self.assertEqual(loaded.tokens, 0)
        self.assertEqual(loaded.created_at, 0)
        self.assertEqual(loaded.updated_at, 0)
        self.assertIsNone(loaded.rollout_path)

    def test_filters_summarizer_sessions_when_marker_is_in_any_title_field(self) -> None:
        marker = sessions.SUM.SUMMARIZER_MARKER
        rows = []
        for index, field in enumerate(("name", "title", "first_user_message", "preview")):
            row = {"id": f"summarizer-{index}", "title": "Ordinary title", "archived": 0}
            row[field] = marker + " transient prompt"
            rows.append(row)
        rows.append({"id": "ordinary", "title": "Keep me", "archived": 0})
        self.create_database(rows)

        self.assertEqual([item.id for item in sessions.load_sessions(str(self.home))], ["ordinary"])

    def test_populates_status_and_completed_summary(self) -> None:
        self.create_database([{"id": "session-id", "title": "Title", "rollout_path": "/rollout", "archived": 0}])
        with (
            mock.patch.object(sessions.SUM, "analyze_rollout", return_value={"status": sessions.STATUS_RUNNING}) as analyze,
            mock.patch.object(sessions.SUM, "summary_state", return_value="done") as state,
            mock.patch.object(sessions.SUM, "read_summary", return_value="Saved summary") as read,
        ):
            [loaded] = sessions.load_sessions(str(self.home))

        self.assertEqual(loaded.status, sessions.STATUS_RUNNING)
        self.assertEqual(loaded.summary_state, "done")
        self.assertEqual(loaded.summary, "Saved summary")
        analyze.assert_called_once_with("/rollout")
        state.assert_called_once_with("session-id", str(self.home))
        read.assert_called_once_with("session-id", str(self.home))

    def test_does_not_read_summary_until_state_is_done(self) -> None:
        self.create_database([{"id": "session-id", "title": "Title", "archived": 0}])
        with (
            mock.patch.object(sessions.SUM, "summary_state", return_value="in_progress"),
            mock.patch.object(sessions.SUM, "read_summary") as read,
        ):
            [loaded] = sessions.load_sessions(str(self.home))

        self.assertEqual(loaded.summary_state, "in_progress")
        self.assertIsNone(loaded.summary)
        read.assert_not_called()


class FindByIdTests(DatabaseTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.create_database(
            [
                {"id": "ABC12345-full-id", "title": "First", "updated_at": 20, "archived": 0},
                {"id": "def67890-full-id", "title": "Second", "updated_at": 10, "archived": 0},
            ]
        )

    def test_finds_exact_id_case_insensitively_and_strips_whitespace(self) -> None:
        found = sessions.find_by_id("  abc12345-FULL-ID  ", str(self.home))
        self.assertIsNotNone(found)
        self.assertEqual(found.id, "ABC12345-full-id")

    def test_finds_short_id_case_insensitively(self) -> None:
        found = sessions.find_by_id("DEF67890", str(self.home))
        self.assertIsNotNone(found)
        self.assertEqual(found.id, "def67890-full-id")

    def test_returns_none_for_unknown_id(self) -> None:
        self.assertIsNone(sessions.find_by_id("missing", str(self.home)))

    def test_returns_none_for_empty_database(self) -> None:
        other_home = self.home / "empty"
        other_home.mkdir()
        self.assertIsNone(sessions.find_by_id("anything", str(other_home)))


if __name__ == "__main__":
    unittest.main()
