#!/usr/bin/env python3
"""Enrich the disklib catalog with movie/series metadata from TMDb.

Requires:
  pip install guessit requests
  TMDB_API_KEY environment variable (free key from https://www.themoviedb.org/settings/api)

Usage:
  ./enrich.py                       # enrich all unlinked video files across all disks
  ./enrich.py --disk-id 3           # only files on disk id 3
  ./enrich.py --dry-run             # parse + look up, don't write to DB
  ./enrich.py --rescan              # also re-process files that previously failed
"""
import argparse
import os
import re
import sys
import time

try:
    from guessit import guessit
except ImportError:
    sys.exit("missing dependency: pip install guessit")
try:
    import requests
except ImportError:
    sys.exit("missing dependency: pip install requests")

from lib import db, init_db, is_video

TMDB = "https://api.themoviedb.org/3"
POSTER_BASE = "https://image.tmdb.org/t/p/w342"
API_KEY = os.environ.get("TMDB_API_KEY")
SESSION = requests.Session()
SESSION.headers["Accept"] = "application/json"

_CACHE = {}


def tmdb_search(kind, title, year=None):
    if not API_KEY:
        raise RuntimeError("set TMDB_API_KEY env var (https://www.themoviedb.org/settings/api)")
    key = (kind, title.lower(), year)
    if key in _CACHE:
        return _CACHE[key]
    endpoint = "movie" if kind == "movie" else "tv"
    params = {"api_key": API_KEY, "query": title, "include_adult": "false", "language": "en-US"}
    if year and kind == "movie":
        params["year"] = year
    elif year and kind == "tv":
        params["first_air_date_year"] = year
    try:
        r = SESSION.get(f"{TMDB}/search/{endpoint}", params=params, timeout=10)
        r.raise_for_status()
        results = r.json().get("results", [])
    except (requests.RequestException, ValueError) as e:
        _CACHE[key] = None
        return None
    if not results:
        if year:
            params.pop("year", None)
            params.pop("first_air_date_year", None)
            try:
                r = SESSION.get(f"{TMDB}/search/{endpoint}", params=params, timeout=10)
                r.raise_for_status()
                results = r.json().get("results", [])
            except (requests.RequestException, ValueError):
                results = []
    if not results:
        _CACHE[key] = None
        return None
    hit = results[0]
    detail = None
    try:
        r = SESSION.get(f"{TMDB}/{endpoint}/{hit['id']}",
                        params={"api_key": API_KEY, "language": "en-US",
                                "append_to_response": "external_ids"}, timeout=10)
        r.raise_for_status()
        detail = r.json()
    except (requests.RequestException, ValueError):
        detail = hit
    record = {
        "type": "movie" if kind == "movie" else "series",
        "title": detail.get("title") or detail.get("name"),
        "year": _parse_year(detail.get("release_date") or detail.get("first_air_date")),
        "tmdb_id": detail.get("id"),
        "imdb_id": (detail.get("external_ids") or {}).get("imdb_id"),
        "overview": detail.get("overview"),
        "poster_url": POSTER_BASE + detail["poster_path"] if detail.get("poster_path") else None,
        "rating": detail.get("vote_average"),
    }
    _CACHE[key] = record
    return record


def _parse_year(s):
    if not s:
        return None
    m = re.match(r"(\d{4})", s)
    return int(m.group(1)) if m else None


def upsert_metadata(conn, rec):
    if rec.get("tmdb_id"):
        row = conn.execute(
            "SELECT id FROM metadata WHERE type=? AND tmdb_id=?",
            (rec["type"], rec["tmdb_id"]),
        ).fetchone()
        if row:
            conn.execute(
                """UPDATE metadata SET title=?, year=?, imdb_id=?, overview=?,
                   poster_url=?, rating=?, fetched_at=datetime('now') WHERE id=?""",
                (rec["title"], rec["year"], rec["imdb_id"], rec["overview"],
                 rec["poster_url"], rec["rating"], row["id"]),
            )
            return row["id"]
    cur = conn.execute(
        """INSERT INTO metadata (type, title, year, tmdb_id, imdb_id, overview, poster_url, rating)
           VALUES (?,?,?,?,?,?,?,?)""",
        (rec["type"], rec["title"], rec["year"], rec.get("tmdb_id"),
         rec.get("imdb_id"), rec.get("overview"), rec.get("poster_url"), rec.get("rating")),
    )
    return cur.lastrowid


def classify(filename):
    g = guessit(filename)
    if g.get("type") == "episode" and g.get("title"):
        return "tv", str(g["title"]), g.get("year")
    if g.get("type") == "movie" and g.get("title"):
        return "movie", str(g["title"]), g.get("year")
    return None, None, None


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--disk-id", type=int)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--rescan", action="store_true",
                   help="also re-process files previously marked 'no_match'/'error'")
    p.add_argument("--limit", type=int, help="cap the number of files processed")
    args = p.parse_args()

    if not API_KEY and not args.dry_run:
        sys.exit("set TMDB_API_KEY env var (https://www.themoviedb.org/settings/api)")

    init_db()
    with db() as conn:
        where = ["metadata_id IS NULL"]
        if not args.rescan:
            where.append("(enrich_status IS NULL OR enrich_status = 'pending')")
        params = []
        if args.disk_id:
            where.append("disk_id = ?")
            params.append(args.disk_id)
        sql = f"SELECT id, disk_id, filename FROM files WHERE {' AND '.join(where)}"
        if args.limit:
            sql += f" LIMIT {int(args.limit)}"
        rows = conn.execute(sql, params).fetchall()

    candidates = [r for r in rows if is_video(r["filename"])]
    print(f"Found {len(candidates):,} video files needing metadata (of {len(rows):,} unenriched files)")
    if not candidates:
        return

    matched = no_match = errors = 0
    t0 = time.time()
    with db() as conn:
        for i, row in enumerate(candidates, 1):
            kind, title, year = classify(row["filename"])
            if not kind:
                conn.execute(
                    "UPDATE files SET enrich_status='no_parse' WHERE id=?", (row["id"],)
                )
                no_match += 1
                continue
            try:
                if args.dry_run:
                    print(f"  [DRY] {kind:>5} | {title} ({year or '?'}) ← {row['filename']}")
                    continue
                rec = tmdb_search(kind, title, year)
            except Exception as e:
                conn.execute(
                    "UPDATE files SET enrich_status=? WHERE id=?",
                    (f"error:{e}"[:200], row["id"]),
                )
                errors += 1
                continue
            if not rec:
                conn.execute(
                    "UPDATE files SET enrich_status='no_match' WHERE id=?", (row["id"],)
                )
                no_match += 1
                continue
            mid = upsert_metadata(conn, rec)
            conn.execute(
                "UPDATE files SET metadata_id=?, enrich_status='ok' WHERE id=?",
                (mid, row["id"]),
            )
            matched += 1
            if i % 25 == 0:
                conn.commit()
                rate = i / max(time.time() - t0, 0.01)
                print(f"  {i:>5}/{len(candidates)}  matched={matched} no_match={no_match} err={errors}  ({rate:.1f}/s)",
                      flush=True)
            time.sleep(0.05)  # gentle on TMDb
        conn.commit()

    print(f"\nDone. matched={matched}  no_match={no_match}  errors={errors}  cache_hits={sum(1 for v in _CACHE.values() if v)}")


if __name__ == "__main__":
    main()
