#!/usr/bin/env python3
"""Scan a mounted disk and add/update it in the disklib catalog."""
import argparse
import sys
import time

from lib import (audit_disk_sync, db, effective_serial, fmt_bytes, init_db,
                 probe_disk, reserved_advice, scan_disk_sync, smart_probe,
                 upsert_smart_for_disk)


def yesno(prompt, choices):
    while True:
        ans = input(prompt).strip().lower()
        if ans in choices:
            return ans
        print(f"  Please answer one of: {', '.join(choices)}")


def main():
    p = argparse.ArgumentParser(
        description="Scan a mounted disk into the disklib catalog."
    )
    p.add_argument("mount", help="Mount point of disk to scan")
    p.add_argument("name", nargs="?", help="Disk name (required if new)")
    p.add_argument("--keep", action="store_true",
                   help="On name conflict, keep existing catalog name (no prompt)")
    p.add_argument("--rename", action="store_true",
                   help="On name conflict, rename to provided name (no prompt)")
    p.add_argument("--yes", action="store_true",
                   help="Skip any rescan-confirmation prompts")
    p.add_argument("--full-rescan", action="store_true",
                   help="Wipe and re-index all files (default is incremental audit + diff)")
    args = p.parse_args()

    init_db()

    print(f"Probing {args.mount} ...")
    try:
        info = probe_disk(args.mount)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(2)

    print(f"  device : {info.get('device') or '?'}")
    print(f"  model  : {info.get('model') or '?'}")
    print(f"  serial : {info.get('serial') or '(unavailable)'}")
    print(f"  fs     : {info.get('fs_type') or '?'}")
    print(f"  total  : {fmt_bytes(info.get('total_bytes'))}")
    print(f"  used   : {fmt_bytes(info.get('used_bytes'))}")
    print(f"  free   : {fmt_bytes(info.get('free_bytes'))}")
    print(f"  external: {'yes' if info.get('is_external') else 'no'}")
    advice = reserved_advice({k: info.get(k) for k in
                              ("fs_type", "is_external", "reserved_bytes", "fs_uuid", "device")})
    if advice and not advice.get("unknown"):
        print(f"  reserved: {fmt_bytes(advice['bytes'])} held back for root on an external disk")
        print(f"            reclaim it with: {advice['cmd']}")
    if info.get("is_subfolder"):
        print(f"  scope  : subfolder of {info['partition_mount']}")

    eff_serial = effective_serial(info)

    if not info.get("serial"):
        print()
        if info.get("is_subfolder"):
            print(f"warning: serial unavailable — cataloged by device path + folder "
                  f"({eff_serial}).", file=sys.stderr)
        else:
            print("warning: serial unavailable — disk will be cataloged by mount path only.",
                  file=sys.stderr)

    with db() as conn:
        existing = None
        if eff_serial:
            existing = conn.execute(
                "SELECT * FROM disks WHERE serial=?", (eff_serial,)
            ).fetchone()

        if not existing and info.get("is_subfolder") and info.get("serial"):
            related = conn.execute(
                "SELECT * FROM disks WHERE serial LIKE ? ORDER BY id",
                (f"{info['serial']}:%",),
            ).fetchall()
            if related:
                print()
                print(f"This physical disk (serial {info['serial']}) is already")
                print(f"cataloged under one or more other paths:")
                for i, r in enumerate(related, 1):
                    print(f"  [{i}] {r['name']!r}  ({r['mount_point']})")
                print(f"You are scanning : {info['mount_point']}")
                print()
                print(f"  [n] add as new entry (different scope)")
                for i, r in enumerate(related, 1):
                    print(f"  [{i}] update entry #{i} ({r['name']!r}) to the new path")
                print(f"  [c] cancel")
                choices = {"n", "c"} | {str(i) for i in range(1, len(related) + 1)}
                ans = yesno(f"  Choice [{'/'.join(sorted(choices))}]: ", choices)
                if ans == "c":
                    print("Cancelled.")
                    sys.exit(0)
                if ans != "n":
                    picked = related[int(ans) - 1]
                    conn.execute(
                        "UPDATE disks SET serial=? WHERE id=?",
                        (eff_serial, picked["id"]),
                    )
                    conn.commit()
                    existing = conn.execute(
                        "SELECT * FROM disks WHERE id=?", (picked["id"],)
                    ).fetchone()

        if existing:
            chosen = existing["name"]
            print()
            print(f"This disk is already cataloged as: {existing['name']!r} (id={existing['id']})")

            if args.name and args.name != existing["name"]:
                print()
                print(f"  ! NAME CONFLICT")
                print(f"    Cataloged as : {existing['name']!r}")
                print(f"    You provided : {args.name!r}")
                if args.keep and args.rename:
                    print("error: --keep and --rename are mutually exclusive", file=sys.stderr)
                    sys.exit(2)
                if args.keep:
                    chosen = existing["name"]
                    print(f"  → --keep: continuing with existing name {chosen!r}")
                elif args.rename:
                    chosen = args.name
                    print(f"  → --rename: updating catalog to {chosen!r}")
                else:
                    print()
                    print("  [k] keep existing name and rescan")
                    print("  [r] rename to provided name and rescan")
                    print("  [c] cancel")
                    ans = yesno("  Choice [k/r/c]: ", {"k", "r", "c"})
                    if ans == "c":
                        print("Cancelled.")
                        sys.exit(0)
                    chosen = existing["name"] if ans == "k" else args.name

            if chosen != existing["name"]:
                conn.execute(
                    "UPDATE disks SET name=? WHERE id=?", (chosen, existing["id"])
                )

            conn.execute(
                """UPDATE disks SET device=?, mount_point=?, fs_type=?, model=?,
                   total_bytes=?, free_bytes=?, used_bytes=?, fs_uuid=?,
                   is_external=?, reserved_bytes=?, scan_status='pending',
                   scan_progress=0, scan_error=NULL WHERE id=?""",
                (info.get("device"), info["mount_point"], info.get("fs_type"),
                 info.get("model"), info.get("total_bytes"), info.get("free_bytes"),
                 info.get("used_bytes"), info.get("fs_uuid"), info.get("is_external"),
                 info.get("reserved_bytes"), existing["id"]),
            )
            conn.commit()
            disk_id = existing["id"]
            print(f"\nRescanning (catalog id {disk_id}, name {chosen!r}) ...")
        else:
            if not args.name:
                print("\nerror: name required when cataloging a new disk.", file=sys.stderr)
                print("  usage: scandisk.py <mount_point> <name>", file=sys.stderr)
                sys.exit(2)
            cur = conn.execute(
                """INSERT INTO disks (name, device, model, serial, fs_type, mount_point,
                   total_bytes, free_bytes, used_bytes, fs_uuid, is_external,
                   reserved_bytes, scan_status)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'pending')""",
                (args.name, info.get("device"), info.get("model"), eff_serial,
                 info.get("fs_type"), info["mount_point"], info.get("total_bytes"),
                 info.get("free_bytes"), info.get("used_bytes"), info.get("fs_uuid"),
                 info.get("is_external"), info.get("reserved_bytes")),
            )
            disk_id = cur.lastrowid
            conn.commit()
            print(f"\nAdded new disk to catalog: id={disk_id} name={args.name!r}")
            print(f"Scanning ...")

    print("\nProbing SMART ...")
    smart = smart_probe(info.get("device"))
    upsert_smart_for_disk(disk_id, smart)
    if smart.get("available"):
        attrs = []
        if smart.get("overall"):
            attrs.append(f"health={smart['overall']}")
        if smart.get("power_on_hours") is not None:
            attrs.append(f"on={smart['power_on_hours']}h")
        if smart.get("power_cycles") is not None:
            attrs.append(f"cycles={smart['power_cycles']}")
        if smart.get("temp_c") is not None:
            attrs.append(f"temp={smart['temp_c']}C")
        for k in ("reallocated", "pending", "uncorrectable", "udma_crc"):
            if smart.get(k) is not None:
                attrs.append(f"{k}={smart[k]}")
        print(f"  {'  '.join(attrs)}")
    else:
        print(f"  unavailable ({smart.get('error', 'unknown')})", file=sys.stderr)

    t0 = time.time()
    last_print = 0

    def progress(n):
        nonlocal last_print
        now = time.time()
        if now - last_print >= 1.0:
            rate = n / (now - t0) if now > t0 else 0
            print(f"  {n:>9,} files  ({rate:,.0f}/s)", flush=True)
            last_print = now

    try:
        if args.full_rescan:
            print("  mode: FULL RESCAN (wipe + reindex)")
            total = scan_disk_sync(disk_id, info["mount_point"], progress_cb=progress)
            elapsed = time.time() - t0
            print(f"\nDone. {total:,} files in {elapsed:.1f}s ({total / max(elapsed, 0.01):,.0f}/s)")
        else:
            print("  mode: AUDIT (diff against catalog)")
            result = audit_disk_sync(disk_id, info["mount_point"], progress_cb=progress)
            elapsed = time.time() - t0
            print(
                f"\nDone in {elapsed:.1f}s."
                f"\n  files before : {result['files_before']:,}"
                f"\n  files after  : {result['files_after']:,}"
                f"\n  added        : {result['added']:,}"
                f"\n  removed      : {result['removed']:,}"
                f"\n  modified     : {result['modified']:,}"
                f"\n  scan id      : {result['scan_id']}"
            )
    except Exception as e:
        print(f"\nscan failed: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
