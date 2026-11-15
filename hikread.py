#!/usr/bin/env python3
"""
hikread.py - read-only footage extractor for Hikvision / HiLook DVR & NVR hard disks.

Reads a raw disk (\\\\.\\PhysicalDrive2 on Windows as Administrator, /dev/sdX on Linux)
or a disk image (recommended). Never writes to the source.

  python hikread.py info    SOURCE
  python hikread.py list    SOURCE [--channel N] [--from "YYYY-MM-DD HH:MM"] [--to "..."]
  python hikread.py extract SOURCE OUTDIR [--channel N] [--from ...] [--to ...] [--mp4] [--whole]
  python hikread.py carve   SOURCE OUTDIR [--limit N] [--mp4]

Output is the recorder's native MPEG-PS stream (.ps). Use --mp4 to remux the
video track with ffmpeg (must be on PATH). Times are shown in local time.
"""
import argparse
import datetime as dt
import os
import shutil
import struct
import subprocess
import sys
from collections import Counter

__version__ = "1.0.0"

ALIGN = 4096                      # raw-device read alignment (some USB docks reject 512)
SIG = b"HIKVISION@HANGZHOU"
BTREE_SIG = b"HIKBTREE"
PES_VIDEO = b"\x00\x00\x01\xe0"
PTS_WRAP = 1 << 33                # 90 kHz PTS wraps roughly every 26.5 hours
MIN_TS = int(dt.datetime(2008, 1, 1).timestamp())
MAX_TS = int(dt.datetime(2040, 1, 1).timestamp())
COPY_CHUNK = 8 * 1024 * 1024

# Master-sector field offsets (relative to the master sector start) seen in the wild.
# capacity is at +0x48 and init time at +0xF0 in both.
LAYOUTS = {
    "A": dict(data_offset=0x70, block_size=0x80, block_count=0x88,
              bt1_off=0x90, bt1_size=0x98, bt2_off=0xA0, bt2_size=0xA8),
    "B": dict(data_offset=0x78, block_size=0x88, block_count=0x90,      # +8 shifted,
              bt1_off=0x98, bt1_size=0xA0, bt2_off=0xA8, bt2_size=0xB0),  # e.g. HiLook 2019 fw
}


# ---------------------------------------------------------------- helpers

def u32(b, o): return struct.unpack_from("<I", b, o)[0]
def u64(b, o): return struct.unpack_from("<Q", b, o)[0]
def ts(t): return dt.datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S")
def stamp(t): return dt.datetime.fromtimestamp(t).strftime("%Y%m%d_%H%M%S")
def gib(n): return f"{n / 1024**3:.2f} GiB"


def parse_time(s):
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return int(dt.datetime.strptime(s, fmt).timestamp())
        except ValueError:
            pass
    sys.exit(f'Bad time "{s}" - use "YYYY-MM-DD HH:MM"')


class RawReader:
    """Aligned, read-only reader that works on images and raw devices."""

    def __init__(self, path):
        self.f = open(path, "rb", buffering=0)

    def read(self, off, size):
        start = off - off % ALIGN
        end = off + size
        end += -end % ALIGN
        self.f.seek(start)
        buf = bytearray()
        need = end - start
        while need > 0:
            chunk = self.f.read(min(need, COPY_CHUNK))
            if not chunk:
                break
            buf += chunk
            need -= len(chunk)
        return bytes(buf[off - start: off - start + size])


# ---------------------------------------------------------------- disk structures

def sane(m):
    bs, n, cap = m["block_size"], m["block_count"], m["capacity"]
    return (16 * 1024**2 <= bs <= 4 * 1024**3 and bs % 512 == 0 and 0 < n < 1_000_000
            and (not cap or bs * n <= cap) and 0 < m["bt1_off"] < (cap or 1 << 50))


def read_master(r):
    sec = r.read(0, 4096)
    pos = sec.find(SIG)
    if pos < 0:
        sys.exit("HIKVISION signature not found in the first 4 KiB - not a Hikvision/HiLook "
                 "disk, or the wrong disk number.")
    base = pos - 0x10
    m = None
    for name, L in LAYOUTS.items():
        m = {k: (u32 if k in ("block_count", "bt1_size", "bt2_size") else u64)(sec, base + o)
             for k, o in L.items()}
        m.update(layout=name, master_offset=base, capacity=u64(sec, base + 0x48),
                 init_time=u32(sec, base + 0xF0), raw=sec[base:base + 0x100])
        if sane(m):
            return m
    m["layout"] = "?"
    return m


def find_btree(r, m):
    for off, size in ((m["bt1_off"], m["bt1_size"]), (m["bt2_off"], m["bt2_size"])):
        if off and 0 < size < 1 << 31 and BTREE_SIG in r.read(off, 64):
            return off, size
    return None, None


def parse_index(r, m):
    """Scan the HIKBTREE for recording entries. Entry layout (relative):
       +0x09 channel, +0x10 start time, +0x14 end time, +0x18 data-block offset.
       Sets m["block_rem"] (data-block alignment) as a side effect."""
    off, size = find_btree(r, m)
    if off is None:
        return []
    bt = r.read(off, size)
    bs = m["block_size"]
    cap = m["capacity"] or 1 << 50
    cands = []
    for p in range(0x18, len(bt) - 8, 8):
        blk = u64(bt, p)
        if blk < 0x100000 or blk >= cap or blk % 512:
            continue
        e = p - 0x18
        start, end = u32(bt, e + 0x10), u32(bt, e + 0x14)
        if MIN_TS < start < MAX_TS and start <= end < MAX_TS and end - start < 30 * 86400:
            cands.append((blk, e, start, end))
    if not cands:
        return []
    rem = Counter(c[0] % bs for c in cands).most_common(1)[0][0]
    m["block_rem"] = rem
    seen, entries = set(), []
    for blk, e, start, end in cands:
        if blk % bs != rem or (blk, start) in seen:
            continue
        seen.add((blk, start))
        entries.append(dict(channel=bt[e + 0x09], start=start, end=end,
                            block=blk, block_no=(blk - rem) // bs))
    entries.sort(key=lambda x: (x["start"], x["channel"]))
    return entries


def filter_entries(entries, a):
    out = entries
    if a.channel is not None:
        out = [e for e in out if e["channel"] == a.channel]
    if a.frm:
        t = parse_time(a.frm)
        out = [e for e in out if e["end"] >= t]
    if a.to:
        t = parse_time(a.to)
        out = [e for e in out if e["start"] <= t]
    return out


# ---------------------------------------------------------------- video stream

def pes_pts(buf, i):
    if i + 14 > len(buf) or not buf[i + 7] & 0x80:
        return None
    b = buf[i + 9:i + 14]
    return ((b[0] >> 1) & 7) << 30 | b[1] << 22 | (b[2] >> 1) << 15 | b[3] << 7 | b[4] >> 1


def time_map(r, blk, size, b_start, b_end, step=256 * 1024):
    """Sample video PTS through a data block -> [(byte_offset, unix_time)].
       Stops where the newest recording ends (older, overwritten footage follows)."""
    samples, base, prev, pts0 = [], 0, None, None
    limit = (b_end - b_start + 3600) * 90000
    for coff in range(0, size, COPY_CHUNK):
        buf = r.read(blk + coff, COPY_CHUNK + 64)
        for pos in range(0, COPY_CHUNK, step):
            i = buf.find(PES_VIDEO, pos, pos + step)
            pts = pes_pts(buf, i) if i >= 0 else None
            if pts is None:
                continue
            if prev is None:
                pts0 = pts
            else:
                if pts < prev:
                    if prev > PTS_WRAP - (1 << 31) and pts < 1 << 31:
                        base += PTS_WRAP
                    else:
                        print()
                        return samples
                if base + pts - pts0 > limit:
                    print()
                    return samples
            prev = pts
            samples.append((coff + i, b_start + (base + pts - pts0) / 90000))
        pct = min(coff + COPY_CHUNK, size) * 100 // size
        print(f"\r    scanning timestamps {pct:3d}%", end="", flush=True)
    print()
    return samples


def copy_range(r, off, size, dest):
    with open(dest, "wb") as f:
        done = 0
        while done < size:
            n = min(COPY_CHUNK, size - done)
            f.write(r.read(off + done, n))
            done += n
            print(f"\r    copying {done * 100 // size:3d}%", end="", flush=True)
    print()


def to_mp4(src):
    if not shutil.which("ffmpeg"):
        print("    ffmpeg not on PATH - skipping MP4 conversion")
        return
    base = os.path.splitext(src)[0]
    err = ""
    for ext in (".mp4", ".mkv"):
        dst = base + ext
        res = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-fflags", "+genpts+discardcorrupt",
             "-analyzeduration", "100M", "-probesize", "100M", "-f", "mpeg", "-i", src,
             "-map", "0:v:0", "-c", "copy", dst],
            capture_output=True, text=True)
        if os.path.exists(dst) and os.path.getsize(dst) > 1024:
            print(f"    -> {os.path.basename(dst)} (video only)")
            return
        if os.path.exists(dst):
            os.remove(dst)
        err = res.stderr.strip()
    print("    ffmpeg conversion failed - play the .ps file in VLC instead")
    if err:
        print("    " + err.splitlines()[-1])


# ---------------------------------------------------------------- commands

def cmd_info(r, a):
    m = read_master(r)
    print(f"Master sector         0x{m['master_offset']:X} (layout {m['layout']})")
    print(f"Capacity              {gib(m['capacity'])}")
    it = m["init_time"]
    print(f"Initialised           {ts(it) if MIN_TS < it < MAX_TS else '?'}")
    print(f"Data block size       {m['block_size'] // 1024**2} MiB")
    print(f"Data blocks           {m['block_count']}")
    print(f"HIKBTREE1             0x{m['bt1_off']:X} ({m['bt1_size']} bytes)")
    print(f"HIKBTREE2             0x{m['bt2_off']:X} ({m['bt2_size']} bytes)")
    if not sane(m):
        print("\nMaster sector hex dump:")
        raw = m["raw"]
        for i in range(0, len(raw), 16):
            print(f"  +{i:03X}: " + " ".join(f"{b:02X}" for b in raw[i:i + 16]))
        print("\nUnknown master-sector layout. Please open an issue with this output.")
        return
    e = parse_index(r, m)
    if not e:
        print("\nNo index entries found - try 'carve'.")
        return
    print(f"\nIndex entries         {len(e)}")
    print(f"Channels              {', '.join(map(str, sorted({x['channel'] for x in e})))}")
    print(f"Footage range         {ts(e[0]['start'])} -> {ts(max(x['end'] for x in e))}")


def cmd_list(r, a):
    m = read_master(r)
    e = filter_entries(parse_index(r, m), a)
    print(f"{'CH':>3}  {'START':19}  {'END':19}  {'BLOCK':>6}")
    for x in e:
        print(f"{x['channel']:>3}  {ts(x['start'])}  {ts(x['end'])}  {x['block_no']:>6}")
    print(f"\n{len(e)} entries")


def cmd_extract(r, a):
    m = read_master(r)
    if not sane(m):
        sys.exit("Unknown master-sector layout - run 'info' and open an issue with its output.")
    all_entries = parse_index(r, m)
    wanted = filter_entries(all_entries, a)
    if not wanted:
        sys.exit("No matching recordings. Run 'list' without filters, or try 'carve'.")
    os.makedirs(a.outdir, exist_ok=True)
    bs = m["block_size"]

    span = {}  # a block's true time span, across all of its entries
    for x in all_entries:
        s0, e0 = span.get(x["block"], (x["start"], x["end"]))
        span[x["block"]] = (min(s0, x["start"]), max(e0, x["end"]))
    blocks = {}
    for x in wanted:
        blocks.setdefault(x["block"], x["channel"])

    t_from = parse_time(a.frm) if a.frm else None
    t_to = parse_time(a.to) if a.to else None
    trim = (t_from or t_to) and not a.whole

    for blk, ch in sorted(blocks.items(), key=lambda kv: span[kv[0]][0]):
        b_start, b_end = span[blk]
        print(f"  ch{ch:02d} block @ 0x{blk:X}: {ts(b_start)} -> {ts(b_end)}")
        lo, hi, n0, n1 = 0, bs, b_start, b_end
        if trim:
            smp = time_map(r, blk, bs, b_start, b_end)
            if len(smp) < 2:
                print("    no readable timestamps - extracting the whole block")
            else:
                last = smp[-1][1]
                drift = abs(last - b_end)
                print(f"    stream timestamps {ts(smp[0][1])} -> {ts(last)} "
                      f"(index end {ts(b_end)}, diff {drift:.0f}s)")
                if drift > max(300, 0.05 * (b_end - b_start)):
                    print("    WARNING: stream clock disagrees with the index - trimmed times may be off")
                pad = 60  # start a minute early so the clip begins on a keyframe
                before = [o for o, t in smp if t <= (t_from or b_start) - pad]
                after = [o for o, t in smp if t >= (t_to or b_end) + pad]
                lo = before[-1] if before else 0
                hi = after[0] if after else smp[-1][0] + 512 * 1024
                lo -= lo % ALIGN
                hi = min(bs, hi + -hi % ALIGN)
                n0 = int(next((t for o, t in smp if o >= lo), b_start))
                n1 = int(next((t for o, t in reversed(smp) if o <= hi), b_end))
        name = f"ch{ch:02d}_{stamp(n0)}_to_{stamp(n1)}.ps"
        dest = os.path.join(a.outdir, name)
        print(f"    writing {name} ({(hi - lo) / 1024**2:.1f} MiB)")
        copy_range(r, blk + lo, hi - lo, dest)
        if a.mp4:
            to_mp4(dest)


def cmd_carve(r, a):
    m = read_master(r)
    if not sane(m):
        sys.exit("Unknown master-sector layout - run 'info' and open an issue with its output.")
    os.makedirs(a.outdir, exist_ok=True)
    parse_index(r, m)  # learns the data-block alignment if the index is readable
    bs = m["block_size"]
    first = m.get("block_rem", m["data_offset"] % bs)
    total = (m["capacity"] - first) // bs if m["capacity"] else m["block_count"]
    n = 0
    for i in range(total):
        off = first + i * bs
        head = r.read(off, 64)
        if not (head.startswith(b"IMKH") or head.startswith(b"\x00\x00\x01\xba")):
            continue
        dest = os.path.join(a.outdir, f"block_{i:05d}.ps")
        print(f"  block {i} @ 0x{off:X}")
        copy_range(r, off, bs, dest)
        if a.mp4:
            to_mp4(dest)
        n += 1
        if a.limit and n >= a.limit:
            break
    print(f"{n} blocks carved")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"hik-diskreader {__version__}")
    sp = ap.add_subparsers(dest="cmd", required=True)
    for name, hlp in (("info", "show disk layout, cameras and date range"),
                      ("list", "list recordings"),
                      ("extract", "extract recordings (trimmed to --from/--to)"),
                      ("carve", "ignore the index and dump every block holding video")):
        p = sp.add_parser(name, help=hlp)
        p.add_argument("source", help=r"disk image, \\.\PhysicalDriveN or /dev/sdX")
        if name in ("extract", "carve"):
            p.add_argument("outdir")
            p.add_argument("--mp4", action="store_true", help="also remux video to MP4 with ffmpeg")
        if name in ("list", "extract"):
            p.add_argument("--channel", type=int)
            p.add_argument("--from", dest="frm", metavar="TIME", help='"YYYY-MM-DD HH:MM"')
            p.add_argument("--to", metavar="TIME", help='"YYYY-MM-DD HH:MM"')
        if name == "extract":
            p.add_argument("--whole", action="store_true", help="copy whole data blocks, no trimming")
        if name == "carve":
            p.add_argument("--limit", type=int, default=0, help="stop after N blocks")
    a = ap.parse_args()
    try:
        r = RawReader(a.source)
    except PermissionError:
        sys.exit("Access denied - raw disks need an Administrator prompt (Windows) or sudo (Linux).")
    except FileNotFoundError:
        sys.exit(f"Not found: {a.source}")
    {"info": cmd_info, "list": cmd_list, "extract": cmd_extract, "carve": cmd_carve}[a.cmd](r, a)


if __name__ == "__main__":
    main()
