#!/usr/bin/env python3
"""Batch-convert .ani files to .gif using ImageMagick with a built-in ANI parser."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def _find_magick() -> str | None:
    for cmd in ("magick", "convert"):
        path = shutil.which(cmd)
        if path:
            return cmd
    return None


def _convert_with_magick(magick: str, src: Path, dst: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [magick, str(src), str(dst)],
        text=True,
        capture_output=True,
    )


def _read_u16(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 2], "little")


def _read_u32(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 4], "little")


def _iter_riff_chunks(data: bytes, start: int, end: int) -> list[tuple[bytes, bytes]]:
    chunks: list[tuple[bytes, bytes]] = []
    pos = start
    while pos + 8 <= end:
        tag = data[pos : pos + 4]
        length = _read_u32(data, pos + 4)
        payload_start = pos + 8
        payload_end = payload_start + length
        chunks.append((tag, data[payload_start:payload_end]))
        pos = payload_end + (length % 2)
    return chunks


def _is_ani(data: bytes) -> bool:
    return len(data) >= 12 and data[0:4] == b"RIFF" and data[8:12] == b"ACON"


def _parse_ani(data: bytes) -> tuple[list[bytes], list[int], list[int], int | None]:
    if not _is_ani(data):
        raise RuntimeError("not an ANI file")

    frames: list[bytes] = []
    order: list[int] = []
    rates: list[int] = []
    default_rate: int | None = None

    for tag, payload in _iter_riff_chunks(data, 12, len(data)):
        if tag == b"anih" and len(payload) >= 36:
            default_rate = _read_u32(payload, 28)
        elif tag == b"rate":
            rates = [
                _read_u32(payload, i)
                for i in range(0, len(payload) - (len(payload) % 4), 4)
            ]
        elif tag == b"seq ":
            order = [
                _read_u32(payload, i)
                for i in range(0, len(payload) - (len(payload) % 4), 4)
            ]
        elif tag == b"LIST" and payload[:4] == b"fram":
            for sub_tag, sub_payload in _iter_riff_chunks(payload, 4, len(payload)):
                if sub_tag == b"icon":
                    frames.append(sub_payload)

    if not frames:
        raise RuntimeError("no icon frames found")

    return frames, order, rates, default_rate


def _pick_best_ico_index(ico_bytes: bytes) -> int:
    if len(ico_bytes) < 6:
        return 0
    count = _read_u16(ico_bytes, 4)
    if count == 0:
        return 0

    best_idx = 0
    best_score = -1
    base = 6
    for i in range(count):
        off = base + i * 16
        if off + 16 > len(ico_bytes):
            break
        width = ico_bytes[off] or 256
        height = ico_bytes[off + 1] or 256
        bitcount = _read_u16(ico_bytes, off + 6)
        score = width * height * max(bitcount, 1)
        if score > best_score:
            best_score = score
            best_idx = i
    return best_idx


def _jiffies_to_cs(value: int) -> int:
    # ANI rates are in 1/60 sec units. GIF delays are in 1/100 sec.
    return max(1, round(value * 100 / 60))


def _convert_with_ani_parser(
    magick: str,
    data: bytes,
    dst: Path,
    delay_override: int | None,
    loop: int,
    dispose: str,
    max_frames: int | None = None,
) -> None:
    frames, order, rates, default_rate = _parse_ani(data)

    if not order:
        order = list(range(len(frames)))

    # Skip frames if max_frames is specified
    if max_frames is not None and len(order) > max_frames:
        step = len(order) / max_frames
        new_order = [order[int(i * step)] for i in range(max_frames)]
        order = new_order
        if rates:
            rates = [rates[int(i * step)] for i in range(max_frames)]

    if delay_override is not None:
        delays = [delay_override] * len(order)
    elif rates:
        if len(rates) == len(order):
            delays = [_jiffies_to_cs(v) for v in rates]
        elif len(rates) == len(frames):
            delays = [_jiffies_to_cs(rates[i]) for i in order]
        else:
            delays = [_jiffies_to_cs(rates[0])] * len(order)
    elif default_rate is not None:
        delays = [_jiffies_to_cs(default_rate)] * len(order)
    else:
        delays = [4] * len(order)

    with tempfile.TemporaryDirectory(prefix="ani_to_gif_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        png_frames: list[Path] = []
        for idx, frame_idx in enumerate(order):
            ico_bytes = frames[frame_idx]
            icon_type = _read_u16(ico_bytes, 2) if len(ico_bytes) >= 4 else 1
            ext = "cur" if icon_type == 2 else "ico"
            ico_path = tmp_path / f"frame_{idx:04d}.{ext}"
            png_path = tmp_path / f"frame_{idx:04d}.png"
            ico_path.write_bytes(ico_bytes)
            ico_index = _pick_best_ico_index(ico_bytes)

            convert = subprocess.run(
                [magick, f"{ico_path}[{ico_index}]", f"png32:{png_path}"],
                text=True,
                capture_output=True,
            )
            if convert.returncode != 0:
                msg = (convert.stderr or convert.stdout).strip() or "magick failed"
                raise RuntimeError(msg)

            png_frames.append(png_path)

        cmd = [magick, "-loop", str(loop)]
        for frame_path, delay in zip(png_frames, delays, strict=False):
            cmd.extend(
                [
                    "-delay",
                    str(delay),
                    str(frame_path),
                    "-set",
                    "dispose",
                    dispose,
                ]
            )
        cmd.append(str(dst))
        build = subprocess.run(cmd, text=True, capture_output=True)
        if build.returncode != 0:
            msg = (build.stderr or build.stdout).strip() or "magick failed"
            raise RuntimeError(msg)


def main() -> int:
    parser = argparse.ArgumentParser(description="Convert .ani files to .gif.")
    parser.add_argument("input", nargs="?", default=".", help="Input directory")
    parser.add_argument("-o", "--output", default=".", help="Output directory")
    parser.add_argument("--glob", default="*.ani", help="Glob pattern (default: *.ani)")
    parser.add_argument(
        "--overwrite", action="store_true", help="Overwrite existing GIFs"
    )
    parser.add_argument(
        "--delay",
        type=int,
        default=None,
        help="Override frame delay in 1/100s (default: from ANI or 4)",
    )
    parser.add_argument(
        "--loop",
        type=int,
        default=0,
        help="GIF loop count (0 = infinite)",
    )
    parser.add_argument(
        "--dispose",
        choices=["none", "background", "previous"],
        default="background",
        help="GIF disposal method (default: background)",
    )
    parser.add_argument(
        "--max-frames",
        type=int,
        default=None,
        help="Maximum number of frames (will skip frames to meet this limit)",
    )
    args = parser.parse_args()

    magick = _find_magick()
    if not magick:
        print("ImageMagick not found. Install with: brew install imagemagick", file=sys.stderr)
        return 2

    in_dir = Path(args.input).expanduser().resolve()
    out_dir = Path(args.output).expanduser().resolve()

    if not in_dir.exists():
        print(f"Input directory not found: {in_dir}", file=sys.stderr)
        return 2

    ani_files = sorted(in_dir.glob(args.glob))
    if not ani_files:
        print(f"No files matched: {in_dir / args.glob}")
        return 0

    failures = 0
    for src in ani_files:
        dst = out_dir / (src.stem + ".gif")
        if dst.exists() and not args.overwrite:
            print(f"Skip (exists): {dst}")
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        data: bytes | None = None
        try:
            data = src.read_bytes()
        except OSError as exc:
            failures += 1
            print(f"Failed: {src.name} ({exc})", file=sys.stderr)
            continue

        if _is_ani(data):
            try:
                _convert_with_ani_parser(
                    magick,
                    data,
                    dst,
                    delay_override=args.delay,
                    loop=args.loop,
                    dispose=args.dispose,
                    max_frames=args.max_frames,
                )
                print(f"OK (ani): {src.name} -> {dst.name}")
                continue
            except RuntimeError as exc:
                failures += 1
                print(f"Failed: {src.name} ({exc})", file=sys.stderr)
                continue

        result = _convert_with_magick(magick, src, dst)
        if result.returncode == 0:
            print(f"OK: {src.name} -> {dst.name}")
            continue

        failures += 1
        magick_err = (result.stderr or result.stdout).strip()
        msg = f"Failed: {src.name}"
        if magick_err:
            msg += f" ({magick_err})"
        print(msg, file=sys.stderr)

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
