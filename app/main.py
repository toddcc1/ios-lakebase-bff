"""Public BFF: bridge Apple / Auth0 identity to a Databricks App + Lakebase.

iOS --(Apple or Auth0 identity token)--> this service
    --(verify token, resolve/create users row)-->
    --(call the Databricks App as this BFF's own SP)-->
Databricks App --(RLS via app.user_id)--> Lakebase

Databricks Apps ingress rejects callers that are not Databricks principals
with CAN_USE, so a consumer ID token can never reach the app directly.
"""
from __future__ import annotations

import logging
import time
import uuid
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, Response, UploadFile
from pydantic import BaseModel

from .databricks_client import call_app
from .identity_auth import IdentityTokenError, VerifiedIdentity, verify_identity_token
from . import request_context
from .session import SessionError, issue, verify

log = logging.getLogger("ios-lakebase-bff")

app = FastAPI(title="ios-lakebase-bff")


@app.middleware("http")
async def correlated_request_log(request: Request, call_next):
    supplied = request.headers.get("X-Request-Id", "")
    try:
        correlation_id = str(uuid.UUID(supplied))
    except (ValueError, AttributeError):
        correlation_id = str(uuid.uuid4())

    tokens = request_context.begin_request(correlation_id)
    started = time.monotonic()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        response.headers["X-Request-Id"] = correlation_id
        return response
    finally:
        elapsed_ms = round((time.monotonic() - started) * 1000)
        log.info(
            "request_complete request_id=%s method=%s path=%s status=%s "
            "elapsed_ms=%s issuer=%s user_id=%s",
            correlation_id,
            request.method,
            request.url.path,
            status_code,
            elapsed_ms,
            request_context.identity_issuer.get(),
            request_context.user_id.get(),
        )
        request_context.end_request(tokens)


@app.get("/health")
def health() -> dict:
    """No auth, no downstream calls -- just proves the service is up."""
    return {"status": "ok"}


def _identity_claims(
    authorization: str | None,
    oidc_nonce: str | None = None,
) -> VerifiedIdentity:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "missing Bearer token")
    token = authorization.split(" ", 1)[1].strip()
    try:
        return verify_identity_token(token, expected_oidc_nonce=oidc_nonce)
    except IdentityTokenError as exc:
        raise HTTPException(401, str(exc)) from exc


def require_session(authorization: str | None = Header(default=None)) -> str:
    """FastAPI dependency for every endpoint except /health and /me: verifies
    this BFF's own session token (not an Apple identity token -- those are
    single-use, see session.py) and returns the resolved user_id.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "missing Bearer token")
    token = authorization.split(" ", 1)[1].strip()
    try:
        resolved_user_id = verify(token)
        request_context.user_id.set(resolved_user_id)
        return resolved_user_id
    except SessionError as exc:
        raise HTTPException(401, f"invalid session: {exc}") from exc


@app.get("/me")
async def me(
    authorization: str | None = Header(default=None),
    x_oidc_nonce: str | None = Header(default=None),
) -> dict:
    """Verify Apple or Okta identity, resolve the internal user, and issue
    the BFF session token used by every subsequent call.
    """
    identity = _identity_claims(authorization, x_oidc_nonce)
    request_context.identity_issuer.set(identity.provider)
    resp = await call_app(
        "POST",
        "/internal/users/resolve",  # no user_id yet -- this IS the resolution call
        json=identity.resolver_payload,
    )
    if resp.status_code >= 400:
        log.error("databricks-app /internal/users/resolve failed: %s %s", resp.status_code, resp.text)
        raise HTTPException(502, "failed to resolve user")
    resolved = resp.json()
    user_id = resolved["user_id"]
    request_context.user_id.set(user_id)
    result = {
        "user_id": user_id,
        "created": resolved["created"],
        "issuer": identity.provider,
        "session_token": issue(user_id),
    }
    # Preserve the existing Apple response field for current iOS builds.
    result[f"{identity.provider}_sub"] = identity.subject
    return result


@app.get("/cards")
async def list_cards(status: str | None = None, user_id: str = Depends(require_session)):
    """Proxies databricks-app's GET /api/cards, scoped to the caller's user_id
    via RLS on the other end -- see server/db.py's resolve_user_id().
    """
    path = "/api/cards" + (f"?status={status}" if status else "")
    resp = await call_app("GET", path, user_id=user_id)
    if resp.status_code >= 400:
        log.error("databricks-app %s failed: %s %s", path, resp.status_code, resp.text)
        raise HTTPException(502, "failed to fetch cards")
    return resp.json()


@app.get("/cards/{card_id}")
async def get_card(card_id: int, user_id: str = Depends(require_session)):
    """Proxies databricks-app's GET /api/cards/{card_id} -- single card incl.
    the `comps` JSONB (layer_a/b/c breakdown), for the card detail screen.
    """
    path = f"/api/cards/{card_id}"
    resp = await call_app("GET", path, user_id=user_id)
    if resp.status_code >= 400:
        log.error("databricks-app %s failed: %s %s", path, resp.status_code, resp.text)
        raise HTTPException(502, "failed to fetch card")
    return resp.json()


@app.delete("/cards/{card_id}")
async def delete_card(card_id: int, user_id: str = Depends(require_session)):
    """Proxies databricks-app's DELETE /api/cards/{card_id} -- a plain row
    delete; every related table (price history, listing links, search
    results/sessions) already has ON DELETE CASCADE on card_id (confirmed
    live against the real FK constraints), so nothing orphaned is left
    behind at the DB level.
    """
    path = f"/api/cards/{card_id}"
    resp = await call_app("DELETE", path, user_id=user_id)
    if resp.status_code >= 400:
        log.error("databricks-app %s failed: %s %s", path, resp.status_code, resp.text)
        raise HTTPException(502, "failed to delete card")
    return resp.json()


class CardPatch(BaseModel):
    """Mirrors the fields of databricks-app's server/routes/cards.py CardPatch
    that this app's UI actually edits -- the full server-side model also
    has psa_cert_number/mlb_person_id/cfbd fields not needed here.
    """
    name: str | None = None
    grade: str | None = None
    player: str | None = None
    parallel: str | None = None
    insert_name: str | None = None
    card_number: str | None = None
    acquisition_type: str | None = None
    paid_price: float | None = None
    asking_price: float | None = None
    acquisition_venue: str | None = None
    ebay_shipping_cost: float | None = None
    ebay_fees: float | None = None
    serial_number: str | None = None
    date_acquired: str | None = None
    quantity: int | None = None
    notes: str | None = None
    value_product_id: str | None = None
    value_source: str | None = None


@app.patch("/cards/{card_id}")
async def update_card(card_id: int, body: CardPatch, user_id: str = Depends(require_session)):
    """Proxies databricks-app's PATCH /api/cards/{card_id} -- partial edit,
    used both by the "Edit acquisition details" sheet (paid/asking/grade/
    etc.) and by catalog-match confirmation (value_product_id/value_source),
    which is why `exclude_unset` matters here: only fields the caller
    actually set should ever reach the UPDATE's SET clause.
    """
    path = f"/api/cards/{card_id}"
    resp = await call_app("PATCH", path, user_id=user_id, json=body.model_dump(exclude_unset=True))
    if resp.status_code >= 400:
        log.error("databricks-app %s failed: %s %s", path, resp.status_code, resp.text)
        raise HTTPException(502, "failed to update card")
    return resp.json()


@app.get("/cards/{card_id}/market")
async def card_market(card_id: int, user_id: str = Depends(require_session)):
    """Proxies databricks-app's GET /cards/{card_id}/market -- re-prices the
    card live (Layer A/B/C) and returns the fresh comps. Note this mutates
    despite the GET verb (matches databricks-app's own route), used for a
    manual "refresh pricing" action on the card detail screen.
    """
    path = f"/api/cards/{card_id}/market"
    resp = await call_app("GET", path, user_id=user_id)
    if resp.status_code >= 400:
        log.error("databricks-app %s failed: %s %s", path, resp.status_code, resp.text)
        raise HTTPException(502, "failed to refresh card pricing")
    return resp.json()


@app.get("/cards/{card_id}/listing-justification")
async def listing_justification(card_id: int, user_id: str = Depends(require_session)):
    """Proxies databricks-app's on-demand LLM price narration -- the "Get
    listing justification" button. 400 (no price band yet) and 502
    (narration unavailable) pass through as-is rather than becoming a
    generic 502, so the UI can show the real reason.
    """
    path = f"/api/cards/{card_id}/listing-justification"
    resp = await call_app("GET", path, user_id=user_id)
    if resp.status_code == 400:
        raise HTTPException(400, resp.json().get("detail", "no price band yet"))
    if resp.status_code >= 400:
        log.error("databricks-app %s failed: %s %s", path, resp.status_code, resp.text)
        raise HTTPException(502, "failed to get listing justification")
    return resp.json()


@app.get("/players/summary")
async def players_summary(user_id: str = Depends(require_session)):
    """Proxies databricks-app's GET /api/players/summary -- one row per player
    (grouped by mlb_person_id when linked), for the player-grouped inventory view.

    Generous timeout, same precedent as the intelligence-refresh proxy below:
    this one query does several sequential lookups server-side (comps, demand
    signals, buying opportunities) and self-provisions two tables on every
    call -- comfortably inside 30s warm, but a cold Lakebase endpoint or a
    cold Databricks App container can blow past the default client timeout
    on the first real request, silently degrading the inventory UI to an ungrouped fallback on cold start.
    """
    path = "/api/players/summary"
    resp = await call_app("GET", path, user_id=user_id, timeout=90)
    if resp.status_code >= 400:
        log.error("databricks-app %s failed: %s %s", path, resp.status_code, resp.text)
        raise HTTPException(502, "failed to fetch player summary")
    return resp.json()


@app.get("/players/intelligence/{group_key}")
async def get_player_intelligence(group_key: str, user_id: str = Depends(require_session)):
    """Proxies databricks-app's GET /api/players/intelligence/{group_key} --
    cached demand (Google Trends legs + why_now narration) and ranked
    buying-opportunities, for the player detail screen's Buying Intelligence
    section. Cheap read -- never runs a fresh Trends/CardHedger/eBay scan
    itself (see the refresh proxy below for that).
    """
    path = f"/api/players/intelligence/{group_key}"
    resp = await call_app("GET", path, user_id=user_id)
    if resp.status_code >= 400:
        log.error("databricks-app %s failed: %s %s", path, resp.status_code, resp.text)
        raise HTTPException(502, "failed to fetch player intelligence")
    return resp.json()


@app.post("/players/intelligence/{group_key}/refresh")
async def refresh_player_intelligence(group_key: str, user_id: str = Depends(require_session)):
    """Proxies databricks-app's POST /api/players/intelligence/{group_key}/refresh
    -- the expensive live scan (Trends + CardHedger + eBay context). Real synchronous 10-60+ second call for a large catalog; generous timeout,
    same precedent as the other slow chained proxies.
    """
    path = f"/api/players/intelligence/{group_key}/refresh"
    resp = await call_app("POST", path, user_id=user_id, timeout=90)
    if resp.status_code >= 400:
        log.error("databricks-app %s failed: %s %s", path, resp.status_code, resp.text)
        raise HTTPException(502, "failed to refresh player intelligence")
    return resp.json()


@app.get("/cards/{card_id}/player-comps")
async def card_player_comps(card_id: int, user_id: str = Depends(require_session)):
    """Proxies databricks-app's GET /api/cards/{card_id}/player-comps -- the
    already-computed player-vs-comp stat tile data for the player detail view.
    """
    path = f"/api/cards/{card_id}/player-comps"
    resp = await call_app("GET", path, user_id=user_id)
    if resp.status_code >= 400:
        log.error("databricks-app %s failed: %s %s", path, resp.status_code, resp.text)
        raise HTTPException(502, "failed to fetch player comps")
    return resp.json()


@app.get("/cards/{card_id}/candidates")
async def card_candidates(card_id: int, q: str | None = None, user_id: str = Depends(require_session)):
    """Proxies databricks-app's GET /api/cards/{card_id}/candidates -- the
    "Browse other matches" list: a lenient candidate search that never
    auto-selects. `q` optionally refines the search text (defaults
    server-side to the card's own name/player/number).
    """
    path = f"/api/cards/{card_id}/candidates" + (f"?q={quote(q)}" if q else "")
    resp = await call_app("GET", path, user_id=user_id)
    if resp.status_code >= 400:
        log.error("databricks-app %s failed: %s %s", path, resp.status_code, resp.text)
        raise HTTPException(502, "failed to fetch match candidates")
    return resp.json()


class LinkListingBody(BaseModel):
    """Mirrors databricks-app's LinkListingBody -- confirms a real live
    listing (not a catalog product) as a card's pricing anchor, for a card
    CardHedger's catalog has no match for at all. `listing_key` is required
    server-side but only ever consumed by the find-match/card_search_results
    persistence path this simpler flow doesn't use -- synthesized from the
    listing's own id/url rather than left for the caller to invent.
    """
    listing_key: str
    source: str
    external_id: str | None = None
    url: str
    title: str | None = None
    price: float | None = None
    currency: str | None = "USD"
    listing_type: str | None = None
    condition: str | None = None


@app.post("/cards/{card_id}/link-listing")
async def link_listing(card_id: int, body: LinkListingBody, user_id: str = Depends(require_session)):
    """Proxies databricks-app's POST /api/cards/{card_id}/link-listing --
    confirms a real eBay listing (from /research/search-ebay) as the card's
    pricing anchor, for cards CardHedger's catalog can't match at all.
    """
    path = f"/api/cards/{card_id}/link-listing"
    resp = await call_app("POST", path, user_id=user_id, json=body.model_dump(exclude_unset=True))
    if resp.status_code >= 400:
        log.error("databricks-app %s failed: %s %s", path, resp.status_code, resp.text)
        raise HTTPException(502, "failed to link listing")
    return resp.json()


class ConfirmIdentityBody(BaseModel):
    """Mirrors databricks-app's server/routes/market.py ConfirmIdentityBody --
    Phase 1C tap-to-confirm of a Phase 1A shadow-resolver candidate.
    `card_ref` is carried along for logging/debugging on the app side even
    though the app re-derives identity from the other three fields, never
    from card_ref or from a client-supplied player name.
    """
    card_ref: str
    release_slug: str
    subset_name: str
    card_number: str


@app.post("/cards/{card_id}/confirm-identity")
async def confirm_identity(card_id: int, body: ConfirmIdentityBody, user_id: str = Depends(require_session)):
    """Proxies databricks-app's POST /api/cards/{card_id}/confirm-identity --
    resolves a `needs_choice` scan by tapping a candidate, rather than the
    free-text "Browse other matches" flow (see CandidatePickerSheet, a
    separate/older feature this doesn't replace).
    """
    path = f"/api/cards/{card_id}/confirm-identity"
    resp = await call_app("POST", path, user_id=user_id, json=body.model_dump())
    if resp.status_code >= 400:
        log.error("databricks-app %s failed: %s %s", path, resp.status_code, resp.text)
        raise HTTPException(502, "failed to confirm card identity")
    return resp.json()


@app.post("/research-cards/{research_card_id}/confirm-identity")
async def confirm_research_identity(research_card_id: int, body: ConfirmIdentityBody, user_id: str = Depends(require_session)):
    """Proxies databricks-app's POST /api/research-cards/{id}/confirm-identity
    -- same tap-to-confirm action for the Research bucket, where
    GuidedCameraCapture's scans land by default.
    """
    path = f"/api/research-cards/{research_card_id}/confirm-identity"
    resp = await call_app("POST", path, user_id=user_id, json=body.model_dump())
    if resp.status_code >= 400:
        log.error("databricks-app %s failed: %s %s", path, resp.status_code, resp.text)
        raise HTTPException(502, "failed to confirm research card identity")
    return resp.json()


class SoldBody(BaseModel):
    """Mirrors databricks-app's SoldIn -- records a real sale price/date and
    flips status to 'sold' in one call. Distinct from PATCH /cards/{id}
    (paid/asking price are cost basis / listing ask, never an implied
    sale), and the only sold-card surface this app had before now was the
    databricks-app route itself -- never proxied to iOS.
    """
    sold_price: float
    date_sold: str | None = None


@app.post("/cards/{card_id}/sold")
async def mark_card_sold(card_id: int, body: SoldBody, user_id: str = Depends(require_session)):
    """Proxies databricks-app's POST /api/cards/{card_id}/sold."""
    path = f"/api/cards/{card_id}/sold"
    resp = await call_app("POST", path, user_id=user_id, json=body.model_dump(exclude_unset=True))
    if resp.status_code >= 400:
        log.error("databricks-app %s failed: %s %s", path, resp.status_code, resp.text)
        raise HTTPException(502, "failed to mark card sold")
    return resp.json()


class CardIn(BaseModel):
    """Mirrors databricks-app's server/routes/cards.py CardIn -- only `name`
    is required, everything else has a server-side default or is optional.
    """
    name: str
    grade: str = "Raw"
    parallel: str | None = None
    player: str | None = None
    card_number: str | None = None
    sport: str | None = None
    acquisition_type: str = "purchased"
    paid_price: float | None = None
    asking_price: float | None = None
    date_acquired: str | None = None
    notes: str | None = None
    quantity: int = 1


@app.post("/cards")
async def create_card(body: CardIn, user_id: str = Depends(require_session)):
    """Proxies databricks-app's POST /api/cards, scoped to the caller's user_id
    the same way list_cards is -- see server/db.py's resolve_user_id().
    """
    resp = await call_app("POST", "/api/cards", user_id=user_id, json=body.model_dump(exclude_unset=True))
    if resp.status_code >= 400:
        log.error("databricks-app POST /api/cards failed: %s %s", resp.status_code, resp.text)
        raise HTTPException(502, "failed to create card")
    return resp.json()


@app.post("/cards/intake")
async def intake_card(
    image: UploadFile = File(...),
    image_back: UploadFile | None = File(None),
    acquisition_type: str = Form("purchased"),
    paid_price: float | None = Form(None),
    asking_price: float | None = Form(None),
    acquisition_venue: str | None = Form(None),
    ebay_shipping_cost: float | None = Form(None),
    ebay_fees: float | None = Form(None),
    user_id: str = Depends(require_session),
):
    """Proxies databricks-app's POST /api/cards/intake: front (+ optional back)
    photo -> vision identify -> pricing -> INSERT -> saved card. Longer
    timeout than the other proxies since this chains a vision call and a
    live eBay pricing lookup on databricks-app's side, not just a DB round trip.
    """
    files = {"image": (image.filename or "front.jpg", await image.read(), image.content_type or "image/jpeg")}
    if image_back is not None:
        files["image_back"] = (image_back.filename or "back.jpg", await image_back.read(), image_back.content_type or "image/jpeg")
    data = {"acquisition_type": acquisition_type}
    if paid_price is not None:
        data["paid_price"] = str(paid_price)
    if asking_price is not None:
        data["asking_price"] = str(asking_price)
    if acquisition_venue is not None:
        data["acquisition_venue"] = acquisition_venue
    if ebay_shipping_cost is not None:
        data["ebay_shipping_cost"] = str(ebay_shipping_cost)
    if ebay_fees is not None:
        data["ebay_fees"] = str(ebay_fees)

    resp = await call_app("POST", "/api/cards/intake", user_id=user_id, files=files, data=data, timeout=60)
    if resp.status_code >= 400:
        log.error("databricks-app POST /api/cards/intake failed: %s %s", resp.status_code, resp.text)
        raise HTTPException(502, "failed to process card intake")
    return resp.json()


@app.post("/cards/identify-front")
async def identify_front(
    image: UploadFile = File(...),
    user_id: str = Depends(require_session),
):
    """Proxies databricks-app's POST /api/cards/identify-front: front-photo-only
    vision preview, no DB write, no pricing -- lets the guided capture flow
    show a player/title guess while the user photographs the back (and the
    next card), well before the real /cards/intake commit. Single file, no
    form fields, so this is the simplest multipart proxy in this file --
    mirrors intake_card's file-building above minus everything intake-only.
    """
    files = {"image": (image.filename or "front.jpg", await image.read(), image.content_type or "image/jpeg")}
    resp = await call_app("POST", "/api/cards/identify-front", user_id=user_id, files=files)
    if resp.status_code >= 400:
        log.error("databricks-app POST /api/cards/identify-front failed: %s %s", resp.status_code, resp.text)
        raise HTTPException(502, "failed to identify card")
    return resp.json()


async def _proxy_image(path: str, user_id: str | None, *, not_found_detail: str) -> Response:
    """Shared plumbing for the card-image proxy routes below -- streams raw
    bytes through rather than decoding as JSON like list_cards does, since
    databricks-app returns these as a binary Response, not a JSON body.
    """
    resp = await call_app("GET", path, user_id=user_id)
    if resp.status_code == 404:
        raise HTTPException(404, not_found_detail)
    if resp.status_code >= 400:
        log.error("databricks-app %s failed: %s %s", path, resp.status_code, resp.text)
        raise HTTPException(502, "failed to fetch image")
    return Response(
        content=resp.content,
        media_type=resp.headers.get("content-type"),
        headers={"Cache-Control": "max-age=3600"},
    )


@app.get("/cards/{card_id}/image")
async def card_image(card_id: int, user_id: str = Depends(require_session)) -> Response:
    """Proxies databricks-app's GET /api/cards/{card_id}/image (the card's front photo)."""
    return await _proxy_image(f"/api/cards/{card_id}/image", user_id, not_found_detail="no image")


@app.get("/cards/{card_id}/image-back")
async def card_image_back(card_id: int, user_id: str = Depends(require_session)) -> Response:
    """Proxies databricks-app's GET /api/cards/{card_id}/image-back (the card's back photo)."""
    return await _proxy_image(f"/api/cards/{card_id}/image-back", user_id, not_found_detail="no back image")


@app.post("/cards/intake-research")
async def intake_research_card(
    image: UploadFile = File(...),
    image_back: UploadFile | None = File(None),
    user_id: str = Depends(require_session),
):
    """Proxies databricks-app's POST /api/cards/intake-research -- same
    identify+price pipeline as intake_card above, but lands in the Research
    bucket instead of Inventory (the research-bucket scan path). No acquisition fields at all -- those only
    get captured later, at promote time. Same generous timeout as
    intake_card since this also chains a vision call + live eBay pricing.
    """
    files = {"image": (image.filename or "front.jpg", await image.read(), image.content_type or "image/jpeg")}
    if image_back is not None:
        files["image_back"] = (image_back.filename or "back.jpg", await image_back.read(), image_back.content_type or "image/jpeg")

    resp = await call_app("POST", "/api/cards/intake-research", user_id=user_id, files=files, timeout=60)
    if resp.status_code >= 400:
        log.error("databricks-app POST /api/cards/intake-research failed: %s %s", resp.status_code, resp.text)
        raise HTTPException(502, "failed to process research card intake")
    return resp.json()


@app.get("/research-cards")
async def list_research_cards(user_id: str = Depends(require_session)):
    """Proxies databricks-app's GET /api/research-cards."""
    resp = await call_app("GET", "/api/research-cards", user_id=user_id)
    if resp.status_code >= 400:
        log.error("databricks-app GET /api/research-cards failed: %s %s", resp.status_code, resp.text)
        raise HTTPException(502, "failed to fetch research cards")
    return resp.json()


@app.get("/research-cards/{research_card_id}")
async def get_research_card(research_card_id: int, user_id: str = Depends(require_session)):
    """Proxies databricks-app's GET /api/research-cards/{id}."""
    path = f"/api/research-cards/{research_card_id}"
    resp = await call_app("GET", path, user_id=user_id)
    if resp.status_code >= 400:
        log.error("databricks-app %s failed: %s %s", path, resp.status_code, resp.text)
        raise HTTPException(502, "failed to fetch research card")
    return resp.json()


@app.delete("/research-cards/{research_card_id}")
async def delete_research_card(research_card_id: int, user_id: str = Depends(require_session)):
    """Proxies databricks-app's DELETE /api/research-cards/{id} -- "Discard"."""
    path = f"/api/research-cards/{research_card_id}"
    resp = await call_app("DELETE", path, user_id=user_id)
    if resp.status_code >= 400:
        log.error("databricks-app %s failed: %s %s", path, resp.status_code, resp.text)
        raise HTTPException(502, "failed to delete research card")
    return resp.json()


@app.get("/research-cards/{research_card_id}/image")
async def research_card_image(research_card_id: int, user_id: str = Depends(require_session)) -> Response:
    """Proxies databricks-app's GET /api/research-cards/{id}/image."""
    return await _proxy_image(f"/api/research-cards/{research_card_id}/image", user_id, not_found_detail="no image")


@app.get("/research-cards/{research_card_id}/image-back")
async def research_card_image_back(research_card_id: int, user_id: str = Depends(require_session)) -> Response:
    """Proxies databricks-app's GET /api/research-cards/{id}/image-back."""
    return await _proxy_image(f"/api/research-cards/{research_card_id}/image-back", user_id, not_found_detail="no back image")


class PromoteResearchCardBody(BaseModel):
    """Mirrors databricks-app's server/routes/research_cards.py PromoteIn --
    the acquisition details a research card never captures at scan time
    (it's typically unbought until this moment)."""
    acquisition_type: str = "purchased"
    paid_price: float | None = None
    asking_price: float | None = None
    acquisition_venue: str | None = None
    ebay_shipping_cost: float | None = None
    ebay_fees: float | None = None
    date_acquired: str | None = None


@app.post("/research-cards/{research_card_id}/promote")
async def promote_research_card(research_card_id: int, body: PromoteResearchCardBody, user_id: str = Depends(require_session)):
    """Proxies databricks-app's POST /api/research-cards/{id}/promote -- turns
    a research card into a real inventory card, returning the new card."""
    path = f"/api/research-cards/{research_card_id}/promote"
    resp = await call_app("POST", path, user_id=user_id, json=body.model_dump(exclude_unset=True))
    if resp.status_code >= 400:
        log.error("databricks-app %s failed: %s %s", path, resp.status_code, resp.text)
        raise HTTPException(502, "failed to promote research card")
    return resp.json()


@app.get("/players/{mlb_person_id}/headshot")
async def player_headshot(mlb_person_id: int, _user_id: str = Depends(require_session)) -> Response:
    """Proxies databricks-app's GET /api/players/{id}/headshot -- shared/global
    reference data (no RLS, no X-Cardshop-User-Id needed), but still gated
    behind a valid app session so it's not a fully open public endpoint.
    """
    return await _proxy_image(f"/api/players/{mlb_person_id}/headshot", None, not_found_detail="no headshot")


@app.get("/cfb-players/{cfbd_athlete_id}/headshot")
async def cfb_player_headshot(cfbd_athlete_id: int, _user_id: str = Depends(require_session)) -> Response:
    """Proxies databricks-app's GET /api/cfb-players/{id}/headshot -- same
    shared/global-reference-data shape as player_headshot above."""
    return await _proxy_image(f"/api/cfb-players/{cfbd_athlete_id}/headshot", None, not_found_detail="no headshot")


@app.get("/nfl-players/{nfl_gsis_id}/headshot")
async def nfl_player_headshot(nfl_gsis_id: str, _user_id: str = Depends(require_session)) -> Response:
    """Proxies databricks-app's GET /api/nfl-players/{gsis_id}/headshot -- same
    shared/global-reference-data shape as player_headshot above."""
    return await _proxy_image(f"/api/nfl-players/{nfl_gsis_id}/headshot", None, not_found_detail="no headshot")


class TrendsWatchIn(BaseModel):
    player_name: str


@app.get("/players/{mlb_person_id}/trends-watch")
async def get_player_trends_watch(mlb_person_id: int, user_id: str = Depends(require_session)):
    """Proxies databricks-app's GET /api/players/{id}/trends-watch -- whether
    the current user has opted this player into the daily Trends pull.
    """
    path = f"/api/players/{mlb_person_id}/trends-watch"
    resp = await call_app("GET", path, user_id=user_id)
    if resp.status_code >= 400:
        log.error("databricks-app %s failed: %s %s", path, resp.status_code, resp.text)
        raise HTTPException(502, "failed to fetch trends-watch status")
    return resp.json()


@app.post("/players/{mlb_person_id}/trends-watch")
async def watch_player_trends(mlb_person_id: int, body: TrendsWatchIn, user_id: str = Depends(require_session)):
    """Proxies databricks-app's POST /api/players/{id}/trends-watch -- opts a
    player into the daily Trends pull even without an owned card.
    """
    path = f"/api/players/{mlb_person_id}/trends-watch"
    resp = await call_app("POST", path, user_id=user_id, json=body.model_dump())
    if resp.status_code >= 400:
        log.error("databricks-app %s failed: %s %s", path, resp.status_code, resp.text)
        raise HTTPException(502, "failed to watch player")
    return resp.json()


@app.delete("/players/{mlb_person_id}/trends-watch")
async def unwatch_player_trends(mlb_person_id: int, user_id: str = Depends(require_session)):
    """Proxies databricks-app's DELETE /api/players/{id}/trends-watch."""
    path = f"/api/players/{mlb_person_id}/trends-watch"
    resp = await call_app("DELETE", path, user_id=user_id)
    if resp.status_code >= 400:
        log.error("databricks-app %s failed: %s %s", path, resp.status_code, resp.text)
        raise HTTPException(502, "failed to unwatch player")
    return resp.json()
