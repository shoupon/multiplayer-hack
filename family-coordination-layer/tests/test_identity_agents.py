from conftest import Env, Family, join, register, signup


def test_signup_and_me(env: Env):
    m = signup(env.api, "Husband")
    me = env.api.ok("GET", "/v1/me", m.user_token)
    assert me["kind"] == "user" and me["id"] == m.user_id


def test_rejects_missing_and_garbage_tokens(env: Env):
    assert env.api.get("/v1/me")[0] == 401
    assert env.api.get("/v1/me", "fcl_u_not-a-real-token")[0] == 401
    assert env.api.get("/v1/me", "something-else")[0] == 401


def test_family_membership_and_invitations(env: Env, family: Family):
    fam = env.api.ok("GET", f"/v1/families/{family.id}", family.wife.user_token)
    assert {m["user_id"] for m in fam["members"]} == {family.husband.user_id, family.wife.user_id}

    outsider = signup(env.api, "Outsider")
    assert env.api.get(f"/v1/families/{family.id}", outsider.user_token)[0] == 404


def test_invitation_is_single_use(env: Env, family: Family):
    inv = env.api.ok("POST", f"/v1/families/{family.id}/invitations", family.husband.user_token, json={})
    a, b = signup(env.api, "A"), signup(env.api, "B")
    assert env.api.post("/v1/invitations/accept", a.user_token, {"code": inv["code"]})[0] == 200
    assert env.api.post("/v1/invitations/accept", b.user_token, {"code": inv["code"]})[0] == 404


def test_only_admins_invite_and_denials_are_audited(env: Env, family: Family):
    status, body = env.api.post(f"/v1/families/{family.id}/invitations", family.wife.user_token, {})
    assert (status, body["error"]["code"]) == (403, "admin_required")
    events = env.api.ok("GET", "/v1/audit", family.wife.user_token)["items"]
    assert any(e["action"] == "invitation.create" and e["outcome"] == "denied" for e in events)


def test_agent_token_cannot_use_human_endpoints(env: Env, family: Family):
    status, body = env.api.post("/v1/families", family.husband.agent_token, {"name": "x"})
    assert (status, body["error"]["code"]) == (403, "user_credential_required")
    assert env.api.ok("GET", "/v1/me", family.husband.agent_token)["agent_id"] == family.husband.agent_id


def test_discovery_needs_recipient_consent(env: Env, family: Family):
    grandma = signup(env.api, "Grandma")
    join(env.api, family.husband, family.id, grandma)
    register(env.api, grandma, family.id, "line-bot", [])  # no receive grant: not discoverable

    seen = env.api.ok("GET", "/v1/agents", family.husband.agent_token, params={"family_id": family.id})["items"]
    ids = {a["id"] for a in seen}
    assert family.wife.agent_id in ids and grandma.agent_id not in ids
    wife_agent = next(a for a in seen if a["id"] == family.wife.agent_id)
    assert wife_agent["provider_verified"] is False
    assert set(wife_agent) == {"id", "owner", "display_name", "provider", "provider_verified", "capabilities", "transports", "availability"}

    # Grandma always sees her own agent.
    own = env.api.ok("GET", "/v1/agents", grandma.user_token, params={"family_id": family.id})["items"]
    assert grandma.agent_id in {a["id"] for a in own}


def test_agent_discovery_needs_a_discover_grant(env: Env, family: Family):
    kid = signup(env.api, "Kid")
    join(env.api, family.husband, family.id, kid)
    register(env.api, kid, family.id, "custom", [{"action": "agent:message:receive"}])
    status, body = env.api.get("/v1/agents", kid.agent_token, family_id=family.id)
    assert (status, body["error"]["code"]) == (403, "no_grant")


def test_capabilities_respects_visibility(env: Env, family: Family):
    caps = env.api.ok("GET", f"/v1/agents/{family.wife.agent_id}/capabilities", family.husband.user_token,
                      params={"family_id": family.id})
    assert caps["capabilities"] == ["propose_schedule", "receive_message"]
    outsider = signup(env.api, "Outsider")
    assert env.api.get(f"/v1/agents/{family.wife.agent_id}/capabilities", outsider.user_token, family_id=family.id)[0] == 404


def test_grant_rules(env: Env, family: Family):
    # Delegating to someone else's agent looks like a missing agent.
    status, _ = env.api.post("/v1/grants", family.husband.user_token,
                             {"agent_id": family.wife.agent_id, "family_id": family.id, "action": "task:propose"})
    assert status == 404
    status, body = env.api.post("/v1/grants", family.husband.user_token,
                                {"agent_id": family.husband.agent_id, "family_id": family.id,
                                 "action": "task:propose", "requires_approval": True})
    assert (status, body["error"]["code"]) == (422, "approval_not_supported")
    status, _ = env.api.post("/v1/grants", family.husband.user_token,
                             {"agent_id": family.husband.agent_id, "family_id": family.id, "action": "calendar:event:create"})
    assert status == 400


def test_revoking_a_grant_takes_effect_on_the_next_request(env: Env, family: Family):
    grants = env.api.ok("GET", "/v1/grants", family.husband.user_token, params={"agent_id": family.husband.agent_id})["items"]
    discover = next(g for g in grants if g["action"] == "family:members:discover")
    assert env.api.delete(f"/v1/grants/{discover['id']}", family.husband.user_token)[0] == 204
    assert env.api.get("/v1/agents", family.husband.agent_token, family_id=family.id)[0] == 403


def test_credential_rotation_and_revocation(env: Env, family: Family):
    new = env.api.ok("POST", f"/v1/agents/{family.husband.agent_id}/credentials", family.husband.user_token, json={"label": "rotated"})
    assert env.api.get("/v1/me", new["token"])[0] == 200
    assert env.api.delete(f"/v1/agents/{family.husband.agent_id}/credentials/{new['id']}", family.husband.user_token)[0] == 204
    assert env.api.get("/v1/me", new["token"])[0] == 401
    assert env.api.get("/v1/me", family.husband.agent_token)[0] == 200


def test_disabled_agent_is_rejected(env: Env, family: Family):
    assert env.api.delete(f"/v1/agents/{family.wife.agent_id}", family.wife.user_token)[0] == 204
    status, body = env.api.get("/v1/me", family.wife.agent_token)
    assert (status, body["error"]["code"]) == (403, "agent_disabled")


def test_removed_member_loses_access(env: Env, family: Family):
    assert env.api.delete(f"/v1/families/{family.id}/members/{family.wife.user_id}", family.husband.user_token)[0] == 204
    assert env.api.get(f"/v1/families/{family.id}", family.wife.user_token)[0] == 404
    assert env.api.get("/v1/agents", family.wife.agent_token, family_id=family.id)[0] == 404
    seen = env.api.ok("GET", "/v1/agents", family.husband.agent_token, params={"family_id": family.id})["items"]
    assert family.wife.agent_id not in {a["id"] for a in seen}


def test_last_admin_cannot_leave(env: Env, family: Family):
    status, body = env.api.delete(f"/v1/families/{family.id}/members/{family.husband.user_id}", family.husband.user_token)
    assert (status, body["error"]["code"]) == (409, "last_admin")


def test_oversized_body_is_rejected(env: Env):
    status, body = env.api.post("/v1/users", json={"display_name": "x" * 70_000})
    assert (status, body["error"]["code"]) == (413, "body_too_large")
