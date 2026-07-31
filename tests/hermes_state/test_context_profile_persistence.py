"""Test that context_profile persists in the session DB for resume."""

import time

import pytest

from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path):
    return SessionDB(db_path=tmp_path / "state.db")


class TestContextProfilePersistence:
    def test_context_profile_column_exists(self, db):
        """The sessions table has a context_profile column."""
        with db._read_ctx() as conn:
            rows = conn.execute("PRAGMA table_info(sessions)").fetchall()
        col_names = [r[1] if isinstance(r, (tuple, list)) else r["name"] for r in rows]
        assert "context_profile" in col_names

    def test_write_and_read_context_profile(self, db):
        """context_profile written to a session row survives get_session."""
        sid = "test_session_build"
        db.create_session(sid, source="desktop")
        # Write context_profile via direct UPDATE (mirrors gateway path).
        db._execute_write(
            lambda conn: conn.execute(
                "UPDATE sessions SET context_profile = ? WHERE id = ?",
                ("build", sid),
            )
        )
        found = db.get_session(sid)
        assert found is not None
        assert found["context_profile"] == "build"

    def test_null_context_profile_for_agent(self, db):
        """Agent mode does not persist a context_profile (auto-detect on resume)."""
        sid = "test_session_agent"
        db.create_session(sid, source="desktop")
        found = db.get_session(sid)
        assert found is not None
        assert found.get("context_profile") is None

    def test_context_profile_survives_reopen(self, db, tmp_path):
        """context_profile persists across DB close and reopen (gateway restart)."""
        sid = "test_session_sanad"
        db.create_session(sid, source="desktop")
        db._execute_write(
            lambda conn: conn.execute(
                "UPDATE sessions SET context_profile = ? WHERE id = ?",
                ("sanad", sid),
            )
        )
        db.close()

        # Reopen from same path (simulates gateway restart).
        db2 = SessionDB(db_path=tmp_path / "state.db")
        found = db2.get_session(sid)
        assert found is not None
        assert found["context_profile"] == "sanad"
        db2.close()
