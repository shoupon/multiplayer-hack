"""HTTP boundary. The only module that knows about FastAPI.

Every input is parsed here into typed values; services trust what they receive.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import threading
import time
from contextlib import asynccontextmanager
from importlib import resources
from typing import Any, Callable, Literal, Optional

from fastapi import Depends, FastAPI, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from . import agents, approvals, audit, briefing, families, grants, identity, links, messaging, permissions, rooms, tasks, webhooks
from .agents import GrantSpec
from .auth import AgentPrincipal, Principal, UserPrincipal, acting_user_id, resolve_principal
from .context import Ctx
from .errors import AppError, DeniedAudit, bad_request, forbidden, not_found, unauthenticated
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


class CreateLinkBody(Body):
    ttl_hours: int = Field(default=24, ge=1, le=168)


class RoomBrief(Body):
    """What everyone agreed on before the agents start. Shown on the room page."""
    participants: Optional[str] = Field(default=None, max_length=200, description="Who takes part, e.g. 'Up to 3 agents'.")
    decision_rule: Optional[str] = Field(default=None, max_length=200, description="e.g. 'Everyone agrees'.")
    deadline: Optional[str] = Field(default=None, max_length=200, description="e.g. 'Today'.")
    ground_rules: Optional[str] = Field(default=None, max_length=200, description="e.g. 'Ask owners before spending'.")


class CreateRoomBody(Body):
    name: str = Field(min_length=1, max_length=100, description="The goal, shown as the room's title.")
    ttl_hours: int = Field(default=72, ge=1, le=336)
    max_members: int = Field(default=10, ge=2, le=20)
    brief: RoomBrief = Field(default_factory=RoomBrief)


class CoordinatorBody(Body):
    agent_id: Optional[str] = Field(default=None, pattern=id_pattern("agent"))


class RoomSummary(Body):
    headline: str = Field(min_length=1, max_length=200)
    sub: Optional[str] = Field(default=None, max_length=300)
    plan: list[str] = Field(default_factory=list, max_length=12)
    positions: dict[str, str] = Field(default_factory=dict, max_length=20, description="agent_id -> that agent's position.")
    booking: Optional[dict[str, str]] = Field(default=None, max_length=10, description="e.g. restaurant, time, party_size, reference.")


class RoomStatusBody(Body):
    status: Literal["waiting", "live", "done"]
    summary: Optional[RoomSummary] = None


class JoinRoomBody(Body):
    display_name: str = Field(min_length=1, max_length=100, description="The person this agent acts for.")
    agent_name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    provider: str = Field(default="unknown", min_length=1, max_length=50)


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


def client_address(scope: dict, trust_forwarded_for: bool) -> str:
    """Behind a proxy that appends the caller's address (Cloud Run does), the last X-Forwarded-For hop is the caller."""
    if trust_forwarded_for:
        forwarded = dict(scope.get("headers") or []).get(b"x-forwarded-for", b"").decode("latin-1")
        hops = [h.strip() for h in forwarded.split(",") if h.strip()]
        if hops:
            return hops[-1]
    return (scope.get("client") or ("?",))[0]


class RateLimit:
    """
    Fixed one-minute window per credential (or per client address when unauthenticated).
    Monitor-link routes get their own, tighter window per client address.
    """

    def __init__(self, app: Any, per_minute: int, link_per_minute: int, trust_forwarded_for: bool):
        self.app, self.per_minute, self.link_per_minute = app, per_minute, link_per_minute
        self.trust_forwarded_for = trust_forwarded_for
        self.windows: dict[str, tuple[int, int]] = {}
        self.lock = threading.Lock()

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        auth = dict(scope.get("headers") or []).get(b"authorization")
        address = client_address(scope, self.trust_forwarded_for)
        limit = self.per_minute
        path = scope.get("path", "")
        if path.startswith(("/l/", "/r/")) or path.endswith(("/join", "/coordinator")):
            key, limit = f"link:{address}", self.link_per_minute
        else:
            key = hashlib.sha256(auth).hexdigest() if auth else f"ip:{address}"
        minute = int(time.time() // 60)
        with self.lock:
            window, count = self.windows.get(key, (minute, 0))
            count = count + 1 if window == minute else 1
            self.windows[key] = (minute, count)
            if len(self.windows) > 10_000:
                self.windows = {k: v for k, v in self.windows.items() if v[0] == minute}
        if count > limit:
            return await _error(429, "rate_limited", "too many requests")(scope, receive, send)
        await self.app(scope, receive, send)


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


_BASE_URL = re.compile(r"^https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?$")


def _markdown(text: str) -> Response:
    """Briefings are read by agents and unfurled by chat apps: never cache, index, or leak the URL onward."""
    return Response(text, media_type="text/markdown; charset=utf-8", headers={
        "cache-control": "no-store", "x-robots-tag": "noindex", "referrer-policy": "no-referrer"})


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
    app.add_middleware(RateLimit, per_minute=ctx.settings.rate_limit_per_minute,
                       link_per_minute=ctx.settings.link_rate_limit_per_minute,
                       trust_forwarded_for=ctx.settings.trust_forwarded_for)

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

    # ---- web page ----

    home_page = (resources.files("fcl") / "web" / "index.html").read_text(encoding="utf-8")

    @app.get("/", include_in_schema=False)
    def home() -> HTMLResponse:
        return HTMLResponse(home_page, headers={"Cache-Control": "no-cache"})

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

    def base_url(request: Request) -> str:
        url = ctx.settings.public_url or str(request.base_url).rstrip("/")
        if not _BASE_URL.match(url):
            raise bad_request("invalid_host", "set FCL_PUBLIC_URL to this server's public origin")
        return url

    app.state.deps = {"principal": principal, "user": user, "agent": agent, "idempotency_key": idempotency_key}

    # ---- identity ----

    @app.post("/v1/users", status_code=201, tags=["identity"])
    def create_user(body: CreateUserBody, request: Request) -> dict[str, Any]:
        """Creates a user and returns their token once. When FCL_SIGNUP_TOKEN is set, X-Signup-Token must match it."""
        expected = ctx.settings.signup_token
        if expected and not hmac.compare_digest(request.headers.get("x-signup-token", "").encode(), expected.encode()):
            raise forbidden("signup_closed", "signup requires a valid X-Signup-Token header")
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

    # ---- monitor links ----

    @app.post("/v1/agents/{agent_id}/links", status_code=201, tags=["links"])
    def create_link(body: CreateLinkBody, request: Request, agent_id: str = IdPath("agent"),
                    p: UserPrincipal = Depends(user)) -> dict[str, Any]:
        """One URL to hand your agent. Opening it shows a briefing; one POST to its claim URL issues a token."""
        base = base_url(request)
        view, code = links.create(ctx, p, agent_id, body.ttl_hours)
        return {**view, "url": f"{base}/l/{code}"}

    @app.delete("/v1/agents/{agent_id}/links/{link_id}", status_code=204, tags=["links"])
    def revoke_link(agent_id: str = IdPath("agent"), link_id: str = IdPath("link"), p: UserPrincipal = Depends(user)) -> Response:
        links.revoke(ctx, p, agent_id, link_id)
        return Response(status_code=204)

    @app.get("/v1/agents/{agent_id}/briefing", tags=["links"], response_class=Response)
    def get_briefing(request: Request, agent_id: str = IdPath("agent"), p: Principal = Depends(principal)) -> Response:
        """The agent's standing instructions. Readable by the agent itself and by its owner."""
        with ctx.db.read() as c:
            if isinstance(p, AgentPrincipal):
                allowed = p.agent_id == agent_id
            else:
                allowed = c.one("SELECT 1 FROM agents WHERE id = ? AND owner_user_id = ?", (agent_id, p.user_id)) is not None
            if not allowed:
                raise not_found("agent")
            return _markdown(briefing.render(c, ctx.clock(), agent_id, base_url(request), "token"))

    @app.get("/l/{code}", tags=["links"], response_class=Response)
    def open_link(request: Request, code: str = Path(pattern=links.CODE_PATTERN)) -> Response:
        """Safe to repeat: never reveals a token and never consumes the link."""
        base = base_url(request)
        now = ctx.clock()
        with ctx.db.read() as c:
            link = links.resolve(c, now, code)
            access = "unclaimed" if link.state == "open" else "claimed"
            return _markdown(briefing.render(c, now, link.agent_id, base, access, f"{base}/l/{code}/claim"))

    @app.post("/l/{code}/claim", status_code=201, tags=["links"])
    def claim_link(code: str = Path(pattern=links.CODE_PATTERN)) -> dict[str, str]:
        return links.claim(ctx, code)

    # ---- rooms ----

    room_page = (resources.files("fcl") / "web" / "room.html").read_text(encoding="utf-8")
    room_code = re.compile(rooms.CODE_PATTERN)

    def room_or_404(c: Any, code: str) -> rooms.Room:
        if not room_code.match(code):
            raise not_found("room")
        return rooms.resolve(c, ctx.clock(), code)

    def room_briefing(request: Request, code: str) -> Response:
        base = base_url(request)
        with ctx.db.read() as c:
            room = room_or_404(c, code)
            return _markdown(briefing.render_room(c, room, base, f"{base}/{code}"))

    @app.post("/v1/rooms", status_code=201, tags=["rooms"])
    def create_room(body: CreateRoomBody, request: Request, p: UserPrincipal = Depends(user)) -> dict[str, Any]:
        """A fresh family plus one shareable code. People open the URL; their agents read URL/agent."""
        base = base_url(request)
        view, code = rooms.create(ctx, p, body.name, "schedule_dinner", body.ttl_hours, body.max_members,
                                  body.brief.model_dump(exclude_none=True))
        return {**view, "url": f"{base}/r/{code}", "room_url": f"{base}/{code}", "agent_url": f"{base}/{code}/agent"}

    @app.post("/v1/rooms/{room_id}/status", tags=["rooms"])
    def set_room_status(body: RoomStatusBody, room_id: str = IdPath("room"), p: AgentPrincipal = Depends(agent)) -> dict[str, Any]:
        """Coordinator only: move the room to live or done, with the summary the room page shows."""
        return rooms.set_status(ctx, p, room_id, body.status, body.summary.model_dump(exclude_none=True) if body.summary else None)

    @app.get("/r/{code}", tags=["rooms"], response_class=Response)
    def open_room_link(request: Request, code: str = Path(pattern=rooms.CODE_PATTERN)) -> Response:
        """The original room briefing. Unchanged; new agents use /{code}/agent."""
        base = base_url(request)
        with ctx.db.read() as c:
            room = rooms.resolve(c, ctx.clock(), code)
            return _markdown(briefing.render_legacy_room(c, room, base, f"{base}/r/{code}/join"))

    @app.get("/{code}/agent", tags=["rooms"], response_class=Response)
    def open_room_briefing(request: Request, code: str) -> Response:
        """The instructions an agent follows to take part in the room. Safe to repeat; holds no secrets."""
        return room_briefing(request, code)

    @app.get("/{code}/state", tags=["rooms"])
    def room_state(code: str) -> dict[str, Any]:
        """What the room page shows: agents, the live timeline, tasks, and the summary."""
        with ctx.db.read() as c:
            return rooms.state(c, room_or_404(c, code))

    @app.post("/r/{code}/join", status_code=201, tags=["rooms"])
    def join_room(body: JoinRoomBody, code: str = Path(pattern=rooms.CODE_PATTERN)) -> dict[str, Any]:
        return rooms.join(ctx, code, body.display_name, body.agent_name or f"{body.display_name} assistant", body.provider)

    @app.post("/{code}/join", status_code=201, tags=["rooms"])
    def join_room_page(body: JoinRoomBody, code: str) -> dict[str, Any]:
        if not room_code.match(code):
            raise not_found("room")
        return rooms.join(ctx, code, body.display_name, body.agent_name or f"{body.display_name} assistant", body.provider)

    @app.post("/{code}/coordinator", status_code=201, tags=["rooms"])
    def add_room_coordinator(code: str, body: Optional[CoordinatorBody] = None, p: UserPrincipal = Depends(user)) -> dict[str, Any]:
        """Room creator only, once. No body: a new coordinator agent and its token. `agent_id`: promote an agent
        already in the room."""
        if not room_code.match(code):
            raise not_found("room")
        return rooms.add_coordinator(ctx, p, code, body.agent_id if body else None)

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
                  unacknowledged: bool = Query(False, description="Only deliveries not yet acknowledged; no cursor needed."),
                  p: AgentPrincipal = Depends(agent)) -> dict[str, Any]:
        return messaging.inbox(ctx, p, agent_id, after, limit, unacknowledged)

    @app.get("/v1/agents/{agent_id}/inbox/count", tags=["messages"])
    def count_inbox(agent_id: str = IdPath("agent"), p: AgentPrincipal = Depends(agent)) -> dict[str, int]:
        """Side-effect free: safe for a background watcher to poll."""
        return messaging.inbox_count(ctx, p, agent_id)

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

    @app.get("/{code}", include_in_schema=False)
    def open_room(request: Request, code: str) -> Response:
        """People get the room page; agents (no text/html in Accept, e.g. curl) get the briefing."""
        if "text/html" not in request.headers.get("accept", ""):
            return room_briefing(request, code)
        with ctx.db.read() as c:
            room_or_404(c, code)
        return HTMLResponse(room_page, headers={"cache-control": "no-store", "x-robots-tag": "noindex",
                                                "referrer-policy": "no-referrer"})

    return app
