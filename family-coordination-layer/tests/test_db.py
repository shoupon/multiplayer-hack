import sqlite3

import pytest

from fcl.db import Database, migrate, open_database

SEED = """
INSERT INTO users (id, display_name, created_at) VALUES ('usr_a', 'A', 't'), ('usr_b', 'B', 't');
INSERT INTO families (id, name, created_by, created_at) VALUES ('fam_1', 'One', 'usr_a', 't'), ('fam_2', 'Two', 'usr_b', 't');
INSERT INTO family_memberships (family_id, user_id, role, joined_at)
  VALUES ('fam_1', 'usr_a', 'admin', 't'), ('fam_2', 'usr_b', 'admin', 't'), ('fam_1', 'usr_b', 'member', 't');
INSERT INTO conversations (id, family_id, created_at) VALUES ('cnv_1', 'fam_1', 't');
INSERT INTO agents (id, owner_user_id, display_name, provider, created_at) VALUES ('agt_a', 'usr_a', 'A agent', 'custom', 't');
"""


@pytest.fixture
def db(tmp_path):
    database = open_database(tmp_path / "fcl.db")
    with database.tx() as c:
        c.script(SEED)
    yield database
    database.close()


def test_migrations_apply_once(tmp_path):
    database = Database(tmp_path / "fcl.db")
    assert migrate(database) == ["0001_init.sql", "0002_permission_requests.sql", "0003_agent_links.sql", "0004_rooms.sql",
                                 "0005_room_page.sql"]
    assert migrate(database) == []


def test_rejects_message_pointing_at_another_familys_conversation(db):
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"), db.tx() as c:
        c.run(
            """INSERT INTO messages (id, family_id, conversation_id, schema_version, type, payload_schema, payload,
                                     sender_kind, correlation_id, created_at)
               VALUES ('msg_x', 'fam_2', 'cnv_1', '1.0', 'message.text', 'fcl.message.text/1', '{}', 'system', 'c', 't')"""
        )


def test_rejects_grant_from_someone_who_does_not_own_the_agent(db):
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"), db.tx() as c:
        c.run(
            """INSERT INTO delegation_grants (id, agent_id, grantor_user_id, family_id, action, created_at)
               VALUES ('grt_x', 'agt_a', 'usr_b', 'fam_1', 'task:propose', 't')"""
        )


def test_failed_transaction_leaves_nothing_behind(db):
    with pytest.raises(sqlite3.IntegrityError), db.tx() as c:
        c.run("INSERT INTO users (id, display_name, created_at) VALUES ('usr_c', 'C', 't')")
        c.run("INSERT INTO users (id, display_name, created_at) VALUES ('usr_c', 'dup', 't')")
    with db.read() as c:
        assert c.one("SELECT 1 FROM users WHERE id = 'usr_c'") is None
