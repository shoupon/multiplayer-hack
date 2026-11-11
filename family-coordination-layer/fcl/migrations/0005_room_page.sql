-- Room page: the brief people agreed on, a coordinator agent, and the outcome it reports.
-- status: waiting (agents joining), live (coordinator working), done (summary written).

ALTER TABLE rooms ADD COLUMN brief TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(brief));
ALTER TABLE rooms ADD COLUMN status TEXT NOT NULL DEFAULT 'waiting' CHECK (status IN ('waiting', 'live', 'done'));
ALTER TABLE rooms ADD COLUMN summary TEXT CHECK (summary IS NULL OR json_valid(summary));
ALTER TABLE rooms ADD COLUMN coordinator_agent_id TEXT REFERENCES agents(id);
