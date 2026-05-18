#!/usr/bin/env python3
"""Refresh SMART data for cataloged disks without re-scanning files.

Usage:
  ./smartcheck.py                    # all cataloged disks (skips disks not currently mounted)
  ./smartcheck.py --disk-id 3        # one disk by id
"""
import argparse
import os
import sys

from lib import db, init_db, smart_probe, upsert_smart_for_disk


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--disk-id", type=int)
    args = p.parse_args()
    init_db()
    with db() as conn:
        if args.disk_id:
            rows = conn.execute(
                "SELECT id, name, device, mount_point FROM disks WHERE id=?", (args.disk_id,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, name, device, mount_point FROM disks ORDER BY name COLLATE NOCASE"
            ).fetchall()

    if not rows:
        sys.exit("no disks in catalog")

    ok = unavailable = 0
    for r in rows:
        dev = r["device"]
        mount_alive = r["mount_point"] and os.path.ismount(r["mount_point"])
        print(f"\n[{r['id']}] {r['name']}  device={dev}  mounted={'yes' if mount_alive else 'no'}")
        smart = smart_probe(dev)
        upsert_smart_for_disk(r["id"], smart)
        if smart.get("available"):
            print(f"  health={smart['overall']}  on={smart.get('power_on_hours')}h"
                  f"  realloc={smart.get('reallocated')}"
                  f"  pending={smart.get('pending')}"
                  f"  crc={smart.get('udma_crc')}  temp={smart.get('temp_c')}C")
            ok += 1
        else:
            print(f"  unavailable: {smart.get('error')}", file=sys.stderr)
            unavailable += 1
    print(f"\nDone. ok={ok} unavailable={unavailable}")


if __name__ == "__main__":
    main()
