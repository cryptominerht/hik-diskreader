# hik-diskreader

Read and extract CCTV footage from a **Hikvision / HiLook DVR or NVR hard disk** on a normal PC, with no recorder needed.

When a recorder dies, its disk looks blank to Windows ("Disk not initialised"). That's because Hikvision uses its own filesystem. This tool reads that filesystem directly. It lists recordings by camera and time and extracts the clips you want, trimmed to the time window you ask for.

- **Read-only.** It never writes to the source disk.
- A single Python file with no dependencies. ffmpeg is optional, for MP4 output.
- Works on Windows and Linux, on a raw disk or a disk image.

> ⚠️ **Never click "Initialise" when Windows offers to.** It overwrites the start of the disk. Click Cancel.

## Quick start (Windows)

1. Connect the disk to your PC (SATA or a USB dock) and **cancel** the "Initialize Disk" prompt.
2. Note its disk number in Disk Management (e.g. *Disk 2*).
3. Install [Python 3](https://www.python.org/downloads/), and [ffmpeg](https://ffmpeg.org/download.html) if you want MP4 output.
4. Open **Command Prompt or PowerShell as Administrator** in the folder containing `hikread.py`:

```
python hikread.py info \\.\PhysicalDrive2
python hikread.py list \\.\PhysicalDrive2 --channel 1 --from "2026-11-02 08:00" --to "2026-11-02 18:00"
python hikread.py extract \\.\PhysicalDrive2 C:\cctv_out --channel 1 --from "2026-11-02 09:25" --to "2026-11-02 09:35" --mp4
```

On Linux, use `/dev/sdX` with `sudo`.

## Commands

| Command | What it does |
|---|---|
| `info` | Checks it's a Hikvision disk and shows its layout, cameras and the date range of the footage |
| `list` | Lists recording segments (channel, start, end, data block) |
| `extract` | Extracts matching recordings. With `--from`/`--to` it reads the video's own timestamps and copies only that window, padded by about a minute. Use `--whole` for complete data blocks |
| `carve` | Ignores the index and dumps every data block that holds video. Use this when the index is damaged |

Add `--mp4` to remux the video track to MP4, falling back to MKV. Audio is dropped, because the recorders' G.711/G.726 audio often won't remux.

## Playing the footage

Most Hikvision cameras record **H.265 (HEVC)**, which Windows can't play out of the box.

- **[VLC](https://www.videolan.org/)** plays the `.mp4` and the raw `.ps` files.
- To make a clip that plays anywhere, convert it to H.264:
  `ffmpeg -i clip.mp4 -c:v libx264 -preset veryfast -crf 20 -an clip_h264.mp4`

## If the disk might be failing

The disk may be what killed the recorder. Check it with CrystalDiskInfo or `smartctl`. If it shows reallocated or pending sectors, **image it first** with HDD Raw Copy Tool or `ddrescue`, then run this tool on the image file:

```
python hikread.py info D:\cctv.img
```

## Tested on

| Recorder | Firmware era | Layout | Notes |
|---|---|---|---|
| HiLook DVR, 1 TB disk | ~2019 | B | 1 GiB data blocks, H.265 |

Your recorder may use a different layout. If `info` prints *"Unknown master-sector layout"* or finds no index entries, please [open an issue](../../issues) and paste the `info` output (it includes a hex dump of the master sector, which contains no footage).

## Limitations

- The disk format is undocumented. Field offsets come from public research and testing, so some firmware versions may need a new layout added.
- Trim points have roughly 1–3 minutes of granularity, and clips start a minute early so they begin on a keyframe.
- Timestamps are shown in the PC's local time zone. Recorder time-zone settings aren't applied.
- **Not a forensically validated tool.** For footage that may be used as evidence, image the disk, record its hash, and use dedicated forensic software such as DVR Examiner.

## Disclaimer

This is an independent project. It is **not affiliated with, endorsed by, or supported by Hikvision or HiLook**; those names are used only to describe compatibility. Use at your own risk. Only access disks and footage you are authorised to view.

## License

[MIT](LICENSE)
