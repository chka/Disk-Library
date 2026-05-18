# Disk Library

A local web app for cataloging your offline disks: every file, every folder, SMART health, and movie/TV metadata. Built for people with shelves of external drives, dock-mounted spares, and the lingering question of "what's on that disk again?"

Plug a disk in, run one command, browse it forever — even after it's unplugged.

- **Stack:** Python 3 + Flask + SQLite (WAL) + Tailwind CSS + Alpine.js via CDN
- **No build step**, no Docker, no node, no daemon dependencies
- **Single SQLite file** is the entire database; portable, greppable, backupable
- **Local-only by default** — binds to `127.0.0.1`, no auth, intended for single-user use on your own machine

---

## Features

- **Catalog every file** on each disk: name, folder path, size, mtime, grouped by disk
- **SMART health capture** at scan time — power-on hours, load cycles, reallocated/pending/uncorrectable sectors, UDMA CRC, temperature, ATA error log; color-coded thresholds in the UI
- **Movie & TV metadata** via TMDb — title, year, IMDb ID, overview, poster, rating. Filenames parsed with [`guessit`](https://github.com/guessit-io/guessit). Series are tracked at the series level (one record per show, not per episode).
- **Incremental audit scans** — diff against the last scan, log every added / removed / modified file. Re-scan a disk and only the changes show up.
- **Scan history** — every scan recorded with before/after counts and the full file-level changeset
- **Duplicates finder** with three groups:
  - **A.** same filename + same size (exact duplicates)
  - **B.** same filename, different sizes (suspected re-encodes / partial copies)
  - **C.** similar filenames after normalization (strips resolution / codec / year / release-group tags; preserves episode markers like `S01E01` so different episodes don't collapse together)
  - Plus a separate **Series across disks** section for shows cataloged on multiple drives (after TMDb enrichment), shown at the series level — not per episode.
- **Deletion jobs**, never automatic — queue files to delete from the Duplicates page; the actual delete only runs when you explicitly invoke it on a job, and only if that disk is currently mounted. Path-traversal blocked, size mismatch detected, every action logged.
- **Inline disk rename** in the UI
- **Search** filenames across every cataloged disk
- **service control** via `start.sh` — start / stop / restart / status

## Who it's for

Anyone with five or more external drives and the lived experience of *"wait, did I already have this on the silver one or the blue one?"* — content hoarders, home-NAS people, retired-disk archivists.

What this is **not:**
- Not a backup tool. Cataloging is read-only by default. Deletion is opt-in, queued, and explicit.
- Not a duplicate auto-deleter. Every delete is a deliberate per-disk action.
- Not a media streamer. Browse the catalog only.

---

## Quickstart

```bash
git clone https://github.com/chka/Disk-Library.git
cd Disk-Library

# minimum to run the web UI + scanner (uses system Python 3.10+)
pip install --user flask

# optional: TMDb enrichment for movie/series metadata
pip install --user guessit requests
export TMDB_API_KEY=your_key_here   # free from themoviedb.org/settings/api

# start the service (binds 127.0.0.1:5050)
./start.sh start

# catalog your first disk
./scandisk.py /media/$USER/MyDrive "My Drive"

# pull TMDb metadata for that disk's video files (optional)
./enrich.py --disk-id 1

# browse
open http://127.0.0.1:5050/
```

`smartmontools` is recommended (`apt install smartmontools`) so SMART health gets captured. Without it, SMART columns stay empty but everything else works.

## Files

```
.
├── app.py              # Flask web UI (browse-only)
├── scandisk.py         # CLI: probe + audit-scan a disk into the catalog
├── enrich.py           # CLI: pull TMDb metadata for cataloged video files
├── smartcheck.py       # CLI: refresh SMART without re-scanning files
├── lib.py              # shared: schema, db(), probe_disk(), scan, SMART
├── start.sh            # service control (start/stop/restart/status)
└── templates/          # Tailwind + Alpine, no build step
```

The catalog lives in `disklib.db` (created on first run, WAL mode). Move it, back it up, drop it on another machine — it's just SQLite.

---

## CLI commands

### `scandisk.py` — index a disk

```bash
./scandisk.py <mount_point> [name]
./scandisk.py /media/chka/RedOrico "Red Orico"   # add a new disk
./scandisk.py /media/chka/RedOrico                # rescan existing (audit-mode diff)
./scandisk.py /media/chka/RedOrico "New Name" --rename   # rename + rescan
./scandisk.py /media/chka/RedOrico --full-rescan         # wipe + reindex
```

- Identifies the disk by its **drive serial** (via `lsblk`), so a disk keeps its catalog identity across remounts and even between machines.
- **Default mode is audit:** diffs the live filesystem against the catalog. Logs added / removed / modified files into a `scans` table. Preserves enrichment links on unchanged files.
- **Name conflict handling:** if the serial is already cataloged under a different name, prompts to keep the existing name or rename. `--keep` / `--rename` flags skip the prompt.
- Captures SMART data on every scan.

### `enrich.py` — pull TMDb metadata

```bash
./enrich.py                       # all unenriched video files, all disks
./enrich.py --disk-id 3           # just one disk
./enrich.py --dry-run             # parse filenames with guessit; no API calls
./enrich.py --rescan              # retry previously failed lookups
```

Parses filenames with `guessit` to identify movies vs episodes and extract title + year. One TMDb lookup per movie. One TMDb lookup **per series** (not per episode) — all episodes of one show share the same metadata record. Requires `TMDB_API_KEY` in the env.

### `smartcheck.py` — refresh SMART only

```bash
./smartcheck.py                   # all disks (skips ones not currently mounted)
./smartcheck.py --disk-id 3       # one disk
```

For when you want to check disk health without re-scanning files (which can be slow on large drives).

### `start.sh` — service control

```bash
./start.sh start | stop | restart | status
```

PID file: `disklib.pid`. Log: `disklib.log`. Detects externally-started processes via port-bind lookup, so won't double-start.

---

## Web UI

Single user, localhost-only, no auth. Navigation:

| Page | Path | What it shows |
|---|---|---|
| Home | `/` | Disk cards: name, model, serial, capacity bar, file count, scan status |
| Disk | `/disks/<id>` | SMART panel + folder browser with metadata snippets per video file. Inline rename. Remove-from-catalog button (drops the records, not the data) |
| Search | `/search?q=…` | Substring filename search across all cataloged disks (LIMIT 500) |
| Movies / Series | `/library?type=movie\|series` | TMDb poster grid with overview, rating, IMDb link, "on Disk X" attribution |
| Scans | `/scans` | Every scan run. Click into a scan for the per-file changeset (filter by added / removed / modified) |
| Duplicates | `/duplicates` | Groups A / B / C + Series-across-disks. Per-location trash icons, per-filename trash icon (toggles all locations), bottom-bar **Create Job** |
| Jobs | `/jobs` | Queued deletion jobs. Per-job detail at `/jobs/<id>` groups pending files by disk; "Run pending (N)" button appears only when that disk is currently mounted on this machine |

---

## How duplicates / jobs work

The Duplicates page identifies redundancy without ever touching files. You select which copies to remove via the trash icons (per location or per filename group), click **Create Job**, name it, done.

A **job is just a queue.** It does not run. It does not delete anything automatically. When you next mount a disk that has pending entries in a job, the job's detail page lights up with a red **Run pending (N)** button for that disk. Click → it deletes those files **on that disk only**, captures status per file (`done`, `missing`, `size_mismatch`, `error`), and removes the row from the catalog.

Safety layers when a job runs:
- The disk must be currently mounted (live `os.path.ismount` check)
- Each deletion path must resolve under the disk's mount point (path traversal blocked)
- File size on disk must match the cataloged size; mismatch → skip, mark `size_mismatch`
- File already missing → mark `missing`, remove catalog row, no error
- One-by-one, every action logged

---

## Architecture notes

- **Single host, single user.** The web UI binds to `127.0.0.1` only. There is no authentication, no multi-tenancy, no rate limiting. Don't expose it to a network you don't fully control.
- **SQLite with WAL** journal mode. Handles the typical "millions of files across a handful of disks" workload comfortably on a laptop. The `files` table grows fastest — index strategy is on `(disk_id, parent_dir)` and `(disk_id, filename)`.
- **Disks identified by drive serial.** Disk records survive remounts, mount-point changes, even moving the catalog to a new machine. The catalog references the disk; the disk doesn't have to be present.
- **SMART access** uses `sudo -n smartctl` first, then plain `smartctl` as fallback. If you want SMART without a sudo prompt during scans, configure passwordless sudo for `/usr/sbin/smartctl` in `/etc/sudoers.d/`.
- **No background workers.** Scans run synchronously in the CLI. The web UI is browse-only. This is intentional — no daemon state to manage, no queue to crash. Run scans from cron if you want them automatic.
- **Tailwind via CDN.** A warning appears in DevTools recommending the production build pipeline. For a single-user local app it doesn't matter; the warning is harmless. Swap to the Tailwind CLI standalone binary if you care to silence it.

---

## Contributing

Issues and PRs welcome. The codebase is intentionally small — one Flask file, one shared library, three CLIs, a handful of templates. No frameworks beyond Flask + SQLite, no ORM, no async, no dependency injection. If a change adds complexity, it probably doesn't belong here.

## License

MIT. See [LICENSE](LICENSE).
