import secrets
import time

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect, status

from jarvis_hub.api.auth import require_source, source_for_token
from jarvis_hub.config import Source, get_settings

router = APIRouter()
TICKET_TTL_S = 60
_tickets: dict[str, tuple[Source, float]] = {}


@router.post("/v1/events/ticket")
async def ticket(source: Source = Depends(require_source)) -> dict:
    """One-time, short-lived ticket so a browser can open the event stream
    without ever holding the long-lived token (the UI server fetches this)."""
    now = time.time()
    for k, (_, exp) in list(_tickets.items()):
        if exp < now:
            _tickets.pop(k, None)
    t = secrets.token_urlsafe(24)
    _tickets[t] = (source, now + TICKET_TTL_S)
    return {"ticket": t, "expires_in": TICKET_TTL_S}


def _redeem(msg: dict) -> Source | None:
    if t := msg.get("ticket"):
        source, exp = _tickets.pop(str(t), (None, 0.0))
        return source if exp >= time.time() else None
    return source_for_token(str(msg.get("token", "")), get_settings())


@router.websocket("/v1/events")
async def events(ws: WebSocket) -> None:
    """Event stream for one source. Browsers can't set headers on WebSockets,
    so the credential arrives as the first message rather than in the URL."""
    await ws.accept()
    try:
        source = _redeem(await ws.receive_json())
    except Exception:
        source = None
    if source is None:
        await ws.close(status.WS_1008_POLICY_VIOLATION)
        return

    bus = ws.app.state.bus
    queue = bus.subscribe(source)
    try:
        await ws.send_json({"type": "subscribed", "source": source})
        while True:
            await ws.send_json((await queue.get()).to_dict())
    except WebSocketDisconnect:
        pass
    finally:
        bus.unsubscribe(source, queue)
