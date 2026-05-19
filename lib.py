import json
import os
import sqlite3
import stat as _stat
import subprocess
from datetime import datetime
from pathlib import Path

APP_DIR = Path(__file__).parent.resolve()
DB_PATH = APP_DIR / "disklib.db"
SCAN_BATCH = 2000


def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with db() as conn:
        conn.executescript("""
            PRAGMA journal_mode = WAL;
            CREATE TABLE IF NOT EXISTS disks (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              name TEXT NOT NULL,
              device TEXT,
              model TEXT,
              serial TEXT UNIQUE,
              fs_type TEXT,
              mount_point TEXT,
              total_bytes INTEGER,
              free_bytes INTEGER,
              used_bytes INTEGER,
              file_count INTEGER DEFAULT 0,
              dir_count INTEGER DEFAULT 0,
              scanned_at TEXT,
              scan_status TEXT DEFAULT 'pending',
              scan_progress INTEGER DEFAULT 0,
              scan_error TEXT,
              created_at TEXT DEFAULT CURRENT_TIMESTAMP,
              smart_available INTEGER DEFAULT 0,
              smart_overall TEXT,
              smart_firmware TEXT,
              smart_rotation_rpm INTEGER,
              smart_power_on_hours INTEGER,
              smart_power_cycles INTEGER,
              smart_load_cycles INTEGER,
              smart_temp_c INTEGER,
              smart_reallocated INTEGER,
              smart_pending INTEGER,
              smart_uncorrectable INTEGER,
              smart_udma_crc INTEGER,
              smart_ata_errors INTEGER,
              smart_error TEXT,
              smart_checked_at TEXT,
              smart_raw_json TEXT
            );
            CREATE TABLE IF NOT EXISTS files (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              disk_id INTEGER NOT NULL REFERENCES disks(id) ON DELETE CASCADE,
              parent_dir TEXT NOT NULL,
              filename TEXT NOT NULL,
              size_bytes INTEGER,
              mtime REAL,
              metadata_id INTEGER REFERENCES metadata(id),
              enrich_status TEXT DEFAULT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_files_disk_parent ON files(disk_id, parent_dir);
            CREATE INDEX IF NOT EXISTS idx_files_name ON files(filename);
            CREATE INDEX IF NOT EXISTS idx_files_disk_name ON files(disk_id, filename);
            CREATE INDEX IF NOT EXISTS idx_files_metadata ON files(metadata_id);
            CREATE TABLE IF NOT EXISTS metadata (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              type TEXT NOT NULL,
              title TEXT NOT NULL,
              year INTEGER,
              tmdb_id INTEGER,
              imdb_id TEXT,
              overview TEXT,
              poster_url TEXT,
              rating REAL,
              fetched_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_metadata_tmdb ON metadata(type, tmdb_id)
              WHERE tmdb_id IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_metadata_title ON metadata(type, title);
            CREATE TABLE IF NOT EXISTS scans (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              disk_id INTEGER NOT NULL REFERENCES disks(id) ON DELETE CASCADE,
              mode TEXT,
              started_at TEXT,
              finished_at TEXT,
              status TEXT DEFAULT 'in_progress',
              files_before INTEGER,
              files_after INTEGER,
              added INTEGER DEFAULT 0,
              removed INTEGER DEFAULT 0,
              modified INTEGER DEFAULT 0,
              error TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_scans_disk ON scans(disk_id, started_at DESC);
            CREATE TABLE IF NOT EXISTS scan_changes (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              scan_id INTEGER NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
              change_type TEXT NOT NULL,
              parent_dir TEXT NOT NULL,
              filename TEXT NOT NULL,
              size_bytes INTEGER,
              mtime REAL,
              old_size_bytes INTEGER,
              old_mtime REAL
            );
            CREATE INDEX IF NOT EXISTS idx_scan_changes_scan ON scan_changes(scan_id, change_type);
            CREATE TABLE IF NOT EXISTS jobs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              name TEXT NOT NULL,
              kind TEXT DEFAULT 'delete',
              status TEXT DEFAULT 'pending',
              created_at TEXT DEFAULT CURRENT_TIMESTAMP,
              notes TEXT
            );
            CREATE TABLE IF NOT EXISTS job_files (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
              disk_id INTEGER NOT NULL REFERENCES disks(id),
              parent_dir TEXT NOT NULL,
              filename TEXT NOT NULL,
              expected_size INTEGER,
              expected_mtime REAL,
              status TEXT DEFAULT 'pending',
              executed_at TEXT,
              error TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_job_files_job ON job_files(job_id);
            CREATE INDEX IF NOT EXISTS idx_job_files_disk_pending ON job_files(disk_id, status);
        """)
        # idempotent: add columns if upgrading an older DB
        existing_cols = {r[1] for r in conn.execute("PRAGMA table_info(files)")}
        if "metadata_id" not in existing_cols:
            conn.execute("ALTER TABLE files ADD COLUMN metadata_id INTEGER REFERENCES metadata(id)")
        if "enrich_status" not in existing_cols:
            conn.execute("ALTER TABLE files ADD COLUMN enrich_status TEXT DEFAULT NULL")
        disk_cols = {r[1] for r in conn.execute("PRAGMA table_info(disks)")}
        for col, ddl in [
            ("smart_available", "INTEGER DEFAULT 0"),
            ("smart_overall", "TEXT"),
            ("smart_firmware", "TEXT"),
            ("smart_rotation_rpm", "INTEGER"),
            ("smart_power_on_hours", "INTEGER"),
            ("smart_power_cycles", "INTEGER"),
            ("smart_load_cycles", "INTEGER"),
            ("smart_temp_c", "INTEGER"),
            ("smart_reallocated", "INTEGER"),
            ("smart_pending", "INTEGER"),
            ("smart_uncorrectable", "INTEGER"),
            ("smart_udma_crc", "INTEGER"),
            ("smart_ata_errors", "INTEGER"),
            ("smart_error", "TEXT"),
            ("smart_checked_at", "TEXT"),
            ("smart_raw_json", "TEXT"),
        ]:
            if col not in disk_cols:
                conn.execute(f"ALTER TABLE disks ADD COLUMN {col} {ddl}")


def smart_probe(device):
    """Run smartctl -j on the device. Returns dict; available=False if SMART couldn't be read."""
    if not device or not device.startswith("/dev/"):
        return {"available": False, "error": "no block device"}

    parent = _run(["lsblk", "-no", "PKNAME", device])
    target = f"/dev/{parent}" if parent else device

    # try sudo -n first, then plain (in case user is in disk group or it's accessible)
    raw = None
    err = None
    for prefix in (["sudo", "-n"], []):
        try:
            res = subprocess.run(
                prefix + ["smartctl", "-j", "-d", "sat", "-a", target],
                capture_output=True, text=True, timeout=15,
            )
            if res.stdout.lstrip().startswith("{"):
                raw = res.stdout
                break
            err = (res.stderr or res.stdout).strip()[:200]
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            err = str(e)
            continue

    if not raw:
        return {"available": False, "error": err or "smartctl unavailable", "device": target}

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        return {"available": False, "error": f"smartctl json parse: {e}", "device": target}

    info = {
        "available": True,
        "device": target,
        "raw_json": raw,
        "model": data.get("model_name"),
        "serial": data.get("serial_number"),
        "firmware": data.get("firmware_version"),
        "rotation_rpm": data.get("rotation_rate"),
        "overall": "PASSED" if data.get("smart_status", {}).get("passed") else "FAILED",
    }
    by_id = {a["id"]: a for a in data.get("ata_smart_attributes", {}).get("table", [])}

    def attr_raw(aid):
        a = by_id.get(aid)
        return a["raw"]["value"] if a else None

    info["power_on_hours"] = attr_raw(9)
    info["power_cycles"] = attr_raw(12)
    info["load_cycles"] = attr_raw(193)
    info["reallocated"] = attr_raw(5)
    info["pending"] = attr_raw(197)
    info["uncorrectable"] = attr_raw(198)
    info["udma_crc"] = attr_raw(199)
    info["temp_c"] = (data.get("temperature") or {}).get("current") or attr_raw(194) or attr_raw(190)
    info["ata_errors"] = (data.get("ata_smart_error_log", {}).get("summary") or {}).get("count", 0)
    return info


def upsert_smart_for_disk(disk_id, smart):
    """Write smart_probe() result into the disks row."""
    with db() as conn:
        if not smart.get("available"):
            conn.execute(
                """UPDATE disks SET smart_available=0, smart_error=?, smart_checked_at=datetime('now')
                   WHERE id=?""",
                (smart.get("error", "unavailable"), disk_id),
            )
        else:
            conn.execute(
                """UPDATE disks SET smart_available=1, smart_overall=?, smart_firmware=?,
                   smart_rotation_rpm=?, smart_power_on_hours=?, smart_power_cycles=?,
                   smart_load_cycles=?, smart_temp_c=?, smart_reallocated=?, smart_pending=?,
                   smart_uncorrectable=?, smart_udma_crc=?, smart_ata_errors=?,
                   smart_error=NULL, smart_checked_at=datetime('now'), smart_raw_json=?
                   WHERE id=?""",
                (smart["overall"], smart.get("firmware"), smart.get("rotation_rpm"),
                 smart.get("power_on_hours"), smart.get("power_cycles"),
                 smart.get("load_cycles"), smart.get("temp_c"), smart.get("reallocated"),
                 smart.get("pending"), smart.get("uncorrectable"), smart.get("udma_crc"),
                 smart.get("ata_errors"), smart.get("raw_json"), disk_id),
            )
        conn.commit()


VIDEO_EXT = {".mkv", ".mp4", ".avi", ".mov", ".m4v", ".ts", ".mpg", ".mpeg",
             ".wmv", ".flv", ".webm", ".m2ts", ".vob", ".divx", ".rmvb", ".iso"}


def is_video(filename):
    name = filename.lower()
    for ext in VIDEO_EXT:
        if name.endswith(ext):
            return True
    return False


def _run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        return ""


def probe_disk(mount_point):
    info = {"mount_point": os.path.abspath(mount_point)}
    if not os.path.isdir(info["mount_point"]):
        raise FileNotFoundError(f"{mount_point} does not exist")

    df_out = _run(["df", "--output=source,fstype", info["mount_point"]])
    lines = df_out.splitlines()
    if len(lines) >= 2:
        parts = lines[1].split()
        info["device"] = parts[0]
        if len(parts) > 1:
            info["fs_type"] = parts[1]

    st = os.statvfs(info["mount_point"])
    info["total_bytes"] = st.f_blocks * st.f_frsize
    info["free_bytes"] = st.f_bavail * st.f_frsize
    info["used_bytes"] = info["total_bytes"] - info["free_bytes"]

    dev = info.get("device", "")
    if dev.startswith("/dev/"):
        parent = _run(["lsblk", "-no", "PKNAME", dev])
        target = f"/dev/{parent}" if parent else dev
        info["model"] = _run(["lsblk", "-no", "MODEL", target]) or None
        info["serial"] = _run(["lsblk", "-no", "SERIAL", target]) or None
    return info


# OS metadata that's pure cruft on a cataloged disk — skip at walk time
# so it never enters the catalog. Extend if other variants show up
# (.fseventsd, .Trashes, .TemporaryItems, $RECYCLE.BIN, etc.).
JUNK_DIRS = {".Spotlight-V100"}
JUNK_FILENAMES = {"thumbs.db", "desktop.ini"}


def _is_junk_file(name):
    if name == ".DS_Store" or name.startswith("._"):
        return True
    return name.lower() in JUNK_FILENAMES


def _walk_disk(mount_abs, batch_cb, progress_cb=None, batch_size=None):
    """Walk filesystem, deliver batches of (parent_dir, filename, size, mtime) tuples."""
    skip_top = {"/proc", "/sys", "/dev", "/run", "/var/run", "/var/lock"}
    batch_size = batch_size or SCAN_BATCH
    batch = []
    count = 0
    dirs_seen = 0
    for root, dirs, files in os.walk(mount_abs, followlinks=False):
        if any(root == s or root.startswith(s + "/") for s in skip_top):
            dirs[:] = []
            continue
        dirs[:] = [d for d in dirs if d not in JUNK_DIRS]
        dirs_seen += 1
        rel_root = os.path.relpath(root, mount_abs)
        if rel_root == ".":
            rel_root = ""
        for name in files:
            if _is_junk_file(name):
                continue
            full = os.path.join(root, name)
            try:
                s = os.lstat(full)
                if _stat.S_ISLNK(s.st_mode):
                    continue
                batch.append((rel_root, name, s.st_size, s.st_mtime))
                count += 1
                if len(batch) >= batch_size:
                    batch_cb(batch)
                    batch = []
                    if progress_cb:
                        progress_cb(count)
            except (OSError, PermissionError):
                continue
    if batch:
        batch_cb(batch)
    return count, dirs_seen


def audit_disk_sync(disk_id, mount_point, progress_cb=None):
    """Incremental scan: diff against existing catalog, record added/removed/modified."""
    mount_abs = os.path.abspath(mount_point)
    conn = db()
    scan_id = None
    try:
        files_before = conn.execute(
            "SELECT COUNT(*) FROM files WHERE disk_id=?", (disk_id,)
        ).fetchone()[0]
        cur = conn.execute(
            """INSERT INTO scans (disk_id, mode, started_at, status)
               VALUES (?, 'audit', datetime('now'), 'in_progress')""",
            (disk_id,),
        )
        scan_id = cur.lastrowid
        conn.execute(
            "UPDATE disks SET scan_status='scanning', scan_progress=0, scan_error=NULL WHERE id=?",
            (disk_id,),
        )
        conn.commit()

        conn.execute("""CREATE TEMP TABLE staging (
            parent_dir TEXT NOT NULL,
            filename TEXT NOT NULL,
            size_bytes INTEGER,
            mtime REAL,
            PRIMARY KEY (parent_dir, filename)
        ) WITHOUT ROWID""")

        def stage(batch):
            conn.executemany(
                "INSERT OR IGNORE INTO staging VALUES (?,?,?,?)", batch
            )
            conn.execute(
                "UPDATE disks SET scan_progress=scan_progress+? WHERE id=?",
                (len(batch), disk_id),
            )
            conn.commit()

        seen, dirs_seen = _walk_disk(mount_abs, stage, progress_cb)

        # Diff
        added = conn.execute("""
            SELECT s.parent_dir, s.filename, s.size_bytes, s.mtime FROM staging s
            WHERE NOT EXISTS (
              SELECT 1 FROM files f
              WHERE f.disk_id=? AND f.parent_dir=s.parent_dir AND f.filename=s.filename
            )
        """, (disk_id,)).fetchall()
        removed = conn.execute("""
            SELECT f.parent_dir, f.filename, f.size_bytes, f.mtime FROM files f
            WHERE f.disk_id=? AND NOT EXISTS (
              SELECT 1 FROM staging s
              WHERE s.parent_dir=f.parent_dir AND s.filename=f.filename
            )
        """, (disk_id,)).fetchall()
        modified = conn.execute("""
            SELECT f.parent_dir, f.filename,
                   f.size_bytes AS old_size, f.mtime AS old_mtime,
                   s.size_bytes AS new_size, s.mtime AS new_mtime
            FROM files f JOIN staging s
              ON s.parent_dir=f.parent_dir AND s.filename=f.filename
            WHERE f.disk_id=?
              AND (COALESCE(f.size_bytes,-1) != COALESCE(s.size_bytes,-1)
                   OR ABS(COALESCE(f.mtime,0) - COALESCE(s.mtime,0)) > 2)
        """, (disk_id,)).fetchall()

        if added:
            conn.executemany(
                """INSERT INTO files (disk_id, parent_dir, filename, size_bytes, mtime)
                   VALUES (?,?,?,?,?)""",
                [(disk_id, r["parent_dir"], r["filename"], r["size_bytes"], r["mtime"])
                 for r in added],
            )
        if removed:
            conn.executemany(
                "DELETE FROM files WHERE disk_id=? AND parent_dir=? AND filename=?",
                [(disk_id, r["parent_dir"], r["filename"]) for r in removed],
            )
        if modified:
            conn.executemany(
                """UPDATE files SET size_bytes=?, mtime=?
                   WHERE disk_id=? AND parent_dir=? AND filename=?""",
                [(r["new_size"], r["new_mtime"], disk_id, r["parent_dir"], r["filename"])
                 for r in modified],
            )

        if added:
            conn.executemany(
                """INSERT INTO scan_changes (scan_id, change_type, parent_dir, filename, size_bytes, mtime)
                   VALUES (?, 'added', ?, ?, ?, ?)""",
                [(scan_id, r["parent_dir"], r["filename"], r["size_bytes"], r["mtime"])
                 for r in added],
            )
        if removed:
            conn.executemany(
                """INSERT INTO scan_changes (scan_id, change_type, parent_dir, filename,
                                              old_size_bytes, old_mtime)
                   VALUES (?, 'removed', ?, ?, ?, ?)""",
                [(scan_id, r["parent_dir"], r["filename"], r["size_bytes"], r["mtime"])
                 for r in removed],
            )
        if modified:
            conn.executemany(
                """INSERT INTO scan_changes (scan_id, change_type, parent_dir, filename,
                                              size_bytes, mtime, old_size_bytes, old_mtime)
                   VALUES (?, 'modified', ?, ?, ?, ?, ?, ?)""",
                [(scan_id, r["parent_dir"], r["filename"], r["new_size"], r["new_mtime"],
                  r["old_size"], r["old_mtime"]) for r in modified],
            )

        new_count = conn.execute(
            "SELECT COUNT(*) FROM files WHERE disk_id=?", (disk_id,)
        ).fetchone()[0]
        conn.execute(
            """UPDATE scans SET finished_at=datetime('now'), status='done',
               files_before=?, files_after=?, added=?, removed=?, modified=?
               WHERE id=?""",
            (files_before, new_count, len(added), len(removed), len(modified), scan_id),
        )
        conn.execute(
            """UPDATE disks SET scan_status='done', scan_progress=?, file_count=?,
                                dir_count=?, scanned_at=datetime('now') WHERE id=?""",
            (new_count, new_count, dirs_seen, disk_id),
        )
        conn.commit()
        return {
            "scan_id": scan_id,
            "added": len(added),
            "removed": len(removed),
            "modified": len(modified),
            "files_before": files_before,
            "files_after": new_count,
        }
    except Exception as e:
        if scan_id:
            try:
                conn.execute(
                    "UPDATE scans SET status='error', finished_at=datetime('now'), error=? WHERE id=?",
                    (str(e), scan_id),
                )
            except Exception:
                pass
        conn.execute(
            "UPDATE disks SET scan_status='error', scan_error=? WHERE id=?",
            (str(e), disk_id),
        )
        conn.commit()
        raise
    finally:
        conn.close()


def scan_disk_sync(disk_id, mount_point, progress_cb=None):
    """Full destructive rescan: wipes all file rows for this disk then reinserts."""
    with db() as conn:
        conn.execute(
            "UPDATE disks SET scan_status=?, scan_progress=0, scan_error=NULL WHERE id=?",
            ("scanning", disk_id),
        )
        conn.execute("DELETE FROM files WHERE disk_id=?", (disk_id,))
        conn.commit()

    skip_top = {"/proc", "/sys", "/dev", "/run", "/var/run", "/var/lock"}
    mount_abs = os.path.abspath(mount_point)
    batch = []
    count = 0
    dirs_seen = 0

    conn = db()
    try:
        for root, dirs, files in os.walk(mount_abs, followlinks=False):
            if any(root == s or root.startswith(s + "/") for s in skip_top):
                dirs[:] = []
                continue
            dirs[:] = [d for d in dirs if d not in JUNK_DIRS]
            dirs_seen += 1
            rel_root = os.path.relpath(root, mount_abs)
            if rel_root == ".":
                rel_root = ""
            for name in files:
                if _is_mac_junk_file(name):
                    continue
                full = os.path.join(root, name)
                try:
                    s = os.lstat(full)
                    if _stat.S_ISLNK(s.st_mode):
                        continue
                    batch.append((disk_id, rel_root, name, s.st_size, s.st_mtime))
                    count += 1
                    if len(batch) >= SCAN_BATCH:
                        conn.executemany(
                            "INSERT INTO files (disk_id, parent_dir, filename, size_bytes, mtime) VALUES (?,?,?,?,?)",
                            batch,
                        )
                        conn.execute(
                            "UPDATE disks SET scan_progress=? WHERE id=?", (count, disk_id)
                        )
                        conn.commit()
                        batch = []
                        if progress_cb:
                            progress_cb(count)
                except (OSError, PermissionError):
                    continue
        if batch:
            conn.executemany(
                "INSERT INTO files (disk_id, parent_dir, filename, size_bytes, mtime) VALUES (?,?,?,?,?)",
                batch,
            )
            conn.commit()
        conn.execute(
            """UPDATE disks SET scan_status='done', scan_progress=?, file_count=?, dir_count=?,
               scanned_at=datetime('now') WHERE id=?""",
            (count, count, dirs_seen, disk_id),
        )
        conn.commit()
        return count
    except Exception as e:
        conn.execute(
            "UPDATE disks SET scan_status='error', scan_error=? WHERE id=?",
            (str(e), disk_id),
        )
        conn.commit()
        raise
    finally:
        conn.close()


_UNITS = ("B", "KB", "MB", "GB", "TB", "PB")


def fmt_bytes(n):
    if n is None:
        return "—"
    n = float(n)
    for unit in _UNITS:
        if abs(n) < 1024.0 or unit == _UNITS[-1]:
            return f"{n:,.1f} {unit}" if unit != "B" else f"{int(n):,} B"
        n /= 1024.0


def fmt_mtime(ts):
    if ts is None:
        return "—"
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except (OSError, ValueError):
        return "—"
