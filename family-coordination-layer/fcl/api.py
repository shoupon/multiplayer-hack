"""HTTP boundary. The only module that knows about FastAPI.

Every input is parsed here into typed values; services trust what they receive.
"""

from __future__ import annotations

import hashlib
import threading
import time
from contextlib import asynccontextmanager
from typing import Any, Callable, Literal, Optional

from fastapi import Depends, FastAPI, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from . import agents, approvals, audit, families, grants, identity, messaging, permissions, tasks, webhooks
from .agents import GrantSpec
from .auth import AgentPrincipal, Principal, UserPrincipal, acting_user_id, resolve_principal
from .context import Ctx
from .errors import AppError, DeniedAudit, bad_request, forbidden, unauthenticated
from .ids import id_pattern
from .messaging import request_hash
from .policy import Action
from .proposal import INTENT_PATTERN, Proposal

ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {"description": "Invalid request"},
    401: {"description": "Missing or invalid credentials"},
    403: {"description": "Not authorized"},
    404: {"description": "Not found"},
}


def IdPath(kind: str) -> Any:
    return Path(pattern=id_pattern(kind))


def IdQuery(kind: str) -> Any:
    return Query(pattern=id_pattern(kind))


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---- request bodies --------------------------------------------------------------------


class CreateUserBody(Body):
    display_name: str = Field(min_length=1, max_length=100)


class CreateFamilyBody(Body):
    name: str = Field(min_length=1, max_length=100)


class CreateInvitationBody(Body):
    role: Literal["admin", "member"] = "member"
    ttl_hours: int = Field(default=72, ge=1, le=720)


class AcceptInvitationBody(Body):
    code: str = Field(min_length=10, max_length=200)


class GrantSpecBody(Body):
    family_id: str = Field(pattern=id_pattern("family"))
    action: Action
    target_user_id: Optional[str] = Field(default=None, pattern=id_pattern("user"))
    requires_approval: bool = False
    expires_at: Optional[AwareDatetime] = None

    def spec(self) -> GrantSpec:
        return GrantSpec(self.family_id, self.action, self.target_user_id, self.requires_approval, self.expires_at)


class CreateGrantBody(GrantSpecBody):
    agent_id: str = Field(pattern=id_pattern("agent"))


class RegisterAgentBody(Body):
    display_name: str = Field(min_length=1, max_length=100)
    provider: str = Field(min_length=1, max_length=50, description="Self-reported label. Never treated as verified.")
    capabilities: list[str] = Field(default_factory=list, max_length=20)
    transports: list[Literal["inbox", "mcp"]] = Field(default_factory=list)
    grants: list[GrantSpecBody] = Field(default_factory=list, max_length=50)


class IssueCredentialBody(Body):
    label: Optional[str] = Field(default=None, max_length=100)


class SendMessageBody(Body):
    family_id: str = Field(pattern=id_pattern("family"))
    to_agent_id: str = Field(pattern=id_pattern("agent"))
    type: str = Field(max_length=64)
    payload: dict[str, Any]
    conversation_id: Optional[str] = Field(default=None, pattern=id_pattern("conversation"))
    correlation_id: Optional[str] = Field(default=None, max_length=100)
    causation_id: Optional[str] = Field(default=None, pattern=id_pattern("message"))
    requires_response: bool = False
    expires_at: Optional[AwareDatetime] = None


class AckBody(Body):
    through: int = Field(ge=0)


class CreateTaskBody(Body):
    family_id: str = Field(pattern=id_pattern("family"))
    invitees: list[str] = Field(min_length=1, max_length=10, description="Agent ids of the other participants.")
    intent: str = Field(pattern=INTENT_PATTERN)
    proposal: Proposal
    expires_at: AwareDatetime


class RespondBody(Body):
    kind: Literal["accept", "decline", "counterpropose", "complete"]
    proposal_version: int = Field(ge=1)
    proposal: Optional[Proposal] = None
    reason: Optional[str] = Field(default=None, max_length=500)
    evidence: Optional[dict[str, Any]] = Field(
        default=None, description="Required for complete: what proves the plan was carried out, e.g. a booking reference.")


class CancelBody(Body):
    reason: Optional[str] = Field(default=None, max_length=500)


class PermissionRequestBody(Body):
    family_id: str = Field(pattern=id_pattern("family"))
    scope: str = Field(max_length=64, description="One of: " + ", ".join(permissions.SCOPES))
    params: dict[str, Any]
    purpose: str = Field(min_length=1, max_length=500, description="Shown to the owner verbatim.")
    expires_at: AwareDatetime
    task_id: Optional[str] = Field(default=None, pattern=id_pattern("task"))


class DecisionBody(Body):
    decision: Literal["grant", "deny"]


class WebhookBody(Body):
    agent_id: str = Field(pattern=id_pattern("agent"))
    url: str = Field(max_length=2000)


# ---- middleware ------------------------------------------------------------------------


class BodyLimit:
    """Rejects request bodies over the limit, whether or not Content-Length is honest."""

    def __init__(self, app: Any, limit: int):
        self.app, self.limit = app, limit

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        declared = dict(scope.get("headers") or []).get(b"content-length")
        if declared and declared.isdigit() and int(declared) > self.limit:
            return await _error(413, "body_too_large", f"request body exceeds {self.limit} bytes")(scope, receive, send)
        seen = 0

        async def limited() -> dict:
            nonlocal seen
            message = await receive()
            seen += len(message.get("body", b""))
            if seen > self.limit:
                raise AppError(413, "body_too_large", f"request body exceeds {self.limit} bytes")
            return message

        await self.app(scope, limited, send)


class RateLimit:
    """Fixed one-minute window per credential (or per client address when unauthenticated)."""

    def __init__(self, app: Any, per_minute: int):
        self.app, self.per_minute = app, per_minute
        self.windows: dict[str, tuple[int, int]] = {}
        self.lock = threading.Lock()

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        auth = dict(scope.get("headers") or []).get(b"authorization")
        key = hashlib.sha256(auth).hexdigest() if auth else f"ip:{(scope.get('client') or ('?',))[0]}"
        minute = int(time.time() // 60)
        with self.lock:
            window, count = self.windows.get(key, (minute, 0))
            count = count + 1 if window == minute else 1
            self.windows[key] = (minute, count)
            if len(self.windows) > 10_000:
                self.windows = {k: v for k, v in self.windows.items() if v[0] == minute}
        if count > self.per_minute:
            return await _error(429, "rate_limited", "too many requests")(scope, receive, send)
        await self.app(scope, receive, send)


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


# ---- app -------------------------------------------------------------------------------


def create_app(ctx: Ctx, lifespan_hooks: Optional[list[Callable[[], Callable[[], None]]]] = None) -> FastAPI:
    """`lifespan_hooks` start background work and return a function that stops it."""

    @asynccontextmanager
    async def lifespan(_: FastAPI):  # type: ignore[no-untyped-def]
        stops = [start() for start in (lifespan_hooks or [])]
        yield
        for stop in stops:
            stop()

    app = FastAPI(
        title="Family Coordination Layer",
        version="0.1.0",
        description="Provider-neutral coordination for family members' personal AI agents.",
        lifespan=lifespan,
        responses=ERROR_RESPONSES,
    )
    app.add_middleware(BodyLimit, limit=ctx.settings.body_limit_bytes)
    app.add_middleware(RateLimit, per_minute=ctx.settings.rate_limit_per_minute)

    @app.exception_handler(AppError)
    def on_app_error(request: Request, exc: AppError) -> JSONResponse:
        if exc.denied is not None:
            # The command's transaction rolled back; record the denial in its own transaction.
            with ctx.db.tx() as c:
                audit.record(
                    c, ctx.clock(), getattr(request.state, "principal", None), exc.denied.action, "denied",
                    family_id=exc.denied.family_id, resource_type=exc.denied.resource_type,
                    resource_id=exc.denied.resource_id, reason=exc.code,
                )
        return _error(exc.status, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    def on_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        details = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:10])
        return _error(400, "invalid_request", details)

    # ---- auth dependencies ----

    def principal(request: Request) -> Principal:
        p = resolve_principal(ctx, request.headers.get("authorization"))
        request.state.principal = p
        if p is None:
            raise unauthenticated()
        if isinstance(p, AgentPrincipal) and not p.agent_active:
            raise forbidden("agent_disabled", "this agent is disabled", DeniedAudit("authenticate"))
        return p

    def user(request: Request, p: Principal = Depends(principal)) -> UserPrincipal:
        if not isinstance(p, UserPrincipal):
            raise forbidden("user_credential_required", "this endpoint requires a human user credential",
                            DeniedAudit(f"{request.method} {request.url.path}"))
        return p

    def agent(request: Request, p: Principal = Depends(principal)) -> AgentPrincipal:
        if not isinstance(p, AgentPrincipal):
            raise forbidden("agent_credential_required", "this endpoint requires an agent credential",
                            DeniedAudit(f"{request.method} {request.url.path}"))
        return p

    def idempotency_key(request: Request) -> str:
        key = request.headers.get("idempotency-key", "")
        if not (1 <= len(key) <= 200) or not all(ch.isalnum() or ch in "_-:." for ch in key):
            raise bad_request("idempotency_key_required", "Idempotency-Key header (1-200 of [A-Za-z0-9_-:.]) is required")
        return key

    app.state.deps = {"principal": principal, "user": user, "agent": agent, "idempotency_key": idempotency_key}

    # ---- identity ----

    @app.post("/v1/users", status_code=201, tags=["identity"])
    def create_user(body: CreateUserBody) -> dict[str, Any]:
        """MVP bootstrap: creates a user and returns their token once."""
        return identity.create_user(ctx, body.display_name)

    @app.get("/v1/me", tags=["identity"])
    def get_me(p: Principal = Depends(principal)) -> dict[str, Any]:
        if isinstance(p, AgentPrincipal):
            return {"kind": "agent", "agent_id": p.agent_id, "owner_user_id": p.owner_user_id}
        return {"kind": "user", **identity.me(ctx, p.user_id)}

    # ---- families ----

    @app.post("/v1/families", status_code=201, tags=["families"])
    def create_family(body: CreateFamilyBody, p: UserPrincipal = Depends(user)) -> dict[str, Any]:
        return families.create_family(ctx, p, body.name)

    @app.get("/v1/families/{family_id}", tags=["families"])
    def get_family(family_id: str = IdPath("family"), p: Principal = Depends(principal)) -> dict[str, Any]:
        return families.get_family(ctx, acting_user_id(p), family_id)

    @app.post("/v1/families/{family_id}/invitations", status_code=201, tags=["families"])
    def create_invitation(body: CreateInvitationBody, family_id: str = IdPath("family"), p: UserPrincipal = Depends(user)) -> dict[str, Any]:
        return families.create_invitation(ctx, p, family_id, body.role, body.ttl_hours)

    @app.post("/v1/invitations/accept", tags=["families"])
    def accept_invitation(body: AcceptInvitationBody, p: UserPrincipal = Depends(user)) -> dict[str, Any]:
        return families.accept_invitation(ctx, p, body.code)

    @app.delete("/v1/families/{family_id}/members/{user_id}", status_code=204, tags=["families"])
    def remove_member(family_id: str = IdPath("family"), user_id: str = IdPath("user"), p: UserPrincipal = Depends(user)) -> Response:
        families.remove_member(ctx, p, family_id, user_id)
        return Response(status_code=204)

    # ---- agents ----

    @app.post("/v1/agents", status_code=201, tags=["agents"])
    def register_agent(body: RegisterAgentBody, p: UserPrincipal = Depends(user)) -> dict[str, Any]:
        for cap in body.capabilities:
            if not (cap and cap[0].isalpha() and cap.replace("_", "").isalnum() and cap.islower() and len(cap) <= 50):
                raise bad_request("invalid_request", f"capability {cap!r} must be a lowercase identifier")
        return agents.register_agent(ctx, p, body.display_name, body.provider, body.capabilities, list(body.transports),
                                     [g.spec() for g in body.grants])

    @app.get("/v1/agents", tags=["agents"])
    def discover_agents(family_id: str = IdQuery("family"), p: Principal = Depends(principal)) -> dict[str, Any]:
        return {"items": agents.discover(ctx, p, family_id)}

    @app.get("/v1/agents/{agent_id}/capabilities", tags=["agents"])
    def agent_capabilities(agent_id: str = IdPath("agent"), family_id: str = IdQuery("family"), p: Principal = Depends(principal)) -> dict[str, Any]:
        return agents.capabilities(ctx, p, family_id, agent_id)

    @app.post("/v1/agents/{agent_id}/credentials", status_code=201, tags=["agents"])
    def issue_credential(body: IssueCredentialBody, agent_id: str = IdPath("agent"), p: UserPrincipal = Depends(user)) -> dict[str, str]:
        return agents.issue_credential(ctx, p, agent_id, body.label)

    @app.delete("/v1/agents/{agent_id}/credentials/{credential_id}", status_code=204, tags=["agents"])
    def revoke_credential(agent_id: str = IdPath("agent"), credential_id: str = IdPath("credential"), p: UserPrincipal = Depends(user)) -> Response:
        agents.revoke_credential(ctx, p, agent_id, credential_id)
        return Response(status_code=204)

    @app.delete("/v1/agents/{agent_id}", status_code=204, tags=["agents"])
    def disable_agent(agent_id: str = IdPath("agent"), p: UserPrincipal = Depends(user)) -> Response:
        agents.disable_agent(ctx, p, agent_id)
        return Response(status_code=204)

    # ---- grants ----

    @app.post("/v1/grants", status_code=201, tags=["grants"])
    def create_grant(body: CreateGrantBody, p: UserPrincipal = Depends(user)) -> dict[str, Any]:
        return grants.create_grant(ctx, p, body.agent_id, body.family_id, body.action, body.target_user_id,
                                   body.requires_approval, body.expires_at)

    @app.get("/v1/grants", tags=["grants"])
    def list_grants(agent_id: str = IdQuery("agent"), p: UserPrincipal = Depends(user)) -> dict[str, Any]:
        return {"items": grants.list_grants(ctx, p, agent_id)}

    @app.delete("/v1/grants/{grant_id}", status_code=204, tags=["grants"])
    def revoke_grant(grant_id: str = IdPath("grant"), p: UserPrincipal = Depends(user)) -> Response:
        grants.revoke_grant(ctx, p, grant_id)
        return Response(status_code=204)

    # ---- messages ----

    @app.post("/v1/messages", tags=["messages"])
    def send_message(body: SendMessageBody, response: Response, p: AgentPrincipal = Depends(agent),
                     key: str = Depends(idempotency_key)) -> dict[str, Any]:
        created, env = messaging.send_direct(
            ctx, p, idempotency_key=key, req_hash=request_hash(body.model_dump(mode="json")), family_id=body.family_id,
            to_agent_id=body.to_agent_id, message_type=body.type, payload=body.payload,
            conversation_id=body.conversation_id, correlation_id=body.correlation_id, causation_id=body.causation_id,
            requires_response=body.requires_response, expires_at=body.expires_at,
        )
        response.status_code = 201 if created else 200
        return env

    @app.get("/v1/messages/{message_id}", tags=["messages"])
    def get_message(message_id: str = IdPath("message"), p: Principal = Depends(principal)) -> dict[str, Any]:
        return messaging.get_message(ctx, p, message_id)

    @app.get("/v1/agents/{agent_id}/inbox", tags=["messages"])
    def get_inbox(agent_id: str = IdPath("agent"), after: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200),
                  p: AgentPrincipal = Depends(agent)) -> dict[str, Any]:
        return messaging.inbox(ctx, p, agent_id, after, limit)

    @app.post("/v1/agents/{agent_id}/inbox/ack", tags=["messages"])
    def ack_inbox(body: AckBody, agent_id: str = IdPath("agent"), p: AgentPrincipal = Depends(agent)) -> dict[str, int]:
        return {"acknowledged": messaging.acknowledge(ctx, p, agent_id, body.through)}

    # ---- tasks ----

    @app.post("/v1/tasks", tags=["tasks"])
    def create_task(body: CreateTaskBody, response: Response, p: AgentPrincipal = Depends(agent),
                    key: str = Depends(idempotency_key)) -> dict[str, Any]:
        created, task = tasks.create(
            ctx, p, idempotency_key=key, req_hash=request_hash(body.model_dump(mode="json")), family_id=body.family_id,
            invitee_agent_ids=body.invitees, intent=body.intent, proposal=body.proposal, expires_at=body.expires_at,
        )
        response.status_code = 201 if created else 200
        return task

    @app.get("/v1/tasks", tags=["tasks"])
    def list_tasks(state: Optional[str] = Query(None, max_length=32), limit: int = Query(50, ge=1, le=200),
                   p: Principal = Depends(principal)) -> dict[str, Any]:
        return {"items": tasks.list_for(ctx, p, state, limit)}

    @app.get("/v1/tasks/{task_id}", tags=["tasks"])
    def get_task(task_id: str = IdPath("task"), p: Principal = Depends(principal)) -> dict[str, Any]:
        return tasks.get(ctx, p, task_id)

    @app.post("/v1/tasks/{task_id}/responses", tags=["tasks"])
    def respond(body: RespondBody, task_id: str = IdPath("task"), p: AgentPrincipal = Depends(agent),
                key: str = Depends(idempotency_key)) -> dict[str, Any]:
        if (body.kind == "counterpropose") != (body.proposal is not None):
            raise bad_request("invalid_request", "proposal is required for counterpropose and only allowed there")
        if (body.kind == "complete") != (body.evidence is not None):
            raise bad_request("invalid_request", "evidence is required for complete and only allowed there")
        return tasks.respond(ctx, p, task_id, idempotency_key=key, req_hash=request_hash(body.model_dump(mode="json")),
                             kind=body.kind, proposal_version=body.proposal_version, proposal=body.proposal,
                             reason=body.reason, evidence=body.evidence)

    @app.post("/v1/tasks/{task_id}/cancel", tags=["tasks"])
    def cancel_task(body: CancelBody, task_id: str = IdPath("task"), p: AgentPrincipal = Depends(agent)) -> dict[str, Any]:
        return tasks.cancel(ctx, p, task_id, body.reason)

    # ---- approvals ----

    @app.post("/v1/approvals", tags=["approvals"])
    def request_permission(body: PermissionRequestBody, response: Response, p: AgentPrincipal = Depends(agent),
                           key: str = Depends(idempotency_key)) -> dict[str, Any]:
        """An agent asks its own owner for a scoped permission to act outside FCL. Only the owner can decide."""
        created, approval_id = permissions.request(
            ctx, p, idempotency_key=key, family_id=body.family_id, scope=body.scope, params=body.params,
            purpose=body.purpose, expires_at=body.expires_at, task_id=body.task_id)
        response.status_code = 201 if created else 200
        return approvals.get(ctx, p, approval_id)

    @app.get("/v1/approvals/{approval_id}", tags=["approvals"])
    def get_approval(approval_id: str = IdPath("approval"), p: Principal = Depends(principal)) -> dict[str, Any]:
        return approvals.get(ctx, p, approval_id)

    @app.get("/v1/approvals", tags=["approvals"])
    def list_approvals(status: Optional[Literal["pending", "granted", "denied", "expired", "superseded"]] = None,
                       p: UserPrincipal = Depends(user)) -> dict[str, Any]:
        return {"items": approvals.list_for(ctx, p, status)}

    @app.post("/v1/approvals/{approval_id}/decisions", tags=["approvals"])
    def decide_approval(body: DecisionBody, approval_id: str = IdPath("approval"), p: UserPrincipal = Depends(user)) -> dict[str, Any]:
        """Human-only: an agent credential is rejected even when it belongs to the approver."""
        return approvals.decide(ctx, p, approval_id, body.decision)

    # ---- webhooks ----

    @app.post("/v1/webhooks", status_code=201, tags=["webhooks"])
    def subscribe_webhook(body: WebhookBody, p: UserPrincipal = Depends(user)) -> dict[str, Any]:
        return webhooks.subscribe(ctx, p, body.agent_id, body.url)

    @app.delete("/v1/webhooks/{webhook_id}", status_code=204, tags=["webhooks"])
    def unsubscribe_webhook(webhook_id: str = IdPath("webhook"), p: UserPrincipal = Depends(user)) -> Response:
        webhooks.unsubscribe(ctx, p, webhook_id)
        return Response(status_code=204)

    # ---- audit ----

    @app.get("/v1/audit", tags=["audit"])
    def get_audit(after: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500), p: UserPrincipal = Depends(user)) -> dict[str, Any]:
        items = audit.list_for_user(ctx, p.user_id, after, limit)
        return {"items": items, "next_after": items[-1]["id"] if items else after}

    return app
