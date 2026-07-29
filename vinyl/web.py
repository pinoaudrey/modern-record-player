"""Web admin: register cards, browse the collection, no more hand-edited songMap."""

from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .db import CONTROL_ACTIONS, Database
from .links import parse_ref
from .player import Player
from .reader import FakeReader
from .spotify import SpotifyClient

TEMPLATES_DIR = Path(__file__).parent / "templates"


def create_app(db: Database, spotify: SpotifyClient, player: Player, reader=None) -> FastAPI:
    app = FastAPI(title="Modern Record Player")
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "cards": db.list_cards(),
                "pending": player.pending_scan,
                "actions": sorted(CONTROL_ACTIONS),
            },
        )

    @app.get("/register", response_class=HTMLResponse)
    def register_form(request: Request, uid: str = ""):
        pending = player.pending_scan
        return templates.TemplateResponse(
            request,
            "register.html",
            {
                "uid": uid or (pending.uid if pending else ""),
                "actions": sorted(CONTROL_ACTIONS),
                "result": None,
                "error": None,
            },
        )

    @app.post("/register/resolve", response_class=HTMLResponse)
    def register_resolve(request: Request, uid: str = Form(...), link: str = Form(...)):
        ref = parse_ref(link)
        error = None
        result = None
        if ref is None:
            error = "Could not find a Spotify track/album/playlist/artist in that link."
        else:
            try:
                result = spotify.resolve(ref)
            except Exception as e:
                error = f"Spotify lookup failed: {e}"
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

    @app.post("/register/save")
    def register_save(
        uid: str = Form(...),
        uri: str = Form(...),
        content_type: str = Form(...),
        name: str = Form(...),
        artist: str = Form(""),
        artwork_url: str = Form(""),
    ):
        db.save_content_card(
            uid=uid.strip(),
            uri=uri,
            content_type=content_type,
            name=name,
            artist=artist or None,
            artwork_url=artwork_url or None,
        )
        player.clear_pending(uid.strip())
        return RedirectResponse("/", status_code=303)

    @app.post("/register/control")
    def register_control(uid: str = Form(...), action: str = Form(...)):
        db.save_control_card(uid.strip(), action)
        player.clear_pending(uid.strip())
        return RedirectResponse("/", status_code=303)

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

    @app.get("/api/pending")
    def api_pending():
        pending = player.pending_scan
        return {"uid": pending.uid if pending else None}

    @app.get("/health")
    def health():
        return {"ok": True}

    if isinstance(reader, FakeReader):

        @app.post("/dev/scan")
        def dev_scan(uid: str = Form(...)):
            reader.inject(uid)
            return {"injected": uid}

    return app
