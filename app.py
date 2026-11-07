#!/usr/bin/env python3
"""Web UI for the disklib catalog. Browse-only: scanning happens via scandisk.py."""
import json
import os
import re
from collections import defaultdict
from datetime import datetime
from urllib.parse import quote, unquote

from flask import Flask, abort, jsonify, redirect, render_template, request, url_for

from lib import (SMART_BAD_IF_RISING, db, fmt_bytes, fmt_mtime, init_db,
                 reserved_advice, smart_attributes)

app = Flask(__name__)
app.jinja_env.filters["bytes"] = fmt_bytes
app.jinja_env.filters["mtime"] = fmt_mtime
app.jinja_env.filters["urlencode"] = lambda s: quote(s, safe="")
app.jinja_env.globals["reserved_advice"] = reserved_advice


@app.route("/")
def index():
    with db() as conn:
        disks = conn.execute(
            "SELECT * FROM disks ORDER BY name COLLATE NOCASE"
        ).fetchall()
    return render_template("index.html", disks=disks)


@app.route("/disks/<int:disk_id>")
def view_disk(disk_id):
    path = unquote(request.args.get("path", ""))
    with db() as conn:
        disk = conn.execute("SELECT * FROM disks WHERE id=?", (disk_id,)).fetchone()
        if not disk:
            abort(404)
        files = conn.execute(
            """SELECT f.filename, f.size_bytes, f.mtime, f.enrich_status,
                      m.type AS meta_type, m.title AS meta_title, m.year AS meta_year,
                      m.overview AS meta_overview, m.poster_url AS meta_poster,
                      m.rating AS meta_rating, m.imdb_id AS meta_imdb
               FROM files f LEFT JOIN metadata m ON m.id = f.metadata_id
               WHERE f.disk_id=? AND f.parent_dir=?
               ORDER BY f.filename COLLATE NOCASE""",
            (disk_id, path),
        ).fetchall()
        prefix = path + "/" if path else ""
        rows = conn.execute(
            """SELECT DISTINCT parent_dir FROM files
               WHERE disk_id=? AND parent_dir LIKE ? AND parent_dir != ?""",
            (disk_id, prefix + "%", path),
        ).fetchall()
        # If the current folder (or anything inside it) holds episodes of a
        # single series, show that series card once at the top instead of
        # repeating TMDb info next to every episode row.
        if path:
            series_q = conn.execute(
                """SELECT m.id, m.title, m.year, m.overview, m.poster_url,
                          m.rating, m.imdb_id, COUNT(*) AS ep_count
                   FROM files f JOIN metadata m ON m.id = f.metadata_id
                   WHERE f.disk_id=? AND (f.parent_dir=? OR f.parent_dir LIKE ?)
                         AND m.type='series'
                   GROUP BY m.id ORDER BY ep_count DESC LIMIT 1""",
                (disk_id, path, prefix + "%"),
            ).fetchone()
        else:
            series_q = conn.execute(
                """SELECT m.id, m.title, m.year, m.overview, m.poster_url,
                          m.rating, m.imdb_id, COUNT(*) AS ep_count
                   FROM files f JOIN metadata m ON m.id = f.metadata_id
                   WHERE f.disk_id=? AND f.parent_dir='' AND m.type='series'
                   GROUP BY m.id ORDER BY ep_count DESC LIMIT 1""",
                (disk_id,),
            ).fetchone()
        folder_series = dict(series_q) if series_q else None
    subfolders = sorted(
        {r["parent_dir"][len(prefix):].split("/", 1)[0] for r in rows if r["parent_dir"][len(prefix):]},
        key=str.lower,
    )
    crumbs = []
    if path:
        accum = []
        for part in path.split("/"):
            accum.append(part)
            crumbs.append({"name": part, "path": "/".join(accum)})
    return render_template(
        "disk.html",
        disk=disk,
        path=path,
        files=files,
        subfolders=subfolders,
        crumbs=crumbs,
        folder_series=folder_series,
    )


SMART_COUNTERS = [
    # (column, label, bad_if_rising)
    ("power_on_hours", "Power-on h", False),
    ("power_cycles", "Cycles", False),
    ("load_cycles", "Load cycles", False),
    ("temp_c", "Temp °C", False),
    ("reallocated", "Reallocated", True),
    ("pending", "Pending", True),
    ("uncorrectable", "Uncorrectable", True),
    ("udma_crc", "UDMA CRC", True),
    ("ata_errors", "ATA errors", True),
]


@app.route("/disks/<int:disk_id>/smart")
def disk_smart_history(disk_id):
    with db() as conn:
        disk = conn.execute("SELECT * FROM disks WHERE id=?", (disk_id,)).fetchone()
        if not disk:
            abort(404)
        history = [dict(r) for r in conn.execute(
            "SELECT * FROM smart_history WHERE disk_id=? ORDER BY checked_at DESC, id DESC",
            (disk_id,),
        )]
    # Delta of each counter against the next-older snapshot that has a value.
    for i, h in enumerate(history):
        h["delta"] = {}
        older = next((o for o in history[i + 1:] if o["available"]), None)
        if h["available"] and older:
            for col, _, _ in SMART_COUNTERS:
                if h[col] is not None and older[col] is not None:
                    h["delta"][col] = h[col] - older[col]

    usable = [h for h in history if h["available"]]
    by_id = {h["id"]: h for h in usable}
    new = by_id.get(request.args.get("a", type=int)) or (usable[0] if usable else None)
    base = by_id.get(request.args.get("b", type=int))
    if base is None and new is not None:
        base = next((h for h in usable if h["checked_at"] < new["checked_at"]
                     or (h["checked_at"] == new["checked_at"] and h["id"] < new["id"])), None)

    compare = []
    if new:
        na = smart_attributes(new["raw_json"])
        ba = smart_attributes(base["raw_json"]) if base else {}
        for aid in sorted(set(na) | set(ba)):
            n, b = na.get(aid), ba.get(aid)
            row = {"id": aid, "name": (n or b)["name"], "new": n, "base": b, "delta": None,
                   "level": ""}
            if n and b and isinstance(n["raw"], int) and isinstance(b["raw"], int):
                row["delta"] = n["raw"] - b["raw"]
            if n and n["thresh"] and n["value"] is not None and n["value"] <= n["thresh"]:
                row["level"] = "fail"
            elif aid in SMART_BAD_IF_RISING and (row["delta"] or 0) > 0:
                row["level"] = "worse"
            elif n and n["when_failed"]:
                row["level"] = "past"
            compare.append(row)

    return render_template(
        "smart_history.html", disk=disk, history=history, counters=SMART_COUNTERS,
        new=new, base=base, compare=compare,
    )


@app.route("/disks/<int:disk_id>/status")
def disk_status(disk_id):
    with db() as conn:
        d = conn.execute(
            "SELECT scan_status, scan_progress, scan_error, file_count FROM disks WHERE id=?",
            (disk_id,),
        ).fetchone()
    if not d:
        return jsonify({"error": "not found"}), 404
    return jsonify(dict(d))


@app.route("/disks/<int:disk_id>/delete", methods=["POST"])
def delete_disk(disk_id):
    """Permanently remove a disk: files (cascade), scans (cascade), scan_changes
    (cascade), job_files for this disk (manual — FK lacks ON DELETE), now-empty
    jobs, and orphaned metadata rows. For lost / sold / dead disks."""
    confirm = (request.form.get("confirm_name") or "").strip()
    with db() as conn:
        disk = conn.execute("SELECT id, name FROM disks WHERE id=?", (disk_id,)).fetchone()
        if not disk:
            abort(404)
        if confirm != disk["name"]:
            return jsonify({"error": "confirm_name must match the disk name exactly"}), 400
        conn.execute("BEGIN")
        try:
            conn.execute("DELETE FROM job_files WHERE disk_id=?", (disk_id,))
            conn.execute("""DELETE FROM jobs WHERE id NOT IN
                            (SELECT DISTINCT job_id FROM job_files)""")
            conn.execute("DELETE FROM disks WHERE id=?", (disk_id,))
            conn.execute("""DELETE FROM metadata WHERE id NOT IN
                            (SELECT metadata_id FROM files WHERE metadata_id IS NOT NULL)""")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return redirect(url_for("index"))


@app.route("/disks/<int:disk_id>/rename", methods=["POST"])
def rename_disk(disk_id):
    new_name = (request.form.get("name") or "").strip()
    if not new_name:
        return jsonify({"error": "name cannot be empty"}), 400
    if len(new_name) > 200:
        return jsonify({"error": "name too long"}), 400
    with db() as conn:
        cur = conn.execute("UPDATE disks SET name=? WHERE id=?", (new_name, disk_id))
        conn.commit()
        if cur.rowcount == 0:
            return jsonify({"error": "disk not found"}), 404
    if request.headers.get("Accept", "").startswith("application/json"):
        return jsonify({"ok": True, "name": new_name})
    return redirect(url_for("view_disk", disk_id=disk_id))


@app.route("/scans")
def scans_index():
    with db() as conn:
        scans = conn.execute(
            """SELECT s.*, d.name AS disk_name FROM scans s
               JOIN disks d ON d.id = s.disk_id
               ORDER BY s.started_at DESC LIMIT 200"""
        ).fetchall()
    return render_template("scans.html", scans=scans)


@app.route("/scans/<int:scan_id>")
def scan_detail(scan_id):
    change_type = request.args.get("type", "all")
    with db() as conn:
        s = conn.execute(
            """SELECT s.*, d.name AS disk_name FROM scans s
               JOIN disks d ON d.id = s.disk_id WHERE s.id=?""",
            (scan_id,),
        ).fetchone()
        if not s:
            abort(404)
        where = "scan_id=?"
        params = [scan_id]
        if change_type in ("added", "removed", "modified"):
            where += " AND change_type=?"
            params.append(change_type)
        changes = conn.execute(
            f"""SELECT * FROM scan_changes WHERE {where}
                ORDER BY change_type, parent_dir, filename LIMIT 2000""",
            params,
        ).fetchall()
    return render_template("scan_detail.html", scan=s, changes=changes,
                           change_type=change_type)


from lib import VIDEO_EXT  # noqa: E402

_DUPE_EXTS = sorted(VIDEO_EXT)
_DUPE_EXT_SQL = "(" + " OR ".join(
    f"LOWER(f.filename) LIKE '%{ext}'" for ext in _DUPE_EXTS
) + ")"


_TAG_RE = re.compile(
    r"\b(1080p|720p|480p|2160p|4k|uhd|hdr|sdr|10bit|8bit|"
    r"x264|x265|h\.?264|h\.?265|hevc|avc|aac|ac3|dts|truehd|atmos|opus|"
    r"bluray|brrip|web[-\.]?dl|web[-\.]?rip|hdtv|hdrip|dvdrip|dvd|cam|ts|tc|"
    r"xvid|divx|amzn|nf|hulu|atvp|dsnp|hmax|max|"
    r"repack|proper|extended|directors?\.cut|remux|imax|unrated|limited|internal|"
    r"multi|dual|dubbed|subbed)\b",
    re.IGNORECASE,
)


_EPISODE_RE = re.compile(r"\b(s\d{1,2}e\d{1,3}|\d{1,2}x\d{1,3})\b", re.IGNORECASE)


def _normalize_name(name):
    n = re.sub(r"\.[a-z0-9]{2,4}$", "", name, flags=re.IGNORECASE)
    # preserve episode marker so different episodes don't collapse together
    ep_match = _EPISODE_RE.search(n)
    ep_token = ep_match.group(0).lower() if ep_match else None
    if ep_token:
        n = _EPISODE_RE.sub(" ", n, count=1)
    n = re.sub(r"[\(\[]?\b(19|20)\d{2}\b[\)\]]?", "", n)
    n = _TAG_RE.sub("", n)
    n = re.sub(r"[-_\.\s]+[a-z0-9]{2,12}$", "", n, flags=re.IGNORECASE)
    n = re.sub(r"[\._\-\s\[\]\(\){}]+", " ", n).strip().lower()
    if ep_token:
        n = (n + " " + ep_token).strip()
    return n


def _common_prefix(paths):
    if not paths:
        return ""
    split = [p.split("/") for p in paths]
    out = []
    for parts in zip(*split):
        if len(set(parts)) == 1:
            out.append(parts[0])
        else:
            break
    return "/".join(out)


@app.route("/duplicates")
def duplicates():
    with db() as conn:
        # ===== Movies, Group A: same filename + same size =====
        exact_groups = conn.execute(f"""
            SELECT f.filename, f.size_bytes
            FROM files f LEFT JOIN metadata m ON m.id = f.metadata_id
            WHERE (m.type IS NULL OR m.type = 'movie') AND f.size_bytes > 0
              AND {_DUPE_EXT_SQL}
            GROUP BY f.filename, f.size_bytes
            HAVING COUNT(*) > 1
            ORDER BY f.filename COLLATE NOCASE
        """).fetchall()
        exact_movies = []
        for g in exact_groups:
            inst = conn.execute("""
                SELECT f.filename, f.parent_dir, f.size_bytes, f.mtime,
                       d.id AS disk_id, d.name AS disk_name,
                       m.title AS meta_title, m.year AS meta_year
                FROM files f
                JOIN disks d ON d.id = f.disk_id
                LEFT JOIN metadata m ON m.id = f.metadata_id
                WHERE f.filename = ? AND f.size_bytes = ?
                ORDER BY d.name COLLATE NOCASE, f.parent_dir
            """, (g["filename"], g["size_bytes"])).fetchall()
            exact_movies.append({
                "filename": g["filename"],
                "size_bytes": g["size_bytes"],
                "instances": [dict(i) for i in inst],
                "meta_title": inst[0]["meta_title"] if inst else None,
                "meta_year": inst[0]["meta_year"] if inst else None,
            })

        # ===== Movies, Group B: same filename, different sizes =====
        samename_groups = conn.execute(f"""
            SELECT f.filename, COUNT(DISTINCT f.size_bytes) AS variants
            FROM files f LEFT JOIN metadata m ON m.id = f.metadata_id
            WHERE (m.type IS NULL OR m.type = 'movie') AND f.size_bytes > 0
              AND {_DUPE_EXT_SQL}
            GROUP BY f.filename
            HAVING variants > 1
            ORDER BY f.filename COLLATE NOCASE
        """).fetchall()
        samename_movies = []
        for g in samename_groups:
            inst = conn.execute("""
                SELECT f.filename, f.parent_dir, f.size_bytes, f.mtime,
                       d.id AS disk_id, d.name AS disk_name,
                       m.title AS meta_title, m.year AS meta_year
                FROM files f
                JOIN disks d ON d.id = f.disk_id
                LEFT JOIN metadata m ON m.id = f.metadata_id
                WHERE f.filename = ?
                ORDER BY d.name COLLATE NOCASE, f.size_bytes
            """, (g["filename"],)).fetchall()
            samename_movies.append({
                "filename": g["filename"],
                "instances": [dict(i) for i in inst],
                "meta_title": inst[0]["meta_title"] if inst else None,
                "meta_year": inst[0]["meta_year"] if inst else None,
            })

        # ===== Movies, Group C: similar filename (normalized fuzzy) =====
        all_movies = conn.execute(f"""
            SELECT DISTINCT f.filename
            FROM files f LEFT JOIN metadata m ON m.id = f.metadata_id
            WHERE (m.type IS NULL OR m.type = 'movie') AND f.size_bytes > 0
              AND {_DUPE_EXT_SQL}
        """).fetchall()
        norm_map = defaultdict(set)
        for r in all_movies:
            n = _normalize_name(r["filename"])
            if len(n) >= 4:
                norm_map[n].add(r["filename"])
        similar_movies = []
        for norm, fnames in norm_map.items():
            if len(fnames) < 2:
                continue
            fnames = sorted(fnames)
            placeholders = ",".join(["?"] * len(fnames))
            inst = conn.execute(f"""
                SELECT f.filename, f.parent_dir, f.size_bytes, f.mtime,
                       d.id AS disk_id, d.name AS disk_name,
                       m.title AS meta_title, m.year AS meta_year
                FROM files f
                JOIN disks d ON d.id = f.disk_id
                LEFT JOIN metadata m ON m.id = f.metadata_id
                WHERE f.filename IN ({placeholders})
                ORDER BY f.filename COLLATE NOCASE, d.name COLLATE NOCASE
            """, fnames).fetchall()
            similar_movies.append({
                "normalized": norm,
                "filenames": fnames,
                "instances": [dict(i) for i in inst],
                "meta_title": inst[0]["meta_title"] if inst else None,
                "meta_year": inst[0]["meta_year"] if inst else None,
            })
        similar_movies.sort(key=lambda x: x["normalized"])

        # ===== Series: a series appears on 2+ disks =====
        series_rows = conn.execute("""
            SELECT m.id, m.title, m.year, m.poster_url, m.overview, m.rating, m.imdb_id,
                   COUNT(DISTINCT f.disk_id) AS disk_count,
                   COUNT(f.id) AS file_count
            FROM metadata m
            JOIN files f ON f.metadata_id = m.id
            WHERE m.type = 'series'
            GROUP BY m.id
            HAVING disk_count > 1
            ORDER BY m.title COLLATE NOCASE
        """).fetchall()
        series_dupes = []
        for s in series_rows:
            paths_per_disk = conn.execute("""
                SELECT d.id AS disk_id, d.name AS disk_name, f.parent_dir, COUNT(f.id) AS file_count
                FROM files f
                JOIN disks d ON d.id = f.disk_id
                WHERE f.metadata_id = ?
                GROUP BY d.id, f.parent_dir
                ORDER BY d.name COLLATE NOCASE, f.parent_dir
            """, (s["id"],)).fetchall()
            by_disk = defaultdict(list)
            files_by_disk = defaultdict(int)
            for r in paths_per_disk:
                by_disk[(r["disk_id"], r["disk_name"])].append(r["parent_dir"])
                files_by_disk[(r["disk_id"], r["disk_name"])] += r["file_count"]
            disk_locs = []
            for key, paths in by_disk.items():
                disk_locs.append({
                    "disk_id": key[0],
                    "disk_name": key[1],
                    "root_folder": _common_prefix(paths) or "(root)",
                    "file_count": files_by_disk[key],
                })
            disk_locs.sort(key=lambda x: x["disk_name"].lower())
            series_dupes.append({"series": dict(s), "disks": disk_locs})

    return render_template(
        "duplicates.html",
        exact_movies=exact_movies,
        samename_movies=samename_movies,
        similar_movies=similar_movies,
        series_dupes=series_dupes,
    )


def _disk_mounted(mount_point):
    if not mount_point or not os.path.isdir(mount_point):
        return False
    return os.path.ismount(mount_point) or any(
        os.path.commonpath([mount_point, os.path.abspath(mount_point)]) == mount_point
        for _ in [1]
    )


@app.route("/jobs/create", methods=["POST"])
def jobs_create():
    items_raw = request.form.getlist("items")
    name = (request.form.get("name") or "").strip()
    if not items_raw:
        return jsonify({"error": "no items selected"}), 400
    if not name:
        name = f"Cleanup {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    parsed = []
    for raw in items_raw:
        try:
            o = json.loads(raw)
            parsed.append((int(o["d"]), str(o.get("p", "")), str(o["f"])))
        except (ValueError, KeyError, TypeError):
            return jsonify({"error": f"invalid item: {raw[:80]}"}), 400
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO jobs (name, kind, status) VALUES (?, 'delete', 'pending')",
            (name,),
        )
        job_id = cur.lastrowid
        rows = []
        for disk_id, parent_dir, filename in parsed:
            r = conn.execute(
                """SELECT size_bytes, mtime FROM files
                   WHERE disk_id=? AND parent_dir=? AND filename=?""",
                (disk_id, parent_dir, filename),
            ).fetchone()
            rows.append((
                job_id, disk_id, parent_dir, filename,
                r["size_bytes"] if r else None,
                r["mtime"] if r else None,
            ))
        conn.executemany(
            """INSERT INTO job_files
               (job_id, disk_id, parent_dir, filename, expected_size, expected_mtime, status)
               VALUES (?, ?, ?, ?, ?, ?, 'pending')""",
            rows,
        )
        conn.commit()
    return jsonify({"id": job_id, "name": name, "files": len(rows)})


@app.route("/jobs")
def jobs_index():
    with db() as conn:
        jobs = conn.execute(
            """SELECT j.*,
                      (SELECT COUNT(*) FROM job_files WHERE job_id=j.id) AS total,
                      (SELECT COUNT(*) FROM job_files WHERE job_id=j.id AND status='pending') AS pending,
                      (SELECT COUNT(*) FROM job_files WHERE job_id=j.id AND status='done') AS done,
                      (SELECT COUNT(*) FROM job_files WHERE job_id=j.id AND status NOT IN ('pending','done')) AS other
               FROM jobs j ORDER BY j.created_at DESC LIMIT 200"""
        ).fetchall()
    return render_template("jobs.html", jobs=jobs)


@app.route("/jobs/<int:job_id>")
def job_detail(job_id):
    with db() as conn:
        job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not job:
            abort(404)
        files = conn.execute(
            """SELECT jf.*, d.name AS disk_name, d.mount_point, d.serial
               FROM job_files jf JOIN disks d ON d.id = jf.disk_id
               WHERE jf.job_id=?
               ORDER BY d.name COLLATE NOCASE, jf.parent_dir, jf.filename""",
            (job_id,),
        ).fetchall()
    by_disk = defaultdict(list)
    for f in files:
        key = (f["disk_id"], f["disk_name"], f["mount_point"], f["serial"])
        by_disk[key].append(dict(f))
    disks = []
    for (did, dname, mount, serial), entries in by_disk.items():
        mounted = _disk_mounted(mount)
        pending = sum(1 for it in entries if it["status"] == "pending")
        done = sum(1 for it in entries if it["status"] == "done")
        other = sum(1 for it in entries if it["status"] not in ("pending", "done"))
        disks.append({
            "disk_id": did, "disk_name": dname, "mount_point": mount, "serial": serial,
            "mounted": mounted, "files": entries,
            "pending": pending, "done": done, "other": other,
        })
    disks.sort(key=lambda x: x["disk_name"].lower())
    return render_template("job_detail.html", job=job, disks=disks)


@app.route("/jobs/<int:job_id>/run", methods=["POST"])
def job_run(job_id):
    disk_id = request.form.get("disk_id", type=int)
    if not disk_id:
        return jsonify({"error": "disk_id required"}), 400
    with db() as conn:
        disk = conn.execute("SELECT * FROM disks WHERE id=?", (disk_id,)).fetchone()
        if not disk:
            return jsonify({"error": "disk not in catalog"}), 404
    mount_point = disk["mount_point"]
    if not _disk_mounted(mount_point):
        return jsonify({"error": f"disk not mounted at {mount_point!r}"}), 400
    mount_real = os.path.realpath(mount_point)

    with db() as conn:
        pending = conn.execute(
            """SELECT * FROM job_files
               WHERE job_id=? AND disk_id=? AND status='pending'""",
            (job_id, disk_id),
        ).fetchall()

    done = missing = mismatch = errors = 0
    for f in pending:
        full = os.path.realpath(os.path.join(mount_point, f["parent_dir"], f["filename"]))
        if not (full == mount_real or full.startswith(mount_real + os.sep)):
            with db() as conn:
                conn.execute(
                    """UPDATE job_files SET status='error', executed_at=datetime('now'), error=?
                       WHERE id=?""",
                    ("path traversal blocked", f["id"]),
                )
                conn.commit()
            errors += 1
            continue
        try:
            if not os.path.lexists(full):
                with db() as conn:
                    conn.execute(
                        """UPDATE job_files SET status='missing', executed_at=datetime('now')
                           WHERE id=?""", (f["id"],),
                    )
                    conn.execute(
                        """DELETE FROM files
                           WHERE disk_id=? AND parent_dir=? AND filename=?""",
                        (f["disk_id"], f["parent_dir"], f["filename"]),
                    )
                    conn.commit()
                missing += 1
                continue
            if f["expected_size"] is not None:
                st = os.stat(full)
                if st.st_size != f["expected_size"]:
                    with db() as conn:
                        conn.execute(
                            """UPDATE job_files SET status='size_mismatch',
                               executed_at=datetime('now'), error=? WHERE id=?""",
                            (f"expected {f['expected_size']}, found {st.st_size}", f["id"]),
                        )
                        conn.commit()
                    mismatch += 1
                    continue
            os.remove(full)
            with db() as conn:
                conn.execute(
                    """DELETE FROM files
                       WHERE disk_id=? AND parent_dir=? AND filename=?""",
                    (f["disk_id"], f["parent_dir"], f["filename"]),
                )
                conn.execute(
                    """UPDATE job_files SET status='done', executed_at=datetime('now'), error=NULL
                       WHERE id=?""", (f["id"],),
                )
                conn.commit()
            done += 1
        except OSError as e:
            with db() as conn:
                conn.execute(
                    """UPDATE job_files SET status='error', executed_at=datetime('now'), error=?
                       WHERE id=?""", (str(e)[:200], f["id"]),
                )
                conn.commit()
            errors += 1

    with db() as conn:
        remaining = conn.execute(
            "SELECT COUNT(*) FROM job_files WHERE job_id=? AND status='pending'", (job_id,),
        ).fetchone()[0]
        total = conn.execute(
            "SELECT COUNT(*) FROM job_files WHERE job_id=?", (job_id,),
        ).fetchone()[0]
        done_total = conn.execute(
            "SELECT COUNT(*) FROM job_files WHERE job_id=? AND status='done'", (job_id,),
        ).fetchone()[0]
        if remaining == 0:
            new_status = "done" if done_total == total else "partial"
        else:
            new_status = "pending" if done_total == 0 else "partial"
        conn.execute("UPDATE jobs SET status=? WHERE id=?", (new_status, job_id))
        conn.commit()

    return jsonify({
        "done": done, "missing": missing, "mismatch": mismatch, "errors": errors,
        "remaining": remaining,
    })


@app.route("/jobs/<int:job_id>/delete", methods=["POST"])
def job_delete(job_id):
    with db() as conn:
        conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))
        conn.commit()
    return redirect(url_for("jobs_index"))


@app.route("/library")
def library():
    kind = request.args.get("type", "movie")
    if kind not in ("movie", "series"):
        kind = "movie"
    with db() as conn:
        items = conn.execute(
            """SELECT m.*, COUNT(f.id) AS file_count,
                      GROUP_CONCAT(DISTINCT d.name) AS disk_names
               FROM metadata m
               JOIN files f ON f.metadata_id = m.id
               JOIN disks d ON d.id = f.disk_id
               WHERE m.type = ?
               GROUP BY m.id
               ORDER BY m.title COLLATE NOCASE""",
            (kind,),
        ).fetchall()
    return render_template("library.html", items=items, kind=kind)


@app.route("/search")
def search():
    q = request.args.get("q", "").strip()
    disk_id = request.args.get("disk_id", type=int)
    results = []
    if q:
        with db() as conn:
            if disk_id:
                results = conn.execute(
                    """SELECT f.*, d.name AS disk_name FROM files f
                       JOIN disks d ON d.id=f.disk_id
                       WHERE f.disk_id=? AND f.filename LIKE ?
                       ORDER BY f.filename COLLATE NOCASE LIMIT 500""",
                    (disk_id, f"%{q}%"),
                ).fetchall()
            else:
                results = conn.execute(
                    """SELECT f.*, d.name AS disk_name FROM files f
                       JOIN disks d ON d.id=f.disk_id
                       WHERE f.filename LIKE ?
                       ORDER BY f.filename COLLATE NOCASE LIMIT 500""",
                    (f"%{q}%",),
                ).fetchall()
    return render_template("search.html", q=q, results=results, disk_id=disk_id)


if __name__ == "__main__":
    init_db()
    app.run(host="127.0.0.1", port=5050, debug=False)
