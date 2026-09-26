"""Local review UI (DESIGN.md §6). Runs on 127.0.0.1 only; never exposed to the network."""

import os
import secrets
import sqlite3
from collections import defaultdict
from datetime import date as calendar_date
from datetime import datetime
from pathlib import Path

from flask import Flask, abort, flash, g, redirect, render_template, request, send_file, url_for
from PIL import Image, ImageOps

from .. import actions
from .. import export as export_mod
from .. import events as events_mod
from .. import faces as faces_mod
from .. import videos as videos_mod
from .. import blog as blog_mod
from ..config import DEFAULT_CONFIG_PATH, Config
from ..dates import UNCERTAIN, sql_in
from ..db import connect
from ..winpath import windows_path

THUMB_SIZES = {320, 1280}
LOCAL_HOSTS = {"127.0.0.1", "localhost"}


def create_app(cfg: Config, config_path: Path | None = None) -> Flask:
    app = Flask(__name__)
    config_path = config_path or DEFAULT_CONFIG_PATH
    token = secrets.token_urlsafe(32)
    app.secret_key = token
    # A global, not just template context: macros imported from _macros.html can't see context.
    app.jinja_env.globals["csrf"] = token

    def db() -> sqlite3.Connection:
        if "db" not in g:
            g.db = connect(cfg.db_path)
        return g.db

    @app.teardown_appcontext
    def close_db(_exc):
        if (conn := g.pop("db", None)) is not None:
            conn.close()

    @app.before_request
    def guard():
        # A web page elsewhere can't use this app: the Host check defeats DNS rebinding,
        # and every change needs the per-launch token embedded in our own forms.
        if request.host.split(":")[0] not in LOCAL_HOSTS:
            abort(403)
        if request.method == "POST" and not secrets.compare_digest(request.form.get("csrf", ""), token):
            # Usually a page left open from before `psort review` was restarted.
            return (
                "<p>This page is out of date (psort review was restarted since it loaded). "
                "Go back, reload the page, and try again.</p>",
                403,
            )

    @app.context_processor
    def nav():
        conn = db()
        one = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
        return {
            "counts": {
                "close_calls": one("SELECT COUNT(DISTINCT moment_id) FROM photos WHERE close_call = 1"),
                "undated": one(f"SELECT COUNT(*) FROM photos WHERE date_source IN {sql_in(UNCERTAIN)}"),
                "tray": one("SELECT COUNT(*) FROM tray"),
                "favorites": one("SELECT COUNT(*) FROM favorites"),
                "videos": one("SELECT COUNT(*) FROM videos WHERE library_path IS NOT NULL"),
                "unnamed_groups": one("SELECT COUNT(DISTINCT cluster) FROM faces WHERE cluster IS NOT NULL"),
            },
        }

    def back(default: str):
        """Redirect to the ?next= page if it's one of ours."""
        target = request.form.get("next") or default
        if not target.startswith("/") or target.startswith("//"):
            target = default
        return redirect(target)

    def act(fn, *args, default="/"):
        try:
            fn(*args)
        except (actions.ActionError, events_mod.EventError, faces_mod.FaceError, FileExistsError) as e:
            flash(str(e), "error")
        return back(default)

    # ---- Browsing ----

    @app.get("/")
    def index():
        by_year = defaultdict(list)
        for f in folders(db()):
            by_year[f["year"]].append(f)
        stats = db().execute(
            "SELECT COUNT(*) AS photos, COUNT(DISTINCT moment_id) AS moments FROM photos WHERE library_path IS NOT NULL"
        ).fetchone()
        return render_template("index.html", by_year=dict(sorted(by_year.items(), reverse=True)), stats=stats)

    @app.get("/folder/<path:key>")
    def folder(key):
        info = next((f for f in folders(db()) if f["key"] == key), None)
        if info is None:
            abort(404)
        # Prefix match, not LIKE: folder names contain '_', which LIKE treats as a wildcard.
        prefix = key + "/"
        photos = db().execute(
            f"""SELECT {CARD_COLUMNS} FROM photos p
                WHERE substr(p.library_path, 1, ?) = ? AND instr(p.library_path, '/_alternates/') = 0
                  AND p.is_best = 1
                ORDER BY p.taken_at, p.name""",
            (len(prefix), prefix),
        ).fetchall()
        videos = db().execute(
            "SELECT * FROM videos WHERE substr(library_path, 1, ?) = ? ORDER BY taken_at, name",
            (len(prefix), prefix),
        ).fetchall()
        return render_template("folder.html", info=info, photos=photos, videos=videos)

    @app.get("/moment/<moment_id>")
    def moment(moment_id):
        shots = db().execute(
            f"""SELECT {CARD_COLUMNS}, p.sharpness, p.exposure, p.faces, p.score, p.user_best, p.taken_at,
                       p.date_source, p.camera, p.width, p.height
                FROM photos p WHERE p.moment_id = ? ORDER BY p.is_best DESC, p.score DESC""",
            (moment_id,),
        ).fetchall()
        if not shots:
            abort(404)
        return render_template("moment.html", shots=shots, moment_id=moment_id,
                               folder_key=folder_key(shots[0]["library_path"]))

    @app.get("/close-calls")
    def close_calls():
        rows = db().execute(
            f"""SELECT {CARD_COLUMNS}, p.score, p.moment_id FROM photos p WHERE p.close_call = 1
                ORDER BY p.taken_at"""
        ).fetchall()
        moments = defaultdict(list)
        for r in rows:
            moments[r["moment_id"]].append(r)
        for shots in moments.values():
            shots.sort(key=lambda r: (-r["is_best"], -r["score"]))
        return render_template("close_calls.html", moments=moments)

    @app.get("/videos")
    def videos():
        rows = db().execute("SELECT * FROM videos WHERE library_path IS NOT NULL ORDER BY taken_at, name").fetchall()
        by_folder = defaultdict(list)
        for r in rows:
            by_folder[folder_key(r["library_path"])].append(r)
        return render_template("videos.html", by_folder=dict(sorted(by_folder.items(), reverse=True)),
                               root=windows_path(cfg.videos))

    @app.get("/poster/<sha>.jpg")
    def video_poster(sha):
        row = db().execute("SELECT library_path FROM videos WHERE sha256 = ?", (sha,)).fetchone()
        if row is None or row["library_path"] is None:
            abort(404)
        out = videos_mod.poster(cfg, sha, row["library_path"])
        if out is None:
            abort(404)
        return send_file(out, mimetype="image/jpeg", max_age=86400)

    @app.get("/video/<sha>")
    def video_file(sha):
        row = db().execute("SELECT library_path FROM videos WHERE sha256 = ?", (sha,)).fetchone()
        if row is None or row["library_path"] is None or not (cfg.videos / row["library_path"]).exists():
            abort(404)
        # conditional=True answers Range requests, so the browser can seek.
        return send_file(cfg.videos / row["library_path"], conditional=True, max_age=0)

    @app.get("/undated")
    def undated():
        photos = db().execute(
            f"""SELECT {CARD_COLUMNS}, p.taken_at, p.date_source FROM photos p
                WHERE p.date_source IN {sql_in(UNCERTAIN)} ORDER BY p.taken_at, p.name"""
        ).fetchall()
        return render_template("undated.html", photos=photos)

    def blog_settings():
        try:
            return blog_mod.settings(config_path)
        except blog_mod.BlogError:
            return None

    @app.get("/tray")
    def tray():
        photos = db().execute(
            f"""SELECT {CARD_COLUMNS}, p.taken_at FROM photos p JOIN tray t ON t.sha256 = p.sha256
                ORDER BY t.position, p.taken_at, p.name"""
        ).fetchall()
        posts = db().execute(
            "SELECT post, COUNT(*) AS photos, MAX(exported_at) AS last FROM exports GROUP BY post ORDER BY last DESC LIMIT 10"
        ).fetchall()
        s = blog_settings()
        draft = blog_mod.load_draft(db())
        if s and not draft.author:
            draft.author = s.default_author
        return render_template("tray.html", photos=photos, posts=posts, outbox=windows_path(cfg.outbox),
                               draft=draft, blog=s, choices=blog_mod.blog_choices(s) if s else None,
                               first_date=photos[0]["taken_at"][:10] if photos else "")

    @app.post("/tray/<sha>/move")
    def tray_move(sha):
        return act(actions.move_in_tray, db(), sha, request.form.get("step", 0, type=int), default="/tray")

    @app.post("/tray/compose")
    def tray_compose():
        f = request.form
        shas = [r["sha256"] for r in db().execute("SELECT sha256 FROM tray")]
        draft = blog_mod.Draft(
            **{k: f.get(k, "") for k in ("title", "author", "categories", "tags", "top_text", "youtube_id",
                                         "bottom_text", "quote", "quote_attribution", "existing_post", "post_date")},
            captions={sha: {"text_before": f.get(f"text_before_{sha}", ""), "alt": f.get(f"alt_{sha}", "")}
                      for sha in shas},
        )
        blog_mod.save_draft(db(), draft)
        action = f.get("action", "save")
        if action == "save":
            flash("Draft saved.", "ok")
            return redirect(url_for("tray"))
        s = blog_settings()
        if s is None:
            flash("Publishing isn't set up yet: run `psort blog-login` in a terminal first.", "error")
            return redirect(url_for("tray"))
        try:
            photos = blog_mod.tray_photos(db())
            filename = blog_mod.post_filename(draft, photos)
            if action == "preview":
                existing = (s.posts_dir / filename).read_text() if draft.existing_post and (s.posts_dir / filename).exists() else None
                text = blog_mod.render_post(draft, photos, existing)
                return render_template("publish.html", mode="preview", filename=filename, text=text,
                                       url=blog_mod.post_url(s, filename, text), log=[])
            result = blog_mod.publish(cfg, db(), s, dry_run=(action == "dryrun"))
        except (blog_mod.BlogError, FileExistsError, OSError) as e:
            flash(str(e), "error")
            return redirect(url_for("tray"))
        return render_template("publish.html", mode=action, filename=result.filename, text=result.text,
                               url=result.url, log=result.log, staged=windows_path(result.staged))

    # ---- Decisions ----

    @app.post("/tray/export")
    def tray_export():
        try:
            result = export_mod.export(cfg, db(), request.form.get("post", ""))
        except (export_mod.ExportError, events_mod.EventError) as e:
            flash(str(e), "error")
            return redirect(url_for("tray"))
        flash(f"Exported {len(result.files)} photo(s) to {windows_path(result.folder)}. "
              "Upload them from there with blogupdate.html.", "ok")
        return redirect(url_for("tray"))

    @app.post("/photo/<sha>/best")
    def best(sha):
        return act(actions.pick_best, cfg, db(), sha)

    @app.post("/moment/<moment_id>/auto")
    def auto(moment_id):
        return act(actions.clear_pick, cfg, db(), moment_id)

    @app.post("/photo/<sha>/favorite")
    def favorite(sha):
        return act(actions.toggle_favorite, cfg, db(), sha)

    @app.get("/favorites")
    def favorites():
        year = request.args.get("year", "")
        person = request.args.get("person", "")
        rows = db().execute(
            f"""SELECT {CARD_COLUMNS}, p.taken_at, h.path AS highlight FROM photos p
                JOIN favorites fav ON fav.sha256 = p.sha256
                LEFT JOIN highlights h ON h.sha256 = p.sha256
                WHERE (? = '' OR substr(p.taken_at, 1, 4) = ?)
                  AND (? = '' OR EXISTS (SELECT 1 FROM faces f JOIN people pe ON pe.id = f.person_id
                                         WHERE f.sha256 = p.sha256 AND pe.name = ?))
                ORDER BY p.taken_at DESC, p.name""",
            (year, year, person, person),
        ).fetchall()
        years = [r[0] for r in db().execute(
            "SELECT DISTINCT substr(p.taken_at, 1, 4) FROM photos p JOIN favorites f ON f.sha256 = p.sha256 ORDER BY 1 DESC")]
        people = [r[0] for r in db().execute(
            """SELECT DISTINCT pe.name FROM favorites fav JOIN faces f ON f.sha256 = fav.sha256
               JOIN people pe ON pe.id = f.person_id ORDER BY pe.name""")]
        return render_template("favorites.html", photos=rows, years=years, people=people, year=year, person=person,
                               root=windows_path(cfg.highlights), library=windows_path(cfg.library))

    @app.post("/photo/<sha>/tray")
    def tray_toggle(sha):
        return act(actions.toggle_tray, db(), sha)

    @app.post("/photo/<sha>/tags")
    def tags(sha):
        return act(actions.set_tags, cfg, db(), sha, request.form.get("tags", ""))

    @app.post("/photo/<sha>/date")
    def date(sha):
        try:
            when = datetime.fromisoformat(request.form.get("when", ""))
        except ValueError:
            flash("Enter a date and time.", "error")
            return back("/undated")
        return act(actions.set_date, cfg, db(), sha, when, default="/undated")

    @app.post("/undated/set-day")
    def set_day():
        try:
            day = calendar_date.fromisoformat(request.form.get("day", ""))  # `date` is a route below
        except ValueError:
            flash("Pick a date.", "error")
            return back("/undated")

        def run():
            n = actions.set_day(cfg, db(), request.form.getlist("photo"), day)
            flash(f"Set {n} photo(s) to {day:%a %-d %b %Y}.", "ok")
        return act(run, default="/undated")

    @app.post("/day/<day>/reviewed")
    def reviewed(day):
        return act(actions.toggle_reviewed, db(), day)

    # ---- Events ----

    @app.get("/events")
    def events():
        return render_template("events.html", events=events_mod.suggest(db(), cfg.event_gap_hours))

    @app.post("/events/name")
    def events_name():
        def run():
            events_mod.name(db(), cfg.event_gap_hours, request.form["event"], request.form.get("name", ""),
                            request.form.get("through") or None)
            actions.refresh(cfg, db())
        return act(run, default="/events")

    @app.post("/events/unname")
    def events_unname():
        def run():
            events_mod.unname(db(), request.form.get("name", ""))
            actions.refresh(cfg, db())
        return act(run, default="/events")

    # ---- Faces ----

    @app.get("/faces")
    def faces():
        people, groups = faces_mod.summary(db(), top=40)
        samples = {
            g["cluster"]: [r["id"] for r in db().execute(
                "SELECT id FROM faces WHERE cluster = ? ORDER BY confidence DESC LIMIT 8", (g["cluster"],))]
            for g in groups
        }
        ids = {r["name"]: r["id"] for r in db().execute("SELECT id, name FROM people")}
        return render_template("faces.html", people=people, groups=groups, samples=samples, person_ids=ids)

    @app.get("/faces/person/<int:person_id>")
    def person(person_id):
        p = db().execute("SELECT * FROM people WHERE id = ?", (person_id,)).fetchone()
        if p is None:
            abort(404)
        faces = db().execute(
            """SELECT f.id, f.label_source, f.similarity, ph.moment_id FROM faces f
               JOIN photos ph ON ph.sha256 = f.sha256 WHERE f.person_id = ?
               ORDER BY f.label_source = 'user', f.similarity, ph.taken_at""",
            (person_id,),
        ).fetchall()
        return render_template("person.html", person=p, faces=faces)

    @app.post("/faces/label")
    def faces_label():
        ticked = {int(i) for i in request.form.getlist("face")}
        group = request.form.get("group", type=int)
        if group is None:
            face_ids = sorted(ticked)
        else:
            # Whole group, minus any sample face you unticked because it isn't this person.
            unticked = {int(i) for i in request.form.getlist("shown")} - ticked
            face_ids = [r["id"] for r in db().execute("SELECT id FROM faces WHERE cluster = ?", (group,))
                        if r["id"] not in unticked]
        if not face_ids:
            flash("No faces selected.", "error")
            return back("/faces")
        # Unticked faces are "not this person", so they won't be auto-matched to the name.
        unticked = sorted({int(i) for i in request.form.getlist("shown")} - ticked)
        return act(faces_mod.label, cfg, db(), request.form.get("name", ""), None, face_ids, unticked,
                   default="/faces")

    @app.post("/faces/unlabel")
    def faces_unlabel():
        return act(faces_mod.unlabel, cfg, db(), [int(i) for i in request.form.getlist("face")], default="/faces")

    # ---- Images ----

    @app.get("/thumb/<sha>/<int:size>.jpg")
    def thumb(sha, size):
        if size not in THUMB_SIZES:
            abort(404)
        row = db().execute("SELECT library_path FROM photos WHERE sha256 = ?", (sha,)).fetchone()
        if row is None or row["library_path"] is None:
            abort(404)
        out = cfg.state_dir / "thumbs" / f"{sha}_{size}.jpg"  # content-addressed: never stale
        if not out.exists():
            src = cfg.library / row["library_path"]
            if not src.exists():
                abort(404)
            with Image.open(src) as im:
                im.draft("RGB", (size, size))
                img = ImageOps.exif_transpose(im).convert("RGB")
            img.thumbnail((size, size), Image.LANCZOS)
            _save_atomic(img, out)
        return send_file(out, mimetype="image/jpeg", max_age=86400)

    @app.get("/clip/<sha>")
    def live_clip(sha):
        row = db().execute("SELECT library_path FROM live_clips WHERE sha256 = ?", (sha,)).fetchone()
        if row is None or row["library_path"] is None or not (cfg.library / row["library_path"]).exists():
            abort(404)
        return send_file(cfg.library / row["library_path"], conditional=True, max_age=0)

    @app.get("/face/<int:face_id>.jpg")
    def face_thumb(face_id):
        row = db().execute(
            "SELECT f.*, p.library_path FROM faces f JOIN photos p ON p.sha256 = f.sha256 WHERE f.id = ?", (face_id,)
        ).fetchone()
        if row is None or row["library_path"] is None:
            abort(404)
        out = cfg.state_dir / "facethumbs" / f"{face_id}_{row['sha256'][:12]}.jpg"
        if not out.exists():
            crop = faces_mod.crop_face(cfg, row)
            if crop is None:
                abort(404)
            _save_atomic(crop, out)
        return send_file(out, mimetype="image/jpeg", max_age=86400)

    app.jinja_env.globals["pretty_folder"] = pretty_folder
    app.jinja_env.globals["video_path"] = lambda rel: windows_path(cfg.videos / rel)
    app.jinja_env.globals["library_path"] = lambda rel: windows_path(cfg.library / rel)
    app.jinja_env.globals["highlight_path"] = lambda rel: windows_path(cfg.highlights / rel)
    app.jinja_env.globals["playable"] = lambda ext: ext.lower() in videos_mod.PLAYABLE
    app.jinja_env.filters["duration"] = _duration
    return app


# Columns every photo card needs; `p` is photos.
CARD_COLUMNS = """p.sha256, p.name, p.library_path, p.moment_id, p.is_best, p.close_call,
    (SELECT COUNT(*) FROM photos m WHERE m.moment_id = p.moment_id AND m.duplicate_of IS NULL) AS shots,
    (SELECT COUNT(*) FROM photos m WHERE m.moment_id = p.moment_id AND m.duplicate_of IS NOT NULL) AS copies,
    p.duplicate_of,
    (SELECT d.name FROM photos d WHERE d.sha256 = p.duplicate_of) AS copy_of,
    EXISTS (SELECT 1 FROM tray t WHERE t.sha256 = p.sha256) AS in_tray,
    (SELECT group_concat(name, ', ') FROM (SELECT DISTINCT pe.name FROM faces f
        JOIN people pe ON pe.id = f.person_id WHERE f.sha256 = p.sha256 ORDER BY pe.name)) AS people,
    (SELECT group_concat(tag, ', ') FROM (SELECT tag FROM tags WHERE sha256 = p.sha256 ORDER BY tag)) AS tags,
    (SELECT group_concat(post, ', ') FROM (SELECT post FROM exports WHERE sha256 = p.sha256 ORDER BY post)) AS posts,
    (SELECT c.sha256 FROM live_clips c JOIN sources s ON s.path = c.photo_path
        WHERE s.sha256 = p.sha256 LIMIT 1) AS live_clip,
    EXISTS (SELECT 1 FROM favorites fv WHERE fv.sha256 = p.sha256) AS favorite"""


def folder_key(library_path: str) -> str:
    parts = library_path.split("/")
    return "_undated" if parts[0] == "_undated" else "/".join(parts[:2])


def pretty_folder(key: str) -> tuple[str, str]:
    """'2026/2026-07-03_birthday-party' → ('Fri 3 Jul 2026', 'birthday party')."""
    if key == "_undated":
        return "Undated", ""
    name = key.split("/")[1]
    if name.endswith("_unknown-day"):
        return datetime.strptime(name[:7], "%Y-%m").strftime("%B %Y"), "day unknown"
    day, _, slug = name.partition("_")
    return datetime.strptime(day, "%Y-%m-%d").strftime("%a %-d %b %Y"), slug.replace("-", " ")


def folders(conn: sqlite3.Connection) -> list[dict]:
    """One entry per library folder (day or day+event), with counts."""
    reviewed = {r["day"] for r in conn.execute("SELECT day FROM reviewed")}
    acc: dict[str, dict] = {}
    def entry(key):
        return acc.setdefault(key, {"key": key, "photos": 0, "moments": set(), "close": set(), "videos": 0})

    for r in conn.execute("SELECT library_path FROM videos WHERE library_path IS NOT NULL"):
        entry(folder_key(r["library_path"]))["videos"] += 1
    for r in conn.execute(
        "SELECT library_path, moment_id, close_call FROM photos WHERE library_path IS NOT NULL ORDER BY taken_at"
    ):
        key = folder_key(r["library_path"])
        f = entry(key)
        f["photos"] += 1
        f["moments"].add(r["moment_id"])
        if r["close_call"]:
            f["close"].add(r["moment_id"])
    out = []
    for key, f in sorted(acc.items()):
        day = None if key == "_undated" or key.endswith("_unknown-day") else key.split("/")[1][:10]
        out.append({
            "key": key, "year": "Undated" if key == "_undated" else key[:4], "day": day,
            "photos": f["photos"], "moments": len(f["moments"]), "close_calls": len(f["close"]),
            "videos": f["videos"],
            "reviewed": day in reviewed,
        })
    return out


def _save_atomic(img: Image.Image, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    partial = out.with_name(f"{out.name}.{secrets.token_hex(4)}.partial")  # unique per request thread
    img.save(partial, "JPEG", quality=82)
    os.replace(partial, out)


def _duration(seconds) -> str:
    if not seconds:
        return ""
    m, s = divmod(int(round(seconds)), 60)
    return f"{m // 60}:{m % 60:02}:{s:02}" if m >= 60 else f"{m}:{s:02}"
