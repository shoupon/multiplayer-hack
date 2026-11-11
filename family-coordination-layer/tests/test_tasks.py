from dataclasses import replace

from conftest import DINNER_AT_7, DINNER_AT_730, Env, Family, build_family, inbox, propose_dinner, respond, signup


def test_duplicate_create_returns_one_task(env: Env, family: Family):
    first = propose_dinner(env, family, idem="same")
    again = propose_dinner(env, family, idem="same")
    assert again["id"] == first["id"]
    assert len(env.api.ok("GET", "/v1/tasks", family.husband.agent_token)["items"]) == 1
    assert len(inbox(env, family.wife)) == 1


def test_same_key_different_request_is_rejected(env: Env, family: Family):
    propose_dinner(env, family, idem="k")
    status, body = env.api.post("/v1/tasks", family.husband.agent_token, {
        "family_id": family.id, "invitees": [family.wife.agent_id], "intent": "schedule_lunch",
        "proposal": DINNER_AT_7, "expires_at": "2026-11-11T12:00:00+08:00"}, idem="k")
    assert (status, body["error"]["code"]) == (422, "idempotency_key_reused")


def test_retried_response_is_applied_once(env: Env, family: Family):
    task = propose_dinner(env, family)
    first = respond(env, family.wife, task["id"], "counterpropose", 1, "retry", DINNER_AT_730)
    again = respond(env, family.wife, task["id"], "counterpropose", 1, "retry", DINNER_AT_730)
    assert first[0] == again[0] == 200
    assert again[1]["task"]["proposal_version"] == 2
    assert [i["message"]["type"] for i in inbox(env, family.husband)] == ["coordination.counterproposal"]


def test_stale_response_is_rejected(env: Env, family: Family):
    task = propose_dinner(env, family)
    respond(env, family.wife, task["id"], "counterpropose", 1, "c1", DINNER_AT_730)
    status, body = respond(env, family.husband, task["id"], "accept", 1, "late")
    assert (status, body["error"]["code"]) == (409, "stale_version")


def test_expired_proposal_cannot_be_accepted(env: Env, family: Family):
    task = propose_dinner(env, family, hours=1)
    env.clock.advance(hours=2)
    status, body = respond(env, family.wife, task["id"], "accept", 1, "too-late")
    assert (status, body["error"]["code"]) == (409, "task_expired")
    assert env.api.ok("GET", f"/v1/tasks/{task['id']}", family.wife.user_token)["state"] == "EXPIRED"


def test_expiry_must_be_in_the_future_and_bounded(env: Env, family: Family):
    for when in ("2026-11-01T00:00:00Z", "2027-11-01T00:00:00Z"):
        status, body = env.api.post("/v1/tasks", family.husband.agent_token, {
            "family_id": family.id, "invitees": [family.wife.agent_id], "intent": "schedule_dinner",
            "proposal": DINNER_AT_7, "expires_at": when}, idem=f"exp-{when}")
        assert (status, body["error"]["code"]) == (422, "invalid_expiry")


def test_approval_cannot_be_forged(env: Env):
    fam = build_family(env.api, wife_needs_approval=True)
    task = propose_dinner(env, fam)
    _, parked = respond(env, fam.wife, task["id"], "accept", 1, "w-accept")
    approval_id = parked["approval_id"]
    path = f"/v1/approvals/{approval_id}/decisions"

    status, body = env.api.post(path, fam.husband.user_token, {"decision": "grant"})
    assert (status, body["error"]["code"]) == (403, "not_approver")
    status, body = env.api.post(path, fam.wife.agent_token, {"decision": "grant"})
    assert (status, body["error"]["code"]) == (403, "user_credential_required")
    status, body = env.api.post(path, fam.husband.agent_token, {"decision": "grant"})
    assert status == 403
    assert env.api.post(path, signup(env.api, "Outsider").user_token, {"decision": "grant"})[0] == 404
    # The agent cannot bypass the gate by answering directly either.
    status, body = respond(env, fam.wife, task["id"], "accept", 1, "w-again")
    assert (status, body["error"]["code"]) == (409, "approval_pending")
    assert env.api.ok("GET", f"/v1/tasks/{task['id']}", fam.husband.user_token)["state"] == "AWAITING_APPROVAL"


def test_denied_approval_drops_the_parked_response(env: Env):
    fam = build_family(env.api, wife_needs_approval=True)
    task = propose_dinner(env, fam)
    _, parked = respond(env, fam.wife, task["id"], "decline", 1, "w-decline")
    denied = env.api.ok("POST", f"/v1/approvals/{parked['approval_id']}/decisions", fam.wife.user_token, json={"decision": "deny"})
    assert denied["status"] == "denied"
    state = env.api.ok("GET", f"/v1/tasks/{task['id']}", fam.wife.user_token)
    assert state["state"] == "AWAITING_RESPONSE"
    assert [i["message"]["type"] for i in inbox(env, fam.husband)] == []
    assert respond(env, fam.wife, task["id"], "accept", 1, "w-accept")[1]["result"] == "pending_approval"


def test_approval_for_a_task_that_moved_on_is_superseded(env: Env):
    fam = build_family(env.api, wife_needs_approval=True)
    task = propose_dinner(env, fam)
    _, parked = respond(env, fam.wife, task["id"], "accept", 1, "w-accept")
    env.api.ok("POST", f"/v1/tasks/{task['id']}/cancel", fam.husband.agent_token, json={"reason": "plans changed"})
    status, body = env.api.post(f"/v1/approvals/{parked['approval_id']}/decisions", fam.wife.user_token, {"decision": "grant"})
    assert (status, body["error"]["code"]) == (409, "approval_not_pending")
    [a] = env.api.ok("GET", "/v1/approvals", fam.wife.user_token)["items"]
    assert a["status"] == "superseded"


def test_negotiation_is_capped(env: Env, family: Family):
    env.ctx.settings = replace(env.ctx.settings, max_negotiations=1)
    task = propose_dinner(env, family)
    respond(env, family.wife, task["id"], "counterpropose", 1, "c1", DINNER_AT_730)
    status, body = respond(env, family.husband, task["id"], "counterpropose", 2, "c2", DINNER_AT_7)
    assert (status, body["error"]["code"]) == (422, "negotiation_limit")


def test_decline_ends_the_task(env: Env, family: Family):
    task = propose_dinner(env, family)
    status, body = respond(env, family.wife, task["id"], "decline", 1, "no")
    assert body["task"]["state"] == "DECLINED"
    assert [i["message"]["type"] for i in inbox(env, family.husband)] == ["coordination.decline"]


def test_only_the_initiator_cancels(env: Env, family: Family):
    task = propose_dinner(env, family)
    status, body = env.api.post(f"/v1/tasks/{task['id']}/cancel", family.wife.agent_token, {})
    assert (status, body["error"]["code"]) == (403, "not_initiator")
    assert env.api.ok("POST", f"/v1/tasks/{task['id']}/cancel", family.husband.agent_token, json={})["state"] == "CANCELLED"
    assert inbox(env, family.wife)[-1]["message"]["type"] == "coordination.cancel"


def test_non_participants_and_revoked_grants_cannot_respond(env: Env, family: Family):
    task = propose_dinner(env, family)
    other = build_family(env.api)
    assert respond(env, other.wife, task["id"], "accept", 1, "x")[0] == 404
    assert env.api.get(f"/v1/tasks/{task['id']}", other.wife.user_token)[0] == 404

    grants = env.api.ok("GET", "/v1/grants", family.wife.user_token, params={"agent_id": family.wife.agent_id})["items"]
    respond_grant = next(g for g in grants if g["action"] == "task:respond")
    env.api.delete(f"/v1/grants/{respond_grant['id']}", family.wife.user_token)
    status, body = respond(env, family.wife, task["id"], "accept", 1, "revoked")
    assert (status, body["error"]["code"]) == (403, "no_grant")


def test_counterpropose_needs_a_proposal(env: Env, family: Family):
    task = propose_dinner(env, family)
    status, _ = respond(env, family.wife, task["id"], "counterpropose", 1, "empty")
    assert status == 400
