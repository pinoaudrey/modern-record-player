"""Web admin: register cards, browse the collection, make records of what's playing."""

import logging
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .db import CONTROL_ACTIONS, Database
from .links import parse_ref
from .player import Player
from .reader import FakeReader
from .spotify import NotAuthorized, ResolvedContent, SpotifyClient

log = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"


def create_app(db: Database, spotify: SpotifyClient, player: Player, reader=None) -> FastAPI:
    app = FastAPI(title="Modern Record Player")
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    def status() -> dict:
        pending = player.pending_scan
        write = player.pending_write
        last = player.last_write
        return {
            "pending_uid": pending.uid if pending else None,
            "write": write.content.name if write else None,
            "last_write": (
                {"uid": last.uid, "ok": last.ok, "error": last.error, "name": last.content.name}
                if last else None
            ),
            "stamp": "|".join([
                pending.uid if pending else "",
                write.content.uri if write else "",
                f"{last.at:.3f}" if last else "",
                str(player.generation),
            ]),
        }

    def now_playing() -> tuple[ResolvedContent | None, str | None]:
        """(content, error) with the error being 'not_authorized' or a message."""
        try:
            if not getattr(spotify, "authorized", True):
                return None, "not_authorized"
            return spotify.now_playing_content(), None
        except NotAuthorized:
            return None, "not_authorized"
        except Exception as e:
            log.warning("Now-playing lookup failed: %s", e)
            return None, str(e)

    def content_from_form(uri, content_type, name, artist, artwork_url) -> ResolvedContent:
        return ResolvedContent(
            uri=uri, content_type=content_type, name=name,
            artist=artist or None, artwork_url=artwork_url or None,
        )

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        now, now_error = now_playing()
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "cards": db.list_cards(),
                "pending": player.pending_scan,
                "write": player.pending_write,
                "last_write": player.last_write,
                "now": now,
                "now_error": now_error,
                "stamp": status()["stamp"],
                "actions": sorted(CONTROL_ACTIONS),
            },
        )

    # --- registration -------------------------------------------------------

    def render_register(request, uid, result=None, error=None, link=""):
        return templates.TemplateResponse(
            request,
            "register.html",
            {
                "uid": uid,
                "actions": sorted(CONTROL_ACTIONS),
                "result": result,
                "error": error,
                "link": link,
            },
        )

    @app.get("/register", response_class=HTMLResponse)
    def register_form(request: Request, uid: str = ""):
        pending = player.pending_scan
        return render_register(request, uid or (pending.uid if pending else ""))

    @app.post("/register/resolve", response_class=HTMLResponse)
    def register_resolve(request: Request, uid: str = Form(""), link: str = Form(...)):
        ref = parse_ref(link)
        error = result = None
        if ref is None:
            error = "Could not find a Spotify track/album/playlist/artist in that link."
        else:
            try:
                result = spotify.resolve(ref)
            except Exception as e:
                error = f"Spotify lookup failed: {e}"
        return render_register(request, uid, result, error, link)

    @app.post("/register/now", response_class=HTMLResponse)
    def register_now(request: Request, uid: str = Form("")):
        now, now_error = now_playing()
        error = None
        if now_error == "not_authorized":
            error = "Spotify isn't authorized on this player yet. Run: python -m vinyl auth"
        elif now_error:
            error = f"Couldn't reach Spotify: {now_error}"
        elif now is None:
            error = "Nothing is playing right now. Start something in the Spotify app first."
        return render_register(request, uid, now, error)

    @app.post("/register/save")
    def register_save(
        uid: str = Form(""),
        uri: str = Form(...),
        content_type: str = Form(...),
        name: str = Form(...),
        artist: str = Form(""),
        artwork_url: str = Form(""),
        write: str = Form(""),
    ):
        uid = uid.strip()
        content = content_from_form(uri, content_type, name, artist, artwork_url)
        if uid:
            db.save_content_card(
                uid=uid, uri=content.uri, content_type=content.content_type, name=content.name,
                artist=content.artist, artwork_url=content.artwork_url,
            )
            player.clear_pending(uid)
        elif not write:
            raise HTTPException(400, "A card UID is required unless writing to a card")
        if write:
            player.arm_write(content)
        return RedirectResponse("/", status_code=303)

    @app.post("/register/control")
    def register_control(uid: str = Form(...), action: str = Form(...)):
        db.save_control_card(uid.strip(), action)
        player.clear_pending(uid.strip())
        return RedirectResponse("/", status_code=303)

    # --- writing URIs onto cards --------------------------------------------

    @app.post("/write/arm")
    def write_arm(
        uri: str = Form(...),
        content_type: str = Form(...),
        name: str = Form(...),
        artist: str = Form(""),
        artwork_url: str = Form(""),
    ):
        player.arm_write(content_from_form(uri, content_type, name, artist, artwork_url))
        return RedirectResponse("/", status_code=303)

    @app.post("/write/cancel")
    def write_cancel():
        player.cancel_write()
        return RedirectResponse("/", status_code=303)

    @app.post("/write/dismiss")
    def write_dismiss():
        player.clear_last_write()
        return RedirectResponse("/", status_code=303)

    @app.post("/cards/{uid}/write")
    def write_card(uid: str):
        card = db.get_card(uid)
        if card is None or card.kind != "content":
            raise HTTPException(404)
        player.arm_write(
            content_from_form(card.uri, card.content_type, card.name, card.artist, card.artwork_url)
        )
        return RedirectResponse("/", status_code=303)

    # --- cards --------------------------------------------------------------

    @app.post("/cards/{uid}/delete")
    def delete_card(uid: str):
        db.delete_card(uid)
        return RedirectResponse("/", status_code=303)

    @app.post("/cards/{uid}/play")
    def play_card(uid: str):
        card = db.get_card(uid)
        if card is None or card.kind != "content":
            raise HTTPException(404)
        spotify.play(card.uri)
        db.record_play(uid)
        return RedirectResponse("/", status_code=303)

    # --- api ----------------------------------------------------------------

    @app.get("/api/status")
    def api_status():
        return status()

    @app.get("/health")
    def health():
        return {"ok": True}

    if isinstance(reader, FakeReader):

        @app.post("/dev/scan")
        def dev_scan(uid: str = Form(...), text: str = Form("")):
            reader.inject(uid, text or None)
            return {"injected": uid, "text": reader.tag_text(uid)}

    return app
