"""The brief's MVP scenario (H§13), end to end over HTTP."""

from conftest import DINNER_AT_7, DINNER_AT_730, Env, build_family, inbox, propose_dinner, respond


def test_dinner_coordination_with_counterproposal_and_human_approval(env: Env):
    fam = build_family(env.api, wife_needs_approval=True)
    h, w = fam.husband, fam.wife

    # 2. The husband's agent discovers the wife's agent.
    found = env.api.ok("GET", "/v1/agents", h.agent_token, params={"family_id": fam.id})["items"]
    wife_agent = next(a for a in found if a["owner"]["user_id"] == w.user_id)
    assert wife_agent["id"] == w.agent_id and wife_agent["provider"] != "custom-mcp"

    # 3-5. Proposal for Friday 7 PM, persisted and routed.
    task = propose_dinner(env, fam)
    assert task["state"] == "AWAITING_DELIVERY"

    # 6. The wife's companion agent receives it.
    [proposal] = inbox(env, w)
    assert proposal["message"]["type"] == "coordination.proposal"
    assert proposal["message"]["payload"]["proposal"]["proposed_time"] == DINNER_AT_7["proposed_time"]
    assert env.api.ok("GET", f"/v1/tasks/{task['id']}", w.agent_token)["state"] == "AWAITING_RESPONSE"

    # 7-8. Her agent wants 7:30; her grant requires her approval first.
    status, parked = respond(env, w, task["id"], "counterpropose", 1, "w-730", DINNER_AT_730)
    assert (status, parked["result"]) == (200, "pending_approval")
    assert parked["task"]["state"] == "AWAITING_APPROVAL"
    assert inbox(env, h) == []  # nothing reaches the husband before she approves

    [approval] = env.api.ok("GET", "/v1/approvals", w.user_token, params={"status": "pending"})["items"]
    assert approval["action"]["kind"] == "counterpropose"
    assert approval["action"]["proposal"]["proposed_time"] == DINNER_AT_730["proposed_time"]
    decided = env.api.ok("POST", f"/v1/approvals/{approval['id']}/decisions", w.user_token, json={"decision": "grant"})
    assert decided["status"] == "granted"

    # 9. The counterproposal reaches the husband's inbox.
    [counter] = inbox(env, h)
    assert counter["message"]["type"] == "coordination.counterproposal"
    assert counter["message"]["sender"]["agent_id"] == w.agent_id
    assert counter["message"]["payload"]["proposal_version"] == 2

    # 10-12. He accepts; both acceptances are recorded; the task is agreed.
    status, accepted = respond(env, h, task["id"], "accept", 2, "h-accept")
    assert status == 200 and accepted["task"]["state"] == "AGREED"
    assert {p["user_id"]: (p["response"], p["responded_version"]) for p in accepted["task"]["participants"]} == {
        h.user_id: ("accepted", 2), w.user_id: ("accepted", 2)}

    # 13-14. Both humans read the same outcome and the full history.
    his = env.api.ok("GET", f"/v1/tasks/{task['id']}", h.user_token)
    hers = env.api.ok("GET", f"/v1/tasks/{task['id']}", w.user_token)
    assert his["outcome"] == hers["outcome"]
    assert his["outcome"]["proposal"]["proposed_time"] == DINNER_AT_730["proposed_time"]
    assert [e["event"]["kind"] for e in his["history"]] == [
        "create", "enqueue", "delivered", "request_approval", "counterpropose", "accept"]
    assert his["history"][4]["event"]["approval_id"] == approval["id"]

    # Agreement is not completion: that takes an explicit act.
    status, done = respond(env, w, task["id"], "complete", 2, "w-done", evidence={"booking_reference": "R1"})
    assert status == 200 and done["task"]["state"] == "COMPLETED"
    kinds = [i["message"]["type"] for i in inbox(env, w, proposal["cursor"])]
    assert kinds == ["approval.granted", "coordination.accept", "task.status", "task.completed"]
