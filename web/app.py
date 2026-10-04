"""Scribe web front end.

  /         This Week — morning brief (deadlines, recent meetings, AI narrative)
  /actions  Action Center — Mine / Waiting / Unassigned to-do triage

Run from the repo root:
    conda activate scribe
    uvicorn web.app:app --reload
Then open http://localhost:8000
"""

import asyncio
import os
import re
import shutil
import sys
import tempfile
from collections import OrderedDict
from datetime import date, datetime

import markdown as _md
from fastapi import FastAPI, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from src import config as _config
from src import notes_index as ni
from src import brief as brief_mod
from src import corrections as corr

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_HERE)
_STATIC = os.path.join(_HERE, "static")
templates = Jinja2Templates(directory=os.path.join(_HERE, "templates"))

app = FastAPI(title="Scribe")
app.mount("/static", StaticFiles(directory=_STATIC), name="static")


@app.middleware("http")
async def _refresh_config(request: Request, call_next):
    # Pick up config.yaml edits made while the server runs (e.g. the menu bar's
    # Settings → Identity), so "Mine" vs "Waiting" bucketing stays live without
    # a restart. A cheap mtime check; only re-reads the file when it changed.
    _config.maybe_reload()
    return await call_next(request)


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    # Browsers request /favicon.ico automatically; serve the small PNG.
    return FileResponse(os.path.join(_STATIC, "favicon.png"), media_type="image/png")


def _render_md(text: str) -> str:
    return _md.markdown(text or "", extensions=["extra", "sane_lists"])


_VALID_BUCKETS = {"mine", "waiting", "unassigned", "done"}


def _reassign_ctx() -> dict:
    """Context the reassign dropdown needs in any template that renders items."""
    return {"owners": ni.known_people(), "user_name": ni.user_name()}


# The Done tab is an ever-growing archive; cap it to the most recent completed
# items by default (whole meeting groups, never split) with a "Show all" escape.
DONE_PAGE_SIZE = 50


def _group_items(bucket: str = "mine", query: str = "", limit: "int | None" = None):
    """Action items for the given view, grouped by source note (newest first),
    with an optional text filter. The 'done' view shows completed items across
    all buckets; the others show open items in that bucket.

    Returns (groups, total, shown). When `limit` is set, whole groups are kept in
    order until `shown` items are reached, so a meeting's items are never split.
    """
    all_items = ni.action_items(include_done=True)
    # Folded merge members are hidden everywhere; their primary carries them as
    # `sources` so the consolidated row can list where they came from.
    folded_by_gid: "dict[str, list]" = {}
    for it in all_items:
        if it.merge_id and not it.merge_primary:
            folded_by_gid.setdefault(it.merge_id, []).append(it)

    if bucket == "done":
        items = [it for it in all_items if it.done and it.bucket not in ("merged", "someday")]
    else:
        items = [it for it in all_items if not it.done and it.bucket == bucket]
    for it in items:
        if it.merge_primary:
            it.sources = folded_by_gid.get(it.merge_id, [])
    q = query.strip().lower()
    filtered = [
        it for it in items
        if (not q or q in it.text.lower() or q in it.note_title.lower()
            or (it.owner and q in it.owner.lower()))
    ]
    total = len(filtered)

    groups: "OrderedDict[str, dict]" = OrderedDict()
    for it in filtered:
        g = groups.get(it.note_filename)
        if g is None:
            g = {
                "note_filename": it.note_filename,
                "note_title": it.note_title,
                "note_date": it.note_date,
                "items": [],
            }
            groups[it.note_filename] = g
        g["items"].append(it)
    all_groups = list(groups.values())

    if limit is None:
        return all_groups, total, total
    shown_groups, shown = [], 0
    for g in all_groups:
        shown_groups.append(g)
        shown += len(g["items"])
        if shown >= limit:
            break
    return shown_groups, total, shown


def _greeting() -> str:
    h = datetime.now().hour
    if h < 12:
        return "Good morning"
    if h < 17:
        return "Good afternoon"
    return "Good evening"


@app.get("/", response_class=HTMLResponse)
def this_week(request: Request):
    """Morning brief: AI narrative (cached) + deadlines + recent meetings."""
    cached = brief_mod.cached_brief()
    deadlines = ni.deadline_view(ni.items_with_deadlines())
    recent = ni.recent_notes(30)
    uname = ni.user_name()
    first_name = uname.split()[0] if uname and uname != "Me" else ""

    nr, nd = len(recent), len(deadlines)
    bits = []
    if nr:
        bits.append(f"{nr} recent meeting{'' if nr == 1 else 's'}")
    if nd:
        bits.append(f"{nd} open deadline{'' if nd == 1 else 's'}")
    summary_line = " · ".join(bits) if bits else "No meetings logged in the last 30 days."

    return templates.TemplateResponse(request, "brief.html", {
        "nav": "brief",
        "deadlines": deadlines[:12],
        "deadlines_total": nd,
        "recent": recent,
        "brief": cached,
        "brief_html": _render_md(cached["markdown"]) if cached else None,
        "today": date.today().strftime("%A, %B %-d, %Y"),
        "greeting": _greeting(),
        "first_name": first_name,
        "summary_line": summary_line,
    })


@app.post("/brief/generate", response_class=HTMLResponse)
def brief_generate(request: Request):
    data = brief_mod.generate_brief(days=30)
    return templates.TemplateResponse(request, "_aibrief.html", {
        "brief": data, "brief_html": _render_md(data["markdown"]),
    })


def _notes_view(query: str = ""):
    """All notes (newest first), optionally filtered by a case-insensitive query
    over title, topics, and body."""
    notes = ni.load_notes()
    q = query.strip().lower()
    if not q:
        return notes
    return [
        n for n in notes
        if q in n.title.lower() or q in n.body.lower()
        or any(q in t.lower() for t in n.topics)
    ]


@app.get("/notes", response_class=HTMLResponse)
def all_notes(request: Request, q: str = ""):
    notes = _notes_view(q)
    return templates.TemplateResponse(request, "all_notes.html", {
        "nav": "notes", "notes": notes, "q": q,
    })


@app.get("/notes/list", response_class=HTMLResponse)
def notes_list(request: Request, q: str = ""):
    """Filtered notes list partial (htmx target)."""
    return templates.TemplateResponse(request, "_notes_list.html", {"notes": _notes_view(q)})


def _done_limit(bucket: str, query: str, show_all: bool) -> "int | None":
    """Cap only the Done tab, and only when not searching or showing all."""
    if bucket == "done" and not show_all and not query.strip():
        return DONE_PAGE_SIZE
    return None


@app.get("/actions", response_class=HTMLResponse)
def dashboard(request: Request, bucket: str = "mine", q: str = "", show_all: bool = False):
    bucket = bucket if bucket in _VALID_BUCKETS else "mine"
    groups, total, shown = _group_items(bucket, q, _done_limit(bucket, q, show_all))
    return templates.TemplateResponse(request, "dashboard.html", {
        "nav": "actions",
        "groups": groups,
        "bucket": bucket,
        "q": q,
        "stats": ni.stats(),
        "done_total": total,
        "done_shown": shown,
        "show_all": show_all,
        **_reassign_ctx(),
    })


@app.get("/stats")
def stats_json():
    """Live counts for the header + tab badges (fetched after toggles/clears)."""
    return ni.stats()


@app.get("/items", response_class=HTMLResponse)
def items(request: Request, bucket: str = "mine", q: str = "", show_all: bool = False):
    """Filtered list partial (htmx target)."""
    bucket = bucket if bucket in _VALID_BUCKETS else "mine"
    groups, total, shown = _group_items(bucket, q, _done_limit(bucket, q, show_all))
    return templates.TemplateResponse(request, "_list.html", {
        "groups": groups, "bucket": bucket, "q": q,
        "done_total": total, "done_shown": shown, "show_all": show_all,
        **_reassign_ctx(),
    })


def _reread_item(filename: str, line_no: int):
    """Re-read one item; if it's a merge primary, attach its folded members so the
    re-rendered row keeps its consolidated appearance."""
    items_all = ni.action_items(include_done=True)
    item = next((it for it in items_all
                 if it.note_filename == filename and it.line_no == line_no), None)
    if item and item.merge_primary:
        item.sources = [m for m in items_all
                        if m.merge_id == item.merge_id and not m.merge_primary]
    return item


def _row_ctx(item, bucket: str, q: str) -> dict:
    return {"item": item, "bucket": bucket, "q": q, **_reassign_ctx()}


@app.post("/action-items/toggle", response_class=HTMLResponse)
def toggle(request: Request, filename: str = Form(...), line_no: int = Form(...),
           done: bool = Form(False), bucket: str = Form("mine"), q: str = Form("")):
    # Re-render the row in place (struck-through when done) so an accidental check
    # is reversible — just uncheck. Completed rows leave the open view only on the
    # next list refresh ("Clear completed" button, filter, or reload).
    ni.toggle_action_item(filename, line_no, done)
    item = _reread_item(filename, line_no)
    # A consolidated row stands for the whole group — carry the state to its members.
    if item and item.merge_primary:
        for m in item.sources:
            ni.toggle_action_item(m.note_filename, m.line_no, done)
    return templates.TemplateResponse(request, "_item.html", _row_ctx(item, bucket, q))


@app.post("/action-items/reassign", response_class=HTMLResponse)
def reassign(request: Request, filename: str = Form(...), line_no: int = Form(...),
             owner: str = Form(...), bucket: str = Form("mine"), q: str = Form("")):
    ni.set_owner(filename, line_no, owner)
    item = _reread_item(filename, line_no)
    return templates.TemplateResponse(request, "_item.html", _row_ctx(item, bucket, q))


@app.post("/action-items/note", response_class=HTMLResponse)
def set_item_note(request: Request, filename: str = Form(...), line_no: int = Form(...),
                  note: str = Form(""), bucket: str = Form("mine"), q: str = Form("")):
    # The note rides on the item's own line, so no line numbers shift — re-rendering
    # just this row (rather than the whole list) is safe.
    ni.set_note(filename, line_no, note)
    item = _reread_item(filename, line_no)
    return templates.TemplateResponse(request, "_item.html", _row_ctx(item, bucket, q))


@app.post("/action-items/delete", response_class=HTMLResponse)
def delete_action_item(request: Request, filename: str = Form(...), line_no: int = Form(...),
                       bucket: str = Form("mine"), q: str = Form("")):
    """Delete a to-do outright (e.g. one created by mistake). Removes the line and
    re-renders the list (deleting shifts line numbers, so a full re-render is required)."""
    items = ni.action_items(include_done=True)
    target = next((it for it in items
                   if it.note_filename == filename and it.line_no == line_no), None)
    # If it's a consolidated (merge-primary) row, un-merge the group first so the
    # folded members don't get orphaned (left tagged with no primary to show them).
    if target and target.merge_id and target.merge_primary:
        for it in items:
            if it.merge_id == target.merge_id and not (it.note_filename == filename and it.line_no == line_no):
                ni.clear_merge(it.note_filename, it.line_no)
    ni.dismiss_item(filename, line_no)
    return _render_item_list(request, bucket, q)


@app.get("/action-items/merge-candidates", response_class=HTMLResponse)
def merge_candidates(request: Request, filename: str, line_no: int,
                     bucket: str = "mine", q: str = ""):
    """The picker body: other open, non-folded to-dos that can fold into this one."""
    cands = [
        it for it in ni.action_items(include_done=False)
        if it.bucket not in ("merged", "someday")
        and not (it.note_filename == filename and it.line_no == line_no)
    ]
    return templates.TemplateResponse(request, "_merge_candidates.html", {
        "candidates": cands, "primary_filename": filename, "primary_line_no": line_no,
        "bucket": bucket, "q": q,
    })


@app.post("/action-items/merge", response_class=HTMLResponse)
def merge_action_items(request: Request, primary_filename: str = Form(...),
                       primary_line_no: int = Form(...), member: list[str] = Form(default=[]),
                       bucket: str = Form("mine"), q: str = Form("")):
    """Consolidate the selected members into the primary to-do (shared merge id)."""
    import secrets
    members = [r for r in member if r.strip()]
    if not members:
        return _render_item_list(request, bucket, q)  # nothing selected — no-op
    # Reuse the primary's existing group if it already has one; else start a new one.
    gid = ""
    for it in ni.action_items(include_done=True):
        if it.note_filename == primary_filename and it.line_no == primary_line_no and it.merge_id:
            gid = it.merge_id
            break
    gid = gid or secrets.token_hex(3)
    ni.set_merge(primary_filename, primary_line_no, gid, primary=True)
    for ref in members:
        fn, _, ln = ref.rpartition("|")
        if fn and ln.isdigit():
            ni.set_merge(fn, int(ln), gid, primary=False)
    return _render_item_list(request, bucket, q)


@app.post("/action-items/unmerge", response_class=HTMLResponse)
def unmerge_action_items(request: Request, merge_id: str = Form(...),
                         bucket: str = Form("mine"), q: str = Form("")):
    """Split a consolidated group back into individual to-dos."""
    for it in ni.action_items(include_done=True):
        if it.merge_id == merge_id:
            ni.clear_merge(it.note_filename, it.line_no)
    return _render_item_list(request, bucket, q)


@app.post("/action-items/find-duplicates", response_class=HTMLResponse)
def find_duplicates(request: Request, bucket: str = Form("mine"), q: str = Form("")):
    """Ask the LLM to propose groups of duplicate open to-dos (the dupes panel)."""
    from src import dedupe
    result = dedupe.find_duplicate_groups()
    return templates.TemplateResponse(request, "_dupes.html", {
        "dupes": result, "bucket": bucket, "q": q,
    })


@app.post("/action-items/merge-group", response_class=HTMLResponse)
def merge_group(request: Request, primary_ref: str = Form(...),
                ref: list[str] = Form(default=[]), bucket: str = Form("mine"),
                q: str = Form("")):
    """Consolidate a proposed duplicate group: `primary_ref` is the chosen canonical
    to-do, `ref` is every member (incl. the primary), each as 'filename|line_no'."""
    import secrets
    pf, _, pl = primary_ref.rpartition("|")
    if not (pf and pl.isdigit()):
        return _render_item_list(request, bucket, q)
    gid = secrets.token_hex(3)
    ni.set_merge(pf, int(pl), gid, primary=True)
    for r in ref:
        if r == primary_ref:
            continue
        fn, _, ln = r.rpartition("|")
        if fn and ln.isdigit():
            ni.set_merge(fn, int(ln), gid, primary=False)
    return _render_item_list(request, bucket, q)


def _render_item_list(request: Request, bucket: str, q: str) -> HTMLResponse:
    bucket = bucket if bucket in _VALID_BUCKETS else "mine"
    groups, total, shown = _group_items(bucket, q, _done_limit(bucket, q, False))
    return templates.TemplateResponse(request, "_list.html", {
        "groups": groups, "bucket": bucket, "q": q,
        "done_total": total, "done_shown": shown, "show_all": False,
        **_reassign_ctx(),
    })


# ─── single-note view + corrections ─────────────────────────────────────────

def _split_note_body(body: str) -> tuple[str, str]:
    """Split a note body into (rendered-note part, plain transcript part).

    The note embeds its transcript as an Obsidian callout at the end; we pull it
    out so it can live in a collapsible <details> instead of cluttering the page,
    and drop the leading H1 (the page header already shows the title)."""
    marker = "> [!note]- Transcript"
    idx = body.find(marker)
    note_part = (body[:idx] if idx != -1 else body).rstrip()
    note_part = re.sub(r"^#\s+.*\n+", "", note_part, count=1)  # leading H1 dup
    transcript = ""
    if idx != -1:
        rest = body[idx + len(marker):]
        transcript = "\n".join(re.sub(r"^> ?", "", ln) for ln in rest.splitlines()).strip()
    return note_part, transcript


def _diff_rows(before: str, after: str) -> list[dict]:
    """Line-level diff of two note bodies for the 'What changed' panel.

    Returns rows tagged add | del | ctx (with a couple of lines of surrounding
    context), skipping unified-diff file/hunk headers. Blank rows are dropped so
    the panel stays compact."""
    import difflib
    rows: list[dict] = []
    for line in difflib.unified_diff(before.splitlines(), after.splitlines(),
                                     lineterm="", n=2):
        if line.startswith(("---", "+++")):
            continue
        if line.startswith("@@"):
            rows.append({"tag": "hunk", "text": line})
            continue
        tag = {"+": "add", "-": "del"}.get(line[:1], "ctx")
        text = line[1:] if tag in ("add", "del", "ctx") else line
        if text.strip():
            rows.append({"tag": tag, "text": text})
    return rows


# Maps a `from` entry-point key to the breadcrumb back-link (href, label).
_FROM_MAP = {
    "brief": ("/", "This Week"),
    "notes": ("/notes", "All notes"),
    "someday": ("/someday", "Someday"),
    "actions": ("/actions", "Action Center"),
}


def _render_note(request: Request, filename: str, result: dict | None = None,
                 origin_from: str = ""):
    path = corr.resolve_note(filename)
    if not path:
        return HTMLResponse("Note not found.", status_code=404)
    meta, body = ni._parse_frontmatter(open(path, encoding="utf-8").read())
    note_part, transcript = _split_note_body(body)

    # After a successful correction, diff the just-saved backup ("before") against
    # the regenerated note ("after") so the user can see exactly what moved.
    diff = None
    if result and result.get("ok") and not result.get("undo"):
        bak = corr.latest_backup(filename)
        if bak:
            try:
                _, before_body = ni._parse_frontmatter(open(bak, encoding="utf-8").read())
                before_part, _ = _split_note_body(before_body)
                diff = _diff_rows(before_part, note_part)
            except OSError:
                diff = None

    # Breadcrumb: honor where the user came from (`from` param) when known; else
    # fall back to type — a live idea note belongs to Someday, everything else to
    # All Notes.
    is_idea = str(meta.get("type", "")).lower() == "idea"
    archived = bool(meta.get("archived"))
    if origin_from in _FROM_MAP:
        back_href, back_label = _FROM_MAP[origin_from]
    else:
        back_href, back_label = ("/someday", "Someday") if (is_idea and not archived) else ("/notes", "All notes")

    # Backlinks: an idea lists the sessions developed from it (each with an inline,
    # expandable preview so you can review prior work without leaving the page);
    # a session links back to the idea it came from.
    developed = []
    if is_idea:
        for s in ni.developed_sessions(filename):
            note_part, _ = _split_note_body(s.body)
            developed.append({
                "filename": s.filename, "title": s.title, "date": s.date,
                "preview_html": _render_md(note_part),
            })
    origin = None
    fi = meta.get("from_idea")
    if fi:
        on = ni.note_by_filename(str(fi))
        origin = {"filename": str(fi), "title": on.title if on else str(fi)}

    return templates.TemplateResponse(request, "note.html", {
        "nav": "",
        "filename": filename,
        "meta": meta,
        "title": meta.get("title", filename),
        "body_html": _render_md(note_part),
        "transcript": transcript,
        "has_backup": corr.has_backup(filename),
        "result": result,
        "diff": diff,
        "back_href": back_href,
        "back_label": back_label,
        "origin_from": origin_from if origin_from in _FROM_MAP else "",
        "is_idea": is_idea,
        "archived": archived,
        "developed": developed,
        "origin": origin,
    })


@app.get("/note", response_class=HTMLResponse)
def note_detail(request: Request, file: str, origin: str = Query("", alias="from")):
    return _render_note(request, file, origin_from=origin)


@app.post("/note/correct", response_class=HTMLResponse)
def note_correct(request: Request, filename: str = Form(...), correction: str = Form(...),
                 origin: str = Form("", alias="from")):
    res = corr.correct_note(filename, correction)
    return _render_note(request, filename, result=res, origin_from=origin)


@app.post("/note/undo", response_class=HTMLResponse)
def note_undo(request: Request, filename: str = Form(...), origin: str = Form("", alias="from")):
    ok = corr.undo(filename)
    res = {"ok": ok, "undo": True} if ok else {"ok": False, "error": "Nothing to undo."}
    return _render_note(request, filename, result=res, origin_from=origin)


@app.post("/note/delete")
def note_delete(filename: str = Form(...)):
    """Soft-delete a note (move to notes/.trash) and return to its home list."""
    path = corr.resolve_note(filename)
    target = "/notes"
    if path:
        meta, _ = ni._parse_frontmatter(open(path, encoding="utf-8").read())
        if str(meta.get("type", "")).lower() == "idea":
            target = "/someday"
    ni.delete_note(filename)
    return RedirectResponse(target, status_code=303)


@app.post("/note/archive")
def note_archive(filename: str = Form(...), archived: bool = Form(True)):
    """Archive an idea (leaves Someday, stays in All Notes) or restore it."""
    ni.set_archived(filename, archived)
    # After archiving go to All Notes; after restoring, back to Someday.
    return RedirectResponse("/notes" if archived else "/someday", status_code=303)


@app.post("/idea/develop")
async def idea_develop(filename: str = Form(...), audio: UploadFile = File(...)):
    """Record-in-browser → develop a Someday idea into an actionable session note.

    Saves the uploaded audio, runs the normal pipeline as a subprocess (default
    type → real to-dos) linked back to the idea via --from-idea, and returns the
    new note's filename for the browser to navigate to.
    """
    if corr.resolve_note(filename) is None:
        return JSONResponse({"ok": False, "error": "Idea not found."}, status_code=400)

    suffix = os.path.splitext(audio.filename or "")[1] or ".webm"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    try:
        shutil.copyfileobj(audio.file, tmp)
        tmp.close()
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "src.cli", "process", tmp.name,
            "--type", "default", "--from-idea", filename, "--no-slack",
            cwd=_PROJECT_ROOT,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()
        if proc.returncode != 0:
            tail = (err.decode(errors="replace").strip().splitlines() or ["Processing failed."])
            # surface the last non-JSON-marker line as the human error
            msg = next((l for l in reversed(tail) if not l.lstrip().startswith("{")), tail[-1])
            return JSONResponse({"ok": False, "error": msg}, status_code=500)
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass

    sessions = ni.developed_sessions(filename)
    if not sessions:
        return JSONResponse({"ok": False, "error": "Processed, but couldn't locate the new note."}, status_code=500)
    return JSONResponse({"ok": True, "note": sessions[0].filename})


@app.post("/note/title", response_class=HTMLResponse)
def note_title(request: Request, filename: str = Form(...), title: str = Form(...)):
    """Rename a note's displayed title (frontmatter + H1) and re-render the header."""
    ni.set_note_title(filename, title)
    return _render_note_header(request, filename)


@app.post("/note/category", response_class=HTMLResponse)
def note_category(request: Request, filename: str = Form(...), category: str = Form("")):
    """Re-tag a general note's category (frontmatter only) and re-render the header."""
    if category.strip():
        ni.set_category(filename, category)
    return _render_note_header(request, filename)


def _render_note_header(request: Request, filename: str) -> HTMLResponse:
    path = corr.resolve_note(filename)
    meta = {}
    if path:
        meta, _ = ni._parse_frontmatter(open(path, encoding="utf-8").read())
    return templates.TemplateResponse(request, "_note_header.html", {
        "filename": filename, "title": meta.get("title", filename), "meta": meta,
    })


# ─── Someday: long-term ideas + the review queue ────────────────────────────

def _someday_ctx() -> dict:
    """Everything the Someday page (and its triage partial) needs: the review
    queue, parked ideas from meetings, and dedicated idea-session notes."""
    return {
        "nav": "someday",
        "review": ni.someday_items(state="review"),
        "parked": ni.someday_items(state="parked"),
        "ideas": ni.idea_notes(),
        **_reassign_ctx(),
    }


def _render_someday_body(request: Request) -> HTMLResponse:
    """Re-render just the page body after a triage action (htmx swap target)."""
    return templates.TemplateResponse(request, "_someday.html", _someday_ctx())


@app.get("/someday", response_class=HTMLResponse)
def someday(request: Request):
    return templates.TemplateResponse(request, "someday.html", _someday_ctx())


@app.post("/someday/confirm-idea", response_class=HTMLResponse)
def someday_confirm_idea(request: Request, filename: str = Form(...), line_no: int = Form(...)):
    """Keep a flagged item as a parked Someday idea."""
    ni.confirm_idea(filename, line_no)
    return _render_someday_body(request)


@app.post("/someday/confirm-todo", response_class=HTMLResponse)
def someday_confirm_todo(request: Request, filename: str = Form(...), line_no: int = Form(...),
                         owner: str = Form(""), deadline: str = Form("")):
    """Promote a flagged item into a live Action Center to-do (with owner/deadline)."""
    ni.confirm_todo(filename, line_no, owner=owner, deadline=deadline)
    return _render_someday_body(request)


@app.post("/someday/dismiss", response_class=HTMLResponse)
def someday_dismiss(request: Request, filename: str = Form(...), line_no: int = Form(...)):
    """Decline a flagged item — delete it from its note."""
    ni.dismiss_item(filename, line_no)
    return _render_someday_body(request)
