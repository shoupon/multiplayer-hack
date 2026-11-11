-- Monitor links: an owner hands their agent one URL. GET shows a briefing; one POST claims a token.
-- Only the code's SHA-256 is stored, like credential tokens.

CREATE TABLE agent_links (
  id                     TEXT PRIMARY KEY,
  agent_id               TEXT NOT NULL,
  owner_user_id          TEXT NOT NULL,
  code_hash              TEXT NOT NULL UNIQUE,
  created_at             TEXT NOT NULL,
  expires_at             TEXT NOT NULL,
  claimed_at             TEXT,
  claimed_credential_id  TEXT REFERENCES credentials(id),
  revoked_at             TEXT,
  FOREIGN KEY (agent_id, owner_user_id) REFERENCES agents (id, owner_user_id),
  CHECK ((claimed_at IS NULL) = (claimed_credential_id IS NULL))
);
CREATE INDEX agent_links_agent_idx ON agent_links (agent_id);
