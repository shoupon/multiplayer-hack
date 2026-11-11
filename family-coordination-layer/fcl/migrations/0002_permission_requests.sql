-- An approval request now has a kind.
--   task_response: an agent's parked answer to a task (the 0001 behaviour); task_id required.
--   permission:    an agent asks its own owner for a scoped permission to act outside FCL
--                  (read a calendar window, charge a card); task_id optional.
-- SQLite cannot relax NOT NULL in place, so the table is rebuilt. Nothing references it.

CREATE TABLE approval_requests_v2 (
  id                     TEXT PRIMARY KEY,
  family_id              TEXT NOT NULL,
  kind                   TEXT NOT NULL DEFAULT 'task_response' CHECK (kind IN ('task_response', 'permission')),
  task_id                TEXT,
  conversation_id        TEXT,
  approver_user_id       TEXT NOT NULL,
  requesting_agent_id    TEXT NOT NULL REFERENCES agents(id),
  action                 TEXT NOT NULL CHECK (json_valid(action)),
  action_hash            TEXT NOT NULL,
  status                 TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'granted', 'denied', 'expired', 'superseded')),
  expires_at             TEXT NOT NULL,
  decided_at             TEXT,
  decided_by_user_id     TEXT,
  decided_credential_id  TEXT REFERENCES credentials(id),
  idempotency_key        TEXT,
  request_hash           TEXT,
  created_at             TEXT NOT NULL,
  CHECK (kind = 'permission' OR task_id IS NOT NULL),
  CHECK (kind = 'task_response' OR conversation_id IS NOT NULL),
  FOREIGN KEY (task_id, family_id) REFERENCES coordination_tasks (id, family_id),
  FOREIGN KEY (conversation_id, family_id) REFERENCES conversations (id, family_id),
  FOREIGN KEY (family_id, approver_user_id) REFERENCES family_memberships (family_id, user_id)
);

INSERT INTO approval_requests_v2 (id, family_id, kind, task_id, approver_user_id, requesting_agent_id, action, action_hash,
                                  status, expires_at, decided_at, decided_by_user_id, decided_credential_id, created_at)
  SELECT id, family_id, 'task_response', task_id, approver_user_id, requesting_agent_id, action, action_hash,
         status, expires_at, decided_at, decided_by_user_id, decided_credential_id, created_at
    FROM approval_requests;

DROP TABLE approval_requests;
ALTER TABLE approval_requests_v2 RENAME TO approval_requests;

CREATE UNIQUE INDEX approval_requests_one_pending_idx
  ON approval_requests (task_id, requesting_agent_id) WHERE status = 'pending' AND kind = 'task_response';
CREATE INDEX approval_requests_approver_idx ON approval_requests (approver_user_id, status);
CREATE UNIQUE INDEX approval_requests_idempotency_idx
  ON approval_requests (requesting_agent_id, idempotency_key) WHERE idempotency_key IS NOT NULL;
