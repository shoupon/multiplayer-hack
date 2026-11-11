-- FCL initial schema (SQLite). See docs/DESIGN.md section 6.
-- Timestamps: UTC ISO-8601 text, fixed format, so text order is time order.
-- JSON: text with json_valid checks. Booleans: 0 or 1.

CREATE TABLE users (
  id            TEXT PRIMARY KEY,
  display_name  TEXT NOT NULL CHECK (length(display_name) BETWEEN 1 AND 100),
  created_at    TEXT NOT NULL,
  deleted_at    TEXT
);

CREATE TABLE families (
  id          TEXT PRIMARY KEY,
  name        TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 100),
  created_by  TEXT NOT NULL REFERENCES users(id),
  created_at  TEXT NOT NULL
);

CREATE TABLE family_memberships (
  family_id   TEXT NOT NULL REFERENCES families(id),
  user_id     TEXT NOT NULL REFERENCES users(id),
  role        TEXT NOT NULL CHECK (role IN ('admin', 'member')),
  status      TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'removed')),
  joined_at   TEXT NOT NULL,
  removed_at  TEXT,
  PRIMARY KEY (family_id, user_id)
);
CREATE INDEX family_memberships_user_idx ON family_memberships (user_id);

CREATE TABLE family_invitations (
  id           TEXT PRIMARY KEY,
  family_id    TEXT NOT NULL REFERENCES families(id),
  created_by   TEXT NOT NULL,
  code_hash    TEXT NOT NULL UNIQUE,
  role         TEXT NOT NULL DEFAULT 'member' CHECK (role IN ('admin', 'member')),
  expires_at   TEXT NOT NULL,
  accepted_by  TEXT REFERENCES users(id),
  accepted_at  TEXT,
  revoked_at   TEXT,
  created_at   TEXT NOT NULL,
  FOREIGN KEY (family_id, created_by) REFERENCES family_memberships (family_id, user_id)
);

CREATE TABLE agents (
  id             TEXT PRIMARY KEY,
  owner_user_id  TEXT NOT NULL REFERENCES users(id),
  display_name   TEXT NOT NULL CHECK (length(display_name) BETWEEN 1 AND 100),
  provider       TEXT NOT NULL CHECK (length(provider) BETWEEN 1 AND 50),
  status         TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
  created_at     TEXT NOT NULL,
  last_seen_at   TEXT,
  UNIQUE (id, owner_user_id)
);
CREATE INDEX agents_owner_idx ON agents (owner_user_id);

CREATE TABLE agent_capabilities (
  agent_id    TEXT NOT NULL REFERENCES agents(id),
  capability  TEXT NOT NULL,
  PRIMARY KEY (agent_id, capability)
);

CREATE TABLE agent_transports (
  agent_id         TEXT NOT NULL REFERENCES agents(id),
  kind             TEXT NOT NULL CHECK (kind IN ('inbox', 'webhook', 'mcp', 'a2a', 'messaging_bridge')),
  status           TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
  last_success_at  TEXT,
  last_failure_at  TEXT,
  created_at       TEXT NOT NULL,
  PRIMARY KEY (agent_id, kind)
);

CREATE TABLE credentials (
  id              TEXT PRIMARY KEY,
  principal_kind  TEXT NOT NULL CHECK (principal_kind IN ('user', 'agent')),
  user_id         TEXT REFERENCES users(id),
  agent_id        TEXT REFERENCES agents(id),
  token_hash      TEXT NOT NULL UNIQUE,
  label           TEXT,
  created_at      TEXT NOT NULL,
  expires_at      TEXT,
  revoked_at      TEXT,
  last_used_at    TEXT,
  CHECK (
    (principal_kind = 'user' AND user_id IS NOT NULL AND agent_id IS NULL) OR
    (principal_kind = 'agent' AND agent_id IS NOT NULL AND user_id IS NULL)
  )
);

-- Only an agent's owner can delegate to it: (agent_id, grantor_user_id) must match agents.
-- Grantor and target must be members of the grant's family.
CREATE TABLE delegation_grants (
  id                 TEXT PRIMARY KEY,
  agent_id           TEXT NOT NULL,
  grantor_user_id    TEXT NOT NULL,
  family_id          TEXT NOT NULL REFERENCES families(id),
  action             TEXT NOT NULL,
  target_user_id     TEXT,
  requires_approval  INTEGER NOT NULL DEFAULT 0 CHECK (requires_approval IN (0, 1)),
  expires_at         TEXT,
  revoked_at         TEXT,
  created_at         TEXT NOT NULL,
  FOREIGN KEY (agent_id, grantor_user_id) REFERENCES agents (id, owner_user_id),
  FOREIGN KEY (family_id, grantor_user_id) REFERENCES family_memberships (family_id, user_id),
  FOREIGN KEY (family_id, target_user_id) REFERENCES family_memberships (family_id, user_id)
);
CREATE INDEX delegation_grants_lookup_idx ON delegation_grants (agent_id, family_id, action);

CREATE TABLE conversations (
  id                   TEXT PRIMARY KEY,
  family_id            TEXT NOT NULL REFERENCES families(id),
  subject              TEXT,
  created_by_agent_id  TEXT REFERENCES agents(id),
  created_at           TEXT NOT NULL,
  UNIQUE (id, family_id)
);

CREATE TABLE coordination_tasks (
  id                       TEXT PRIMARY KEY,
  family_id                TEXT NOT NULL REFERENCES families(id),
  conversation_id          TEXT NOT NULL,
  intent                   TEXT NOT NULL,
  state                    TEXT NOT NULL CHECK (state IN (
                             'CREATED', 'AWAITING_DELIVERY', 'AWAITING_RESPONSE', 'COUNTERPROPOSED',
                             'AWAITING_APPROVAL', 'AGREED', 'COMPLETED', 'DECLINED', 'EXPIRED',
                             'CANCELLED', 'FAILED')),
  proposal                 TEXT NOT NULL CHECK (json_valid(proposal)),
  proposal_version         INTEGER NOT NULL CHECK (proposal_version >= 1),
  proposal_author_user_id  TEXT NOT NULL,
  negotiation_count        INTEGER NOT NULL DEFAULT 0,
  max_negotiations         INTEGER NOT NULL CHECK (max_negotiations >= 0),
  initiator_user_id        TEXT NOT NULL,
  initiator_agent_id       TEXT NOT NULL REFERENCES agents(id),
  idempotency_key          TEXT NOT NULL,
  request_hash             TEXT NOT NULL,
  expires_at               TEXT NOT NULL,
  outcome                  TEXT CHECK (outcome IS NULL OR json_valid(outcome)),
  row_version              INTEGER NOT NULL DEFAULT 1,
  created_at               TEXT NOT NULL,
  updated_at               TEXT NOT NULL,
  UNIQUE (id, family_id),
  UNIQUE (initiator_agent_id, idempotency_key),
  FOREIGN KEY (conversation_id, family_id) REFERENCES conversations (id, family_id),
  FOREIGN KEY (family_id, initiator_user_id) REFERENCES family_memberships (family_id, user_id)
);
CREATE INDEX coordination_tasks_family_idx ON coordination_tasks (family_id, state);

CREATE TABLE task_participants (
  task_id            TEXT NOT NULL,
  family_id          TEXT NOT NULL,
  user_id            TEXT NOT NULL,
  agent_id           TEXT NOT NULL REFERENCES agents(id),
  role               TEXT NOT NULL CHECK (role IN ('initiator', 'invitee')),
  response           TEXT NOT NULL DEFAULT 'pending' CHECK (response IN ('pending', 'accepted', 'declined')),
  responded_version  INTEGER,
  responded_at       TEXT,
  delivered          INTEGER NOT NULL DEFAULT 0 CHECK (delivered IN (0, 1)),
  approval_pending   INTEGER NOT NULL DEFAULT 0 CHECK (approval_pending IN (0, 1)),
  PRIMARY KEY (task_id, user_id),
  UNIQUE (task_id, agent_id),
  FOREIGN KEY (task_id, family_id) REFERENCES coordination_tasks (id, family_id),
  FOREIGN KEY (family_id, user_id) REFERENCES family_memberships (family_id, user_id)
);
CREATE INDEX task_participants_user_idx ON task_participants (user_id);
CREATE INDEX task_participants_agent_idx ON task_participants (agent_id);

CREATE TABLE messages (
  id                    TEXT PRIMARY KEY,
  family_id             TEXT NOT NULL REFERENCES families(id),
  conversation_id       TEXT NOT NULL,
  task_id               TEXT,
  schema_version        TEXT NOT NULL,
  type                  TEXT NOT NULL,
  payload_schema        TEXT NOT NULL,
  payload               TEXT NOT NULL CHECK (json_valid(payload)),
  sender_kind           TEXT NOT NULL CHECK (sender_kind IN ('agent', 'system')),
  sender_user_id        TEXT,
  sender_agent_id       TEXT REFERENCES agents(id),
  sender_credential_id  TEXT REFERENCES credentials(id),
  grant_id              TEXT REFERENCES delegation_grants(id),
  correlation_id        TEXT NOT NULL,
  causation_id          TEXT,
  idempotency_key       TEXT,
  request_hash          TEXT,
  requires_response     INTEGER NOT NULL DEFAULT 0 CHECK (requires_response IN (0, 1)),
  expires_at            TEXT,
  created_at            TEXT NOT NULL,
  UNIQUE (id, family_id),
  UNIQUE (sender_agent_id, idempotency_key),
  CHECK (
    (sender_kind = 'agent' AND sender_agent_id IS NOT NULL AND sender_user_id IS NOT NULL) OR
    (sender_kind = 'system' AND sender_agent_id IS NULL)
  ),
  FOREIGN KEY (conversation_id, family_id) REFERENCES conversations (id, family_id),
  FOREIGN KEY (task_id, family_id) REFERENCES coordination_tasks (id, family_id)
);
CREATE INDEX messages_task_idx ON messages (task_id);

CREATE TABLE message_deliveries (
  seq                 INTEGER PRIMARY KEY AUTOINCREMENT,
  message_id          TEXT NOT NULL,
  family_id           TEXT NOT NULL,
  recipient_user_id   TEXT NOT NULL,
  recipient_agent_id  TEXT NOT NULL REFERENCES agents(id),
  status              TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'delivered', 'acknowledged', 'failed')),
  delivered_at        TEXT,
  acknowledged_at     TEXT,
  last_error          TEXT,
  created_at          TEXT NOT NULL,
  UNIQUE (message_id, recipient_agent_id),
  FOREIGN KEY (message_id, family_id) REFERENCES messages (id, family_id),
  FOREIGN KEY (family_id, recipient_user_id) REFERENCES family_memberships (family_id, user_id)
);
CREATE INDEX message_deliveries_inbox_idx ON message_deliveries (recipient_agent_id, seq);

CREATE TABLE task_transitions (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id          TEXT NOT NULL,
  family_id        TEXT NOT NULL,
  from_state       TEXT,
  to_state         TEXT NOT NULL,
  event            TEXT NOT NULL CHECK (json_valid(event)),
  actor_kind       TEXT NOT NULL CHECK (actor_kind IN ('agent', 'user', 'system')),
  actor_user_id    TEXT,
  actor_agent_id   TEXT,
  idempotency_key  TEXT,
  request_hash     TEXT,
  message_id       TEXT,
  created_at       TEXT NOT NULL,
  UNIQUE (task_id, actor_agent_id, idempotency_key),
  FOREIGN KEY (task_id, family_id) REFERENCES coordination_tasks (id, family_id)
);
CREATE INDEX task_transitions_task_idx ON task_transitions (task_id, id);

CREATE TABLE approval_requests (
  id                     TEXT PRIMARY KEY,
  family_id              TEXT NOT NULL,
  task_id                TEXT NOT NULL,
  approver_user_id       TEXT NOT NULL,
  requesting_agent_id    TEXT NOT NULL REFERENCES agents(id),
  action                 TEXT NOT NULL CHECK (json_valid(action)),
  action_hash            TEXT NOT NULL,
  status                 TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'granted', 'denied', 'expired', 'superseded')),
  expires_at             TEXT NOT NULL,
  decided_at             TEXT,
  decided_by_user_id     TEXT,
  decided_credential_id  TEXT REFERENCES credentials(id),
  created_at             TEXT NOT NULL,
  FOREIGN KEY (task_id, family_id) REFERENCES coordination_tasks (id, family_id),
  FOREIGN KEY (family_id, approver_user_id) REFERENCES family_memberships (family_id, user_id)
);
CREATE UNIQUE INDEX approval_requests_one_pending_idx
  ON approval_requests (task_id, requesting_agent_id) WHERE status = 'pending';
CREATE INDEX approval_requests_approver_idx ON approval_requests (approver_user_id, status);

CREATE TABLE audit_events (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  family_id       TEXT,
  actor_kind      TEXT NOT NULL CHECK (actor_kind IN ('user', 'agent', 'system', 'anonymous')),
  actor_user_id   TEXT,
  actor_agent_id  TEXT,
  credential_id   TEXT,
  action          TEXT NOT NULL,
  resource_type   TEXT,
  resource_id     TEXT,
  outcome         TEXT NOT NULL CHECK (outcome IN ('allowed', 'denied', 'succeeded', 'failed')),
  reason          TEXT,
  metadata        TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(metadata)),
  created_at      TEXT NOT NULL
);
CREATE INDEX audit_events_actor_user_idx ON audit_events (actor_user_id, id);

CREATE TABLE webhook_subscriptions (
  id                 TEXT PRIMARY KEY,
  agent_id           TEXT NOT NULL REFERENCES agents(id),
  url                TEXT NOT NULL,
  secret_ciphertext  TEXT NOT NULL,
  status             TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
  created_at         TEXT NOT NULL,
  disabled_at        TEXT
);
CREATE UNIQUE INDEX webhook_subscriptions_one_active_idx
  ON webhook_subscriptions (agent_id) WHERE status = 'active';

CREATE TABLE scheduled_jobs (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  kind          TEXT NOT NULL,
  payload       TEXT NOT NULL CHECK (json_valid(payload)),
  run_at        TEXT NOT NULL,
  status        TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'running', 'done', 'dead')),
  attempts      INTEGER NOT NULL DEFAULT 0,
  max_attempts  INTEGER NOT NULL DEFAULT 8,
  locked_until  TEXT,
  last_error    TEXT,
  dedupe_key    TEXT UNIQUE,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);
CREATE INDEX scheduled_jobs_due_idx ON scheduled_jobs (status, run_at);
