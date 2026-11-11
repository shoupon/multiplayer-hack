-- Rooms: a reusable join code for one coordination goal, such as arranging a dinner.
-- Anyone holding the code can join the room's family with a new user and agent, up to max_members.

CREATE TABLE rooms (
  id           TEXT PRIMARY KEY,
  family_id    TEXT NOT NULL REFERENCES families(id),
  created_by   TEXT NOT NULL REFERENCES users(id),
  intent       TEXT NOT NULL,
  code_hash    TEXT NOT NULL UNIQUE,
  max_members  INTEGER NOT NULL CHECK (max_members BETWEEN 2 AND 20),
  created_at   TEXT NOT NULL,
  expires_at   TEXT NOT NULL,
  revoked_at   TEXT
);
CREATE INDEX rooms_family_idx ON rooms (family_id);
