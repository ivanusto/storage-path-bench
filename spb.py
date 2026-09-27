#!/usr/bin/env python3
"""spb: storage path bench. How long does it take to get a model's bytes from
a given path into RAM, and under which conditions.

It reads every file under --path sequentially (or mmaps and touches them,
which is what a GGUF loader does), reports wall time and throughput, and
records the conditions that decide whether the number means anything:
mount type and options, NFS connection count, readahead, whether the client
page cache was dropped, and the load average before the run. Everything goes
to JSONL so runs on different days and paths can be compared.

    spb.py run  --path /srv/models/llm/... --label nfs-cold --rounds 3 --drop-caches
    spb.py run  --path /nvme/models/...    --label nvme-warm --rounds 3
    spb.py run  --path /srv/models/llm/... --label nfs-mmap --mode mmap
    spb.py report results.jsonl

--drop-caches writes 3 to /proc/sys/vm/drop_caches when run as root. Without
root it evicts only the files under --path with posix_fadvise(DONTNEED), which
works on NFS too. Either way it clears the client's page cache only: the NAS
keeps its own cache (ZFS ARC), so a "cold" NFS run is cold on the client, not
on the server, and the JSONL says so. Standard library only.
"""
import argparse, datetime as dt, glob, json, mmap, os, re, socket, subprocess, sys, time

GIB = 1024 ** 3


def sh(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=20).stdout.strip()
    except Exception:
        return ""


def files_under(path):
    if os.path.isfile(path):
        return [path]
    out = []
    for d, _, fs in os.walk(path):
        for f in sorted(fs):
            p = os.path.join(d, f)
            if os.path.isfile(p) and not os.path.islink(p):
                out.append(p)
    return sorted(out)


def link_to(ip):
    """Interface and negotiated speed on the route to ip: a 10GbE card that
    quietly negotiated 2.5GbE still passes traffic, it is just slower."""
    m = re.search(r"\bdev (\S+)", sh(["ip", "route", "get", ip]))
    if not m:
        return {}
    dev = m.group(1)
    speed = sh(["cat", f"/sys/class/net/{dev}/speed"])
    return {"iface": dev, "link_mbps": int(speed) if speed.lstrip("-").isdigit() else None}


def block_queue(sysdev):
    """/sys/dev/block/MAJ:MIN of a partition points at the partition; the queue
    settings live on the whole disk one directory up."""
    d = os.path.realpath(sysdev)
    if os.path.exists(os.path.join(d, "partition")):
        d = os.path.dirname(d)
    return d


def mount_info(path):
    """What the path is mounted from, so the JSONL explains itself."""
    info = {}
    # An automounted NFS path lists the autofs trigger first and the real mount
    # last; the last line is the one the bytes come through.
    lines = sh(["findmnt", "-T", path, "-n", "-o", "TARGET,SOURCE,FSTYPE,OPTIONS"]).splitlines()
    line = lines[-1] if lines else ""
    if line:
        parts = line.split(None, 3)
        info.update(dict(zip(["target", "source", "fstype", "options"], parts)))
    st = os.stat(path)
    majmin = f"{os.major(st.st_dev)}:{os.minor(st.st_dev)}"
    fstype = info.get("fstype", "")
    if fstype.startswith("nfs"):
        host = info.get("source", "").split(":")[0]
        try:
            ip = socket.gethostbyname(host)
        except Exception:
            ip = host
        conns = sh(["ss", "-tn", f"dst {ip}:2049"]).splitlines()
        info["nfs_server"] = ip
        info["nfs_tcp_connections"] = max(0, len(conns) - 1)
        m = re.search(r"nconnect=(\d+)", info.get("options", ""))
        info["nconnect_requested"] = int(m.group(1)) if m else 1
        # NFS readahead is per mount (its bdi), separate from any disk's setting.
        ra = sh(["cat", f"/sys/class/bdi/{majmin}/read_ahead_kb"])
        info["read_ahead_kb"] = int(ra) if ra.isdigit() else None
        info.update(link_to(ip))
    elif os.path.exists(f"/sys/dev/block/{majmin}"):
        q = block_queue(f"/sys/dev/block/{majmin}")
        info["block_device"] = os.path.basename(q)
        ra = sh(["cat", f"{q}/queue/read_ahead_kb"])
        info["read_ahead_kb"] = int(ra) if ra.isdigit() else None
        rot = sh(["cat", f"{q}/queue/rotational"])
        info["rotational"] = rot == "1" if rot else None
        # iSCSI shows up as a plain block device; its sysfs path runs through the session.
        dev = os.path.realpath(f"{q}/device") if os.path.exists(f"{q}/device") else ""
        m = re.match(r"(.*/session\d+)/", dev)
        if m:
            info["transport"] = "iscsi"
            addr = glob.glob(f"{m.group(1)}/connection*/iscsi_connection/*/persistent_address")
            if addr:
                ip = sh(["cat", addr[0]])
                info["iscsi_target"] = ip
                info.update(link_to(ip))
    return info


def drop_caches(paths):
    if os.geteuid() == 0:
        os.sync()
        try:
            with open("/proc/sys/vm/drop_caches", "w") as f:
                f.write("3\n")
            return "dropped"
        except OSError as e:
            return f"failed: {e}"
    # Not root: evict just the files we are about to read.
    try:
        for p in paths:
            fd = os.open(p, os.O_RDONLY)
            try:
                os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
            finally:
                os.close(fd)
        return "dropped (fadvise, files under --path only)"
    except OSError as e:
        return f"failed: {e}"


def read_all(paths, block):
    n = 0
    buf = bytearray(block)
    view = memoryview(buf)
    for p in paths:
        with open(p, "rb", buffering=0) as f:
            while True:
                got = f.readinto(view)
                if not got:
                    break
                n += got
    return n


def mmap_touch(paths):
    """Touch one byte per page, the way a GGUF loader pulls weights in."""
    n = 0
    page = mmap.PAGESIZE
    for p in paths:
        size = os.path.getsize(p)
        if size == 0:
            continue
        with open(p, "rb") as f:
            with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as m:
                s = 0
                for off in range(0, size, page):
                    s += m[off]
                n += size
    return n


def cmd_run(a):
    paths = files_under(a.path)
    if not paths:
        sys.exit(f"no files under {a.path}")
    total = sum(os.path.getsize(p) for p in paths)
    minfo = mount_info(a.path)
    rows = []
    for r in range(1, a.rounds + 1):
        cache = drop_caches(paths) if a.drop_caches else "kept"
        load1 = os.getloadavg()[0]
        t0 = time.monotonic()
        n = mmap_touch(paths) if a.mode == "mmap" else read_all(paths, a.block_mb * 1024 * 1024)
        secs = time.monotonic() - t0
        row = {
            "t": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "label": a.label, "round": r, "mode": a.mode, "path": a.path,
            "files": len(paths), "bytes": n, "gib": round(n / GIB, 2),
            "seconds": round(secs, 2), "gib_per_s": round(n / GIB / secs, 3) if secs else None,
            "client_cache": cache, "server_cache": "unknown (not controllable from the client)",
            "block_mb": a.block_mb, "load1_before": round(load1, 2),
            "host": socket.gethostname(), "mount": minfo, "note": a.note or "",
        }
        rows.append(row)
        print(f"{a.label} round {r}: {row['gib']} GiB in {row['seconds']} s = {row['gib_per_s']} GiB/s (client cache {cache})")
        if a.out:
            with open(a.out, "a") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        if r < a.rounds and a.pause:
            time.sleep(a.pause)
    if total != rows[-1]["bytes"]:
        print(f"warning: read {rows[-1]['bytes']} of {total} bytes", file=sys.stderr)


def cmd_report(a):
    by = {}
    with open(a.jsonl) as f:
        for line in f:
            if line.strip():
                row = json.loads(line)
                by.setdefault(row["label"], []).append(row)
    print(f"{'label':18s} {'mode':5s} {'GiB':>7s} {'rounds':>6s} {'min s':>8s} {'med s':>8s} {'max s':>8s} {'med GiB/s':>10s}  cache / mount")
    for label, rows in by.items():
        secs = sorted(r["seconds"] for r in rows)
        med = secs[len(secs) // 2]
        gibs = sorted(r["gib_per_s"] or 0 for r in rows)
        m = rows[-1]["mount"]
        cond = rows[-1]["client_cache"]
        mnt = m.get("fstype", "?")
        if "nfs_tcp_connections" in m:
            mnt += f" nconnect={m.get('nconnect_requested')} conns={m['nfs_tcp_connections']}"
        if m.get("transport"):
            mnt += f" {m['transport']}"
        if m.get("read_ahead_kb") is not None:
            mnt += f" ra={m['read_ahead_kb']}k"
        if m.get("link_mbps"):
            mnt += f" link={m['iface']}@{m['link_mbps']}"
        print(f"{label:18s} {rows[-1]['mode']:5s} {rows[-1]['gib']:7.2f} {len(rows):6d} {secs[0]:8.2f} {med:8.2f} {secs[-1]:8.2f} {gibs[len(gibs)//2]:10.3f}  {cond} / {mnt}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run")
    p.add_argument("--path", required=True); p.add_argument("--label", required=True)
    p.add_argument("--rounds", type=int, default=3); p.add_argument("--mode", choices=["read", "mmap"], default="read")
    p.add_argument("--block-mb", type=int, default=8); p.add_argument("--drop-caches", action="store_true")
    p.add_argument("--pause", type=float, default=5.0, help="seconds between rounds")
    p.add_argument("--out", default="results.jsonl"); p.add_argument("--note")
    p.set_defaults(fn=cmd_run)
    p = sub.add_parser("report"); p.add_argument("jsonl"); p.set_defaults(fn=cmd_report)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
