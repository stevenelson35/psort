"""Command-line interface (DESIGN.md §9)."""

import os
import sqlite3
import urllib.request
from pathlib import Path
from typing import Annotated

import typer

from . import config as config_mod
from . import events as events_mod
from . import faces as faces_mod
from . import blog as blog_mod
from . import highlights as highlights_mod
from .config import DEFAULT_CONFIG_PATH, DEFAULT_STATE_DIR, Config, ConfigError
from .dates import UNCERTAIN, sql_in
from .db import connect
from .export import ExportError, export
from . import ingest as ingest_mod
from .ingest import ingest
from .library import curate, verify, write_manifest
from .moments import cluster, score
from .progress import Progress, Stage
from .winpath import windows_path

app = typer.Typer(no_args_is_help=True, help="Local photo curation. See DESIGN.md.")
events_app = typer.Typer(help="Suggested events, and naming them.", invoke_without_command=True)
faces_app = typer.Typer(help="Face recognition: see who's who and name people.", no_args_is_help=True)
app.add_typer(events_app, name="events")
app.add_typer(faces_app, name="faces")

ConfigOpt = Annotated[Path, typer.Option("--config", "-c", help="Path to psort.toml.")]
_config_path = DEFAULT_CONFIG_PATH


@app.callback()
def main(config: ConfigOpt = DEFAULT_CONFIG_PATH) -> None:
    global _config_path
    _config_path = config


def _open() -> tuple[Config, sqlite3.Connection]:
    try:
        cfg = config_mod.load(_config_path)
    except ConfigError as e:
        typer.secho(str(e), fg="red", err=True)
        raise typer.Exit(1) from e
    return cfg, connect(cfg.db_path)


@app.command()
def init(
    inbox: Annotated[Path, typer.Option(help="Folder you drop photo batches into. psort never writes here.")],
    library: Annotated[Path, typer.Option(help="Where the curated library is built.")],
    outbox: Annotated[Path, typer.Option(help="Where exports for blog posts go.")],
    state_dir: Annotated[Path, typer.Option(help="psort's database and models (keep inside WSL).")] = DEFAULT_STATE_DIR,
    videos: Annotated[Path | None, typer.Option(help="Video tree (default: videos/ beside the library).")] = None,
    face_model: Annotated[bool, typer.Option(help="Download the face detection + recognition models (~39 MB).")] = True,
    force: Annotated[bool, typer.Option(help="Overwrite an existing config.")] = False,
) -> None:
    """Create psort.toml and the state database."""
    if _config_path.exists() and not force:
        typer.secho(f"{_config_path} already exists (use --force to overwrite).", fg="red", err=True)
        raise typer.Exit(1)
    inbox, library, outbox, state_dir = (p.expanduser().resolve() for p in (inbox, library, outbox, state_dir))
    videos = videos.expanduser().resolve() if videos else None
    config_mod.write_default(_config_path, inbox, library, outbox, state_dir, videos)
    cfg = config_mod.load(_config_path)
    connect(cfg.db_path).close()
    library.mkdir(parents=True, exist_ok=True)
    outbox.mkdir(parents=True, exist_ok=True)
    typer.echo(f"Wrote {_config_path}")
    if not inbox.is_dir():
        typer.secho(f"Note: inbox {inbox} doesn't exist yet.", fg="yellow")
    if face_model:
        _download_models(cfg)


@app.command("fetch-models")
def fetch_models() -> None:
    """Download the face detection and recognition models if missing."""
    cfg, _ = _open()
    _download_models(cfg)


def _download_models(cfg: Config) -> None:
    for path, url in cfg.models:
        if path.exists():
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_name(path.name + ".partial")
        try:
            urllib.request.urlretrieve(url, partial)
            os.replace(partial, path)
            typer.echo(f"Downloaded {path.name}")
        except OSError as e:
            typer.secho(f"Couldn't download {path.name} ({e}); face features stay off.", fg="yellow")


@app.command("ingest")
def ingest_cmd() -> None:
    """Scan the inbox and record every file."""
    cfg, conn = _open()
    _ingest(cfg, conn)


@app.command("cluster")
def cluster_cmd() -> None:
    """Group near-duplicate bursts into moments."""
    cfg, conn = _open()
    _cluster(cfg, conn)


@app.command("score")
def score_cmd() -> None:
    """Score photos and pick each moment's best shot."""
    cfg, conn = _open()
    score(cfg, conn)
    typer.echo("Scored.")


@app.command("curate")
def curate_cmd(dry_run: Annotated[bool, typer.Option(help="Show what would change.")] = False) -> None:
    """Copy and arrange photos in the library."""
    cfg, conn = _open()
    _curate(cfg, conn, dry_run)


@app.command()
def run(dry_run: Annotated[bool, typer.Option(help="Don't touch the library; show what would change.")] = False) -> None:
    """Ingest → group moments → score → arrange library → faces → highlights, with progress."""
    cfg, conn = _open()
    if not cfg.inbox.is_dir():
        typer.secho(f"Inbox not found: {cfg.inbox}", fg="red", err=True)
        raise typer.Exit(1)
    # One progress display for the whole run, showing from the very first moment: just looking at a
    # big inbox on a Windows drive takes a minute or more. Step sizes are filled in once known.
    names = ["Scanning inbox", "Grouping moments", "Scoring", "Arranging library"]
    if not dry_run:
        names += ["Finding faces", "Updating highlights"]
    p = Progress([Stage(n, 1) for n in names])
    p.start(0, "looking at the inbox…")
    try:
        files, pending = ingest_mod.plan(cfg, conn, note=p.note)
    except BaseException:
        p.close()
        raise
    photos = conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0] + pending
    faces_on = cfg.recognition_model.exists() and cfg.face_model.exists()
    unscanned = conn.execute("SELECT COUNT(*) FROM photos WHERE faces_scanned = 0").fetchone()[0] + pending
    # Rough seconds per step, so "overall" and "time left" reflect this run's actual work.
    estimates = [1 + 0.02 * len(files) + 0.5 * pending, 1 + 0.0005 * photos, 0.5 + 0.0003 * photos,
                 2 + 0.2 * pending, 0.5 + (0.3 * unscanned if faces_on else 0), 1]
    for i in range(len(names)):
        p.set_weight(i, estimates[i])
    try:
        p.start(0, f"{len(files):,} files, {pending:,} new or changed")
        _ingest(cfg, conn, p, files)
        p.finish(0)
        p.start(1)
        _cluster(cfg, conn, p)
        p.finish(1)
        p.start(2)
        score(cfg, conn)
        close = conn.execute("SELECT COUNT(DISTINCT moment_id) FROM photos WHERE close_call = 1").fetchone()[0]
        p.finish(2, f"{close:,} close call(s)")
        p.start(3)
        _curate(cfg, conn, dry_run, p)
        p.finish(3)
        if not dry_run:
            todo = conn.execute("SELECT COUNT(*) FROM photos WHERE faces_scanned = 0 AND library_path IS NOT NULL"
                                ).fetchone()[0]
            p.set_weight(4, 0.5 + (0.3 * todo if faces_on else 0))  # now we know exactly
            p.start(4, f"{todo:,} photos to scan" if faces_on else "")
            _faces(cfg, conn, p)
            p.finish(4)
            p.start(5)
            _highlights(cfg, conn, p)
            p.finish(5)
        p.done()
    finally:
        p.close()  # stop the spinner even if the run is interrupted


@app.command()
def review(port: Annotated[int, typer.Option(help="Port on 127.0.0.1.")] = 5000) -> None:
    """Open the review UI in your browser at http://localhost:<port>."""
    from .review import create_app

    cfg, _ = _open()
    typer.echo(f"psort review running at http://localhost:{port}  (Ctrl+C to stop)")
    create_app(cfg, _config_path).run(host="127.0.0.1", port=port, threaded=True)


@app.command("export")
def export_cmd(
    post: Annotated[str, typer.Argument(help="Post name; becomes the outbox folder (e.g. go-dogs-go).")],
    names: Annotated[list[str] | None, typer.Argument(help="Library names to export instead of the tray.")] = None,
    keep_tray: Annotated[bool, typer.Option(help="Leave exported photos in the tray.")] = False,
) -> None:
    """Export the post tray as web-ready JPEGs (upright, ≤2048px, no location data)."""
    cfg, conn = _open()
    try:
        result = export(cfg, conn, post, names, keep_tray)
    except (ExportError, ValueError) as e:
        typer.secho(str(e), fg="red", err=True)
        raise typer.Exit(1) from e
    typer.echo(f"Exported {len(result.files)} photo(s) to {result.folder}")
    typer.echo(f"In Windows: {windows_path(result.folder)}")


@app.command()
def status() -> None:
    """Library totals."""
    cfg, conn = _open()
    counts = {
        "Photos": "SELECT COUNT(*) FROM photos",
        "Moments": "SELECT COUNT(DISTINCT moment_id) FROM photos",
        "Alternates": "SELECT COUNT(*) FROM photos WHERE is_best = 0 AND duplicate_of IS NULL",
        "Visual duplicates": "SELECT COUNT(*) FROM photos WHERE duplicate_of IS NOT NULL",
        "Close calls": "SELECT COUNT(DISTINCT moment_id) FROM photos WHERE close_call = 1",
        "Favorites": "SELECT COUNT(*) FROM favorites",
        "In trash": "SELECT COUNT(*) FROM deleted_photos WHERE purged = 0",
        "Deleted for good": "SELECT COUNT(*) FROM deleted_photos WHERE purged = 1",
        "Named events": "SELECT COUNT(*) FROM named_events",
        "Named people": "SELECT COUNT(*) FROM people",
        "Faces found": "SELECT COUNT(*) FROM faces",
        "Exact duplicates": "SELECT COUNT(*) - COUNT(DISTINCT sha256) FROM sources WHERE status = 'image'",
        "Undated": f"SELECT COUNT(*) FROM photos WHERE date_source IN {sql_in(UNCERTAIN)}",
        "Screenshots": "SELECT COUNT(*) FROM photos WHERE is_screenshot = 1",
        "Videos": "SELECT COUNT(*) FROM videos",
        "Live Photo clips": "SELECT COUNT(*) FROM live_clips",
        "Rich Capture": "SELECT COUNT(*) FROM rich_packages",
        "Rich frames": "SELECT COUNT(*) FROM derived_frames d JOIN photos p ON p.sha256 = d.sha256",
        "Other files": "SELECT COUNT(*) FROM other_files",
        "Unreadable": "SELECT COUNT(*) FROM sources WHERE status = 'error'",
        "Not copied yet": """SELECT (SELECT COUNT(*) FROM photos WHERE library_path IS NULL)
            + (SELECT COUNT(*) FROM videos WHERE library_path IS NULL)
            + (SELECT COUNT(*) FROM live_clips WHERE library_path IS NULL)
            + (SELECT COUNT(*) FROM other_files WHERE library_path IS NULL)""",
    }
    for label, sql in counts.items():
        typer.echo(f"{label + ':':19}{conn.execute(sql).fetchone()[0]}")
    typer.echo(f"{'Face detection:':19}{'on' if cfg.face_model.exists() else 'off (model not downloaded)'}")
    typer.echo(f"{'Face recognition:':19}{'on' if cfg.recognition_model.exists() else 'off (model not downloaded)'}")


@app.command("blog-login")
def blog_login(
    user: Annotated[str, typer.Option(help="FTP username.")] = "sjnelson@itsallonesong.com",
    host: Annotated[str, typer.Option(help="FTP server (Turbify's name matches its TLS certificate).")] = "cpanel292.turbify.biz",
    remote_dir: Annotated[str, typer.Option(help="Blog photos folder, from the FTP account's top.")] = "pics/blog",
    repo: Annotated[Path, typer.Option(help="Your local blog repo.")] = Path.home() / "repos/stevenelson35.github.io",
    site_url: Annotated[str, typer.Option(help="The blog's address.")] = "https://blog.itsallonesong.com",
    tls: Annotated[bool, typer.Option(hidden=True)] = True,
    port: Annotated[int, typer.Option(hidden=True)] = 21,
) -> None:
    """Test your Turbify FTP login and save it for publishing posts (password stays on this PC)."""
    _open()  # make sure psort is set up
    s = blog_mod.BlogSettings(repo=repo.expanduser().resolve(), ftp_host=host, ftp_user=user, remote_dir=remote_dir,
                              site_url=site_url, ftp_tls=tls, ftp_port=port)
    password = typer.prompt(f"FTP password for {user}", hide_input=True)
    typer.echo(f"Connecting to {host}{' with TLS' if tls else ''}…")
    try:
        up = blog_mod.Uploader(s, password)
    except blog_mod.BlogError as e:
        typer.secho(str(e), fg="red", err=True)
        raise typer.Exit(1) from e
    sample = up.check()
    up.close()
    if not sample:
        typer.secho(f"Logged in, but {remote_dir}/1024 has no photos. Is {remote_dir!r} the blog photos folder?",
                    fg="yellow")
        raise typer.Exit(1)
    typer.echo(f"Login OK. {remote_dir}/1024 has photos such as {', '.join(sample)}.")
    if not (s.posts_dir).is_dir():
        typer.secho(f"Warning: {s.posts_dir} not found. Check --repo.", fg="yellow")
    blog_mod.save_password(password)
    blog_mod.write_settings(_config_path, s)
    typer.echo(f"Saved. Settings are in {_config_path} [blog]; the password is in {blog_mod.SECRETS_PATH} (private).")


@app.command("reconcile")
def reconcile_cmd(apply: Annotated[bool, typer.Option(help="Make the changes (otherwise just report).")] = False) -> None:
    """Catch up with files you moved, renamed or deleted by hand in the library, videos or unsorted files."""
    from . import actions
    from .reconcile import reconcile

    cfg, conn = _open()
    r = reconcile(cfg, conn, apply=apply)
    for label, old, new in r.moved:
        typer.echo(f"  moved   {label}: {old} → {new}")
    for label, path in r.missing:
        typer.echo(f"  missing {label}: {path}")
    if r.unknown:
        typer.echo(f"  {len(r.unknown)} file(s) psort didn't put there (left alone):")
        for path in r.unknown[:20]:
            typer.echo(f"    {path}")
    if not (r.moved or r.missing):
        typer.echo("Everything is where psort expects it.")
        return
    if not apply:
        typer.echo(f"\n{len(r.moved)} moved, {len(r.missing)} missing. Run `psort reconcile --apply` to:\n"
                   "  - adopt moved files (psort then files them back in its usual place, without re-copying)\n"
                   "  - record missing photos as deleted, so they're never copied back from the inbox")
        return
    actions.refresh(cfg, conn, recluster=True)
    typer.echo(f"Adopted {len(r.moved)} moved file(s); recorded missing photos as deleted. Library is back in order.")


@app.command("empty-trash")
def empty_trash_cmd() -> None:
    """Permanently delete the photos in library/_trash (psort still won't copy them back)."""
    from . import actions

    cfg, conn = _open()
    n = conn.execute("SELECT COUNT(*) FROM deleted_photos WHERE purged = 0").fetchone()[0]
    if n and typer.confirm(f"Permanently delete {n} photo(s) in the trash?"):
        typer.echo(f"Deleted {actions.empty_trash(cfg, conn)} photo(s) for good.")
    elif not n:
        typer.echo("The trash is empty.")


@app.command("highlights")
def highlights_cmd() -> None:
    """Bring highlights/ up to date with your favorites (also part of `psort run`)."""
    cfg, conn = _open()
    _highlights(cfg, conn)
    n = conn.execute("SELECT COUNT(*) FROM highlights").fetchone()[0]
    typer.echo(f"{n} favorite(s) in {cfg.highlights}\nIn Windows: {windows_path(cfg.highlights)}")


@app.command("close-calls")
def close_calls() -> None:
    """Moments where the automatic best pick was a near tie. Worth a quick look."""
    cfg, conn = _open()
    rows = conn.execute(
        """SELECT moment_id, name, library_path, score, is_best FROM photos
           WHERE close_call = 1 ORDER BY moment_id, is_best DESC, score DESC"""
    ).fetchall()
    if not rows:
        typer.echo("No close calls.")
        return
    moment = None
    for r in rows:
        if r["moment_id"] != moment:
            moment = r["moment_id"]
            typer.echo("")
        typer.echo(f"  {'★' if r['is_best'] else ' '} {r['score']:.3f}  {r['library_path']}")
    count = len({r["moment_id"] for r in rows})
    typer.echo(f"\n{count} close call(s). ★ = current pick. The review UI will let you choose.")


@events_app.callback()
def events_list(ctx: typer.Context) -> None:
    """List suggested events (a new one starts after a long gap in shooting)."""
    if ctx.invoked_subcommand:
        return
    cfg, conn = _open()
    events = events_mod.suggest(conn, cfg.event_gap_hours)
    for e in events:
        span = e.start[:16].replace("T", " ") + (" → " + e.end[:16].replace("T", " ") if e.end != e.start else "")
        typer.echo(f"  {e.id}  {e.photos:4} photos  {span}  {e.slug or ''}")
    typer.echo(f"{len(events)} events. Name one: psort events name <id> <name> [--through <id>]")


@events_app.command("name")
def events_name(
    event: Annotated[str, typer.Argument(help="Event id from `psort events`.")],
    name: Annotated[str, typer.Argument(help="e.g. birthday-party (spaces become hyphens).")],
    through: Annotated[str | None, typer.Option(help="Last event id, for a multi-day event.")] = None,
) -> None:
    """Name an event; its day folders become YYYY-MM-DD_<name>."""
    cfg, conn = _open()
    try:
        slug = events_mod.name(conn, cfg.event_gap_hours, event, name, through)
    except events_mod.EventError as e:
        typer.secho(str(e), fg="red", err=True)
        raise typer.Exit(1) from e
    typer.echo(f"Named {slug!r}.")
    _curate(cfg, conn, dry_run=False)


@events_app.command("unname")
def events_unname(name: str) -> None:
    """Remove an event name; its folders go back to plain dates."""
    cfg, conn = _open()
    try:
        events_mod.unname(conn, name)
    except events_mod.EventError as e:
        typer.secho(str(e), fg="red", err=True)
        raise typer.Exit(1) from e
    _curate(cfg, conn, dry_run=False)


@faces_app.command("scan")
def faces_scan() -> None:
    """Find faces in library photos that haven't been scanned yet (also part of `psort run`)."""
    cfg, conn = _open()
    _faces(cfg, conn)


@faces_app.command("list")
def faces_list() -> None:
    """Named people, and the biggest groups of unnamed look-alike faces."""
    cfg, conn = _open()
    people, clusters = faces_mod.summary(conn)
    typer.echo("People:" if people else "No named people yet.")
    for p in people:
        typer.echo(f"  {p['name']:20} {p['photos']:5} photos  ({p['user']} named by you, {p['auto']} auto)")
    typer.echo("Unnamed groups:" if clusters else "No unnamed faces.")
    for c in clusters:
        typer.echo(f"  group {c['cluster']:<6} {c['faces']:5} faces in {c['photos']} photos")
    if clusters:
        typer.echo("See them: psort faces crops   Name one: psort faces label <name> --group <id>")


@faces_app.command("crops")
def faces_crops() -> None:
    """Write face thumbnails per person and group, to browse in File Explorer."""
    cfg, conn = _open()
    out = faces_mod.write_crops(cfg, conn)
    typer.echo(f"Face thumbnails in {out}\nIn Windows: {windows_path(out)}")


@faces_app.command("label")
def faces_label(
    name: Annotated[str, typer.Argument(help="Person's name, e.g. Steve.")],
    group: Annotated[int | None, typer.Option(help="Unnamed group id from `psort faces list`.")] = None,
    face: Annotated[list[int] | None, typer.Option(help="Face id (from a thumbnail name). Repeatable.")] = None,
) -> None:
    """Name the faces in a group (or specific faces). Similar faces get the name automatically."""
    cfg, conn = _open()
    if group is None and not face:
        typer.secho("Give --group or --face.", fg="red", err=True)
        raise typer.Exit(1)
    try:
        n = faces_mod.label(cfg, conn, name, group, face)
    except faces_mod.FaceError as e:
        typer.secho(str(e), fg="red", err=True)
        raise typer.Exit(1) from e
    auto = conn.execute(
        "SELECT COUNT(*) FROM faces f JOIN people p ON p.id = f.person_id WHERE p.name = ? AND f.label_source = 'auto'",
        (name.strip(),),
    ).fetchone()[0]
    typer.echo(f"Named {n} face(s) {name!r}; {auto} more matched automatically.")
    write_manifest(cfg, conn)


@faces_app.command("unlabel")
def faces_unlabel(face: Annotated[list[int], typer.Option(help="Face id. Repeatable.")]) -> None:
    """Remove a wrong name from specific faces."""
    cfg, conn = _open()
    faces_mod.unlabel(cfg, conn, face)
    typer.echo(f"Unlabeled {len(face)} face(s).")
    write_manifest(cfg, conn)


@app.command("verify")
def verify_cmd(
    batch: Annotated[str | None, typer.Argument(help="Batch folder in the inbox (default: the whole inbox).")] = None,
    show_ok: Annotated[bool, typer.Option("--all", help="List safe files too.")] = False,
) -> None:
    """Report whether an inbox batch (or the whole inbox) is safe to delete."""
    cfg, conn = _open()
    try:
        results = verify(cfg, conn, batch)
    except FileNotFoundError as e:
        typer.secho(str(e), fg="red", err=True)
        raise typer.Exit(1) from e
    problems = [r for r in results if not r.ok]
    for r in results if show_ok else problems:
        typer.echo(f"{'✅' if r.ok else '⚠️ '} {r.path}  —  {r.message}")
    if problems:
        typer.secho(f"{len(problems)} of {len(results)} files are NOT copied anywhere yet. Don't delete.", fg="yellow")
        raise typer.Exit(2)
    what = repr(batch) if batch else "The whole inbox"
    typer.secho(f"All {len(results)} files are copied (or are OS cache files). {what} is safe to delete.", fg="green")


def _say(p: "Progress | None"):
    return p.echo if p else typer.echo


def _ingest(cfg: Config, conn: sqlite3.Connection, p: "Progress | None" = None, files=None) -> None:
    say = _say(p)
    try:
        s = ingest(cfg, conn, log=say, files=files,
                   progress=(lambda d, t: p.update(d, t, "files")) if p else None)
    except FileNotFoundError as e:
        typer.secho(str(e), fg="red", err=True)
        raise typer.Exit(1) from e
    say(
        f"Ingest: {s.new_photos} new, {s.duplicates} exact duplicates, {s.unchanged} unchanged, "
        f"{s.others} other files, {s.errors} unreadable"
        + (f", {s.deleted} you deleted before (not copied)" if s.deleted else "")
    )
    if s.arriving:
        say(typer.style(f"{s.arriving} file(s) still arriving (changed in the last {cfg.settle_seconds:.0f}s): "
                        "left for the next run.", fg="yellow"))
    if s.recovered:
        say(f"{s.recovered} previously unreadable file(s) read fine now and moved out of unsorted_files.")
    if s.replaced:
        say(f"{s.replaced} file(s) changed since last seen (e.g. a copy finished): old copies cleaned up.")
    if s.redated:
        say(f"Dated {s.redated} earlier undated photo(s) from their folder names")
    if s.rich_packages:
        say(f"Rich Capture: {s.rich_packages} package(s), {s.rich_frames} frame(s) added as photos")
    if s.new_videos or s.live_clips:
        say(f"Videos: {s.new_videos} new, {s.live_clips} Live Photo clip(s) kept beside their photos")


def _cluster(cfg: Config, conn: sqlite3.Connection, p: "Progress | None" = None) -> None:
    moments = cluster(cfg, conn)
    copies = conn.execute("SELECT COUNT(*) FROM photos WHERE duplicate_of IS NOT NULL").fetchone()[0]
    _say(p)(f"Moments: {moments} ({copies} visual duplicates set aside in _duplicates)")


def _highlights(cfg: Config, conn: sqlite3.Connection, p: "Progress | None" = None) -> None:
    s = highlights_mod.sync(cfg, conn, log=_say(p))
    if s.written or s.moved or s.removed:
        _say(p)(f"Highlights: {s.written} written, {s.moved} moved, {s.removed} removed → {cfg.highlights}")


def _faces(cfg: Config, conn: sqlite3.Connection, p: "Progress | None" = None) -> None:
    scanned = faces_mod.scan(cfg, conn, log=_say(p), progress=(lambda d, t: p.update(d, t, "photos")) if p else None)
    if scanned is None:
        _say(p)("Faces: recognition model not downloaded (run `psort fetch-models`).")
        return
    faces_mod.assign(cfg, conn)
    _say(p)(f"Faces: scanned {scanned} new photos")
    write_manifest(cfg, conn)


def _curate(cfg: Config, conn: sqlite3.Connection, dry_run: bool, p: "Progress | None" = None) -> None:
    say = _say(p)
    s = curate(cfg, conn, dry_run=dry_run, log=say, progress=(lambda d, t: p.update(d, t, "photos")) if p else None)
    prefix = "Would curate" if dry_run else "Curate"
    say(f"{prefix}: {s.copied} copied, {s.moved} moved, {s.unchanged} unchanged")
    if s.videos_copied or s.videos_moved:
        say(f"{prefix} videos: {s.videos_copied} copied, {s.videos_moved} moved → {cfg.videos}")
    if s.clips_copied:
        say(f"{prefix} Live Photo clips / Rich Capture packages: {s.clips_copied} copied beside their photos")
    if s.others_copied:
        say(f"{prefix} other files: {s.others_copied} copied → {cfg.unsorted}")
    if s.missing:
        say(typer.style(f"{len(s.missing)} photos have no library copy and their inbox files are gone: "
                        + ", ".join(s.missing[:10]), fg="red"))
    if not dry_run:
        write_manifest(cfg, conn)
