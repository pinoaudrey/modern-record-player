"""Web admin: register cards, browse the collection, make records of what's playing."""

import logging
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import updates
from .db import CONTROL_ACTIONS, CardOptions, Database
from .history import DEFAULT_WINDOW, WINDOWS, HistoryPoller, build_report
from .links import parse_ref
from .player import Player
from .reader import FakeReader
from .spotify import NotAuthorized, ResolvedContent, SpotifyClient
from .status import Health

log = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"


def localtime(iso: str | None, fmt: str = "%b %d, %H:%M") -> str:
    """Render an ISO UTC timestamp (Spotify's or ours) in the Pi's local time."""
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().strftime(fmt)


def create_app(
    db: Database, spotify: SpotifyClient, player: Player, reader=None,
    poller: HistoryPoller | None = None, health: Health | None = None,
) -> FastAPI:
    app = FastAPI(title="Modern Record Player")
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters["localtime"] = localtime

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

    def options_from_form(single: str, resume: str, shuffle: str) -> CardOptions:
        """Checkboxes post "1" when ticked and nothing when not; the shuffle
        select is "" (leave alone), "on" or "off"."""
        return CardOptions(
            single=bool(single), resume=bool(resume),
            shuffle={"on": True, "off": False}.get(shuffle),
        )

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        now, now_error = now_playing()
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "cards": db.list_cards(),
                "positions": db.list_positions(),
                "pending": player.pending_scan,
                "write": player.pending_write,
                "last_write": player.last_write,
                "now": now,
                "now_error": now_error,
                "stamp": status()["stamp"],
                "actions": sorted(CONTROL_ACTIONS),
            },
        )

    @app.get("/records", response_class=HTMLResponse)
    def records(request: Request, window: str = DEFAULT_WINDOW):
        report = build_report(db, spotify, window)
        return templates.TemplateResponse(
            request,
            "records.html",
            {
                "report": report,
                "windows": WINDOWS,
                "recent": db.recent_plays(15),
                "total_plays": db.play_count(),
                "last_poll": poller.last_poll_at if poller else None,
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
                "options": CardOptions(),
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
            error = "This player isn't connected to Spotify yet. Use the Connect Spotify page first."
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
        single: str = Form(""),
        resume: str = Form(""),
        shuffle: str = Form(""),
    ):
        uid = uid.strip()
        content = content_from_form(uri, content_type, name, artist, artwork_url)
        options = options_from_form(single, resume, shuffle)
        if uid:
            db.save_content_card(
                uid=uid, uri=content.uri, content_type=content.content_type, name=content.name,
                artist=content.artist, artwork_url=content.artwork_url, options=options,
            )
            player.clear_pending(uid)
        elif not write:
            raise HTTPException(400, "A card UID is required unless writing to a card")
        if write:
            player.arm_write(content, options=options)
        return RedirectResponse("/", status_code=303)

    @app.post("/register/control")
    def register_control(uid: str = Form(...), action: str = Form(...)):
        db.save_control_card(uid.strip(), action)
        player.clear_pending(uid.strip())
        return RedirectResponse("/", status_code=303)

    # --- spotify login ------------------------------------------------------

    def render_auth(request, error=None):
        authorized = bool(getattr(spotify, "authorized", True))
        return templates.TemplateResponse(
            request,
            "auth.html",
            {
                "authorized": authorized,
                "url": spotify.authorize_url(),
                "error": error,
            },
        )

    @app.get("/auth", response_class=HTMLResponse)
    def auth_form(request: Request):
        return render_auth(request)

    @app.post("/auth", response_class=HTMLResponse)
    def auth_complete(request: Request, redirect_url: str = Form(...)):
        try:
            spotify.complete_authorization(redirect_url)
        except Exception as e:
            log.warning("Spotify authorization failed: %s", e)
            return render_auth(request, error=f"That didn't work: {e}")
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
        player.play_card(card)
        return RedirectResponse("/", status_code=303)

    @app.post("/cards/{uid}/restart")
    def restart_card(uid: str):
        """Play from the top, forgetting any saved resume position."""
        card = db.get_card(uid)
        if card is None or card.kind != "content":
            raise HTTPException(404)
        player.play_card(card, from_top=True)
        return RedirectResponse("/", status_code=303)

    @app.post("/cards/{uid}/options")
    def card_options(
        uid: str, single: str = Form(""), resume: str = Form(""), shuffle: str = Form(""),
    ):
        card = db.get_card(uid)
        if card is None or card.kind != "content":
            raise HTTPException(404)
        db.set_card_options(uid, options_from_form(single, resume, shuffle))
        return RedirectResponse("/", status_code=303)

    # --- records: stacking, pressings, surprise me (vinyl/player.py, vinyl/db.py) ---
    # Kept in one block, separate from the card routes above.

    templates.env.globals["pressing_for"] = db.get_pressing

    def content_card(uid: str):
        card = db.get_card(uid)
        if card is None or card.kind != "content":
            raise HTTPException(404)
        return card

    @app.post("/cards/{uid}/queue")
    def queue_card(uid: str):
        """Stack this record behind whatever is playing."""
        player.queue_card(content_card(uid))
        return RedirectResponse("/", status_code=303)

    @app.post("/cards/{uid}/press")
    def press_card(uid: str):
        """Freeze a playlist card's track list as it is right now (re-pressing
        replaces an earlier pressing). The tag still holds the live playlist."""
        card = content_card(uid)
        if card.content_type != "playlist":
            raise HTTPException(400, "Only playlist cards can be pressed")
        try:
            uris = spotify.content_tracks(card.uri, limit=200)
        except NotAuthorized:
            raise HTTPException(409, "Spotify isn't connected yet")
        except Exception as e:
            log.warning("Could not press %s: %s", card.name, e)
            raise HTTPException(502, f"Couldn't fetch the playlist's tracks: {e}")
        if not uris:
            raise HTTPException(400, "That playlist has no playable tracks to press")
        db.set_pressing(uid, uris)
        log.info("Pressed %s: %d tracks", card.name, len(uris))
        return RedirectResponse("/", status_code=303)

    @app.post("/cards/{uid}/unpress")
    def unpress_card(uid: str):
        content_card(uid)
        db.clear_pressing(uid)
        return RedirectResponse("/", status_code=303)

    @app.post("/shelf/random")
    def shelf_random(next: str = Form("/")):
        """Surprise me: play a record off the shelf, the dustier the likelier."""
        player.play_random()
        local = next.startswith("/") and not next.startswith("//")   # same-site paths only
        return RedirectResponse(next if local else "/", status_code=303)

    # --- api ----------------------------------------------------------------

    @app.get("/api/status")
    def api_status():
        return status()

    @app.get("/health")
    def health_ping():
        return {"ok": True}

    # --- status page, self-update, reboot (vinyl/status.py, vinyl/updates.py) ---
    # Kept in one block, separate from the player routes above.

    if health is None:
        health = Health(
            db, spotify, reader, poller, repo=TEMPLATES_DIR.parent.parent,
            device_name=getattr(spotify, "device_name", "") or "",
        )

    def render_status(request, result=None, notice=None, error=None, restarting=False, refresh=False):
        return templates.TemplateResponse(
            request,
            "status.html",
            {
                "health": health.collect(refresh_updates=refresh),
                "result": result,
                "notice": notice,
                "error": error,
                "restarting": restarting,
                "restart_cmd": " ".join(updates.restart_command()[2:]),
            },
        )

    @app.get("/status", response_class=HTMLResponse)
    def status_page(request: Request, refresh: str = ""):
        return render_status(request, refresh=bool(refresh))

    @app.get("/api/health")
    def api_health():
        return health.collect()

    @app.post("/update", response_class=HTMLResponse)
    def update_now(request: Request):
        try:
            result = updates.apply(health.repo, restart=False)
        except updates.UpdateError as e:
            log.warning("Update failed: %s", e)
            return render_status(request, error=f"Update failed: {e}")
        health.invalidate_updates()
        restarting = False
        if result.updated:
            # The restart kills this process, so answer first and restart a
            # second later. Ask sudo whether it's allowed instead of finding
            # out from a dead page.
            if updates.can_sudo(updates.restart_command()):
                threading.Timer(1.0, updates.restart_service).start()
                result.restarted = restarting = True
            else:
                result.warning = (
                    "The code is updated, but this user may not restart the service without a "
                    "password (re-run deploy/install-pi.sh to install the sudo rule). Restart it "
                    f"manually: sudo {' '.join(updates.restart_command()[2:])}"
                )
        return render_status(request, result=result, restarting=restarting)

    @app.post("/reboot", response_class=HTMLResponse)
    def reboot(request: Request):
        if not updates.can_sudo(updates.reboot_command()):
            return render_status(
                request,
                error="This user may not reboot without a password (re-run deploy/install-pi.sh "
                      "to install the sudo rule). Over ssh: sudo reboot",
            )
        threading.Timer(1.0, subprocess.run, args=(updates.reboot_command(),)).start()
        return render_status(request, notice="Rebooting. This page reloads once the player is back.",
                             restarting=True)

    if isinstance(reader, FakeReader):

        @app.post("/dev/scan")
        def dev_scan(uid: str = Form(...), text: str = Form(""), hold: str = Form("")):
            if hold:
                reader.hold(uid, text or None)
                return {"held": uid, "text": reader.tag_text(uid)}
            reader.inject(uid, text or None)
            return {"injected": uid, "text": reader.tag_text(uid)}

        @app.post("/dev/release")
        def dev_release():
            held = reader.held
            reader.release()
            return {"released": held}

    return app
