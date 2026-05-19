#!/usr/bin/env python3
"""
mosaic.py — Assemble Sentinel-2 tiles into a single full-resolution mosaic.

Reads    : downloads/{zone}/{date}/tile_{col}_{row}.npy
Outputs  : downloads/{zone}/{date}/mosaic_g{gain}_y{gamma}.png   (always)
           downloads/{zone}/{date}/mosaic.tif                     (--geotiff, requires rasterio)

Resolution is preserved : each pixel = 10 m on the ground.

Usage:
    python mosaic.py                                              # interactive
    python mosaic.py -z Zone_Alpha -d 2024-05-15
    python mosaic.py -z Zone_Alpha -d 2024-05-15 --gain 3.0 --gamma 1.2
    python mosaic.py -z Zone_Alpha -d 2024-05-15 --scale 0.5     # half-size
    python mosaic.py -z Zone_Alpha -d 2024-05-15 --geotiff        # + GeoTIFF
"""

import argparse
import json
import re
import sys
from pathlib import Path

import numpy as np

try:
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

try:
    import rasterio
    import rasterio.windows
    from rasterio.transform import from_bounds
    RASTERIO_AVAILABLE = True
except ImportError:
    RASTERIO_AVAILABLE = False

DOWNLOADS_ROOT = Path("./downloads")


# ─────────────────────────────────────────────────────────────────────────────
# Tone mapping
# ─────────────────────────────────────────────────────────────────────────────

def apply_tone(arr: np.ndarray, gain: float, gamma: float) -> np.ndarray:
    """float32 [0, 1] → uint8 [0, 255]."""
    return (np.clip(arr * gain, 0.0, 1.0) ** (1.0 / gamma) * 255).astype(np.uint8)


# ─────────────────────────────────────────────────────────────────────────────
# Discovery
# ─────────────────────────────────────────────────────────────────────────────

def scan_library(root: Path) -> dict[str, list[str]]:
    """Return {zone: [date, ...]} for every folder that contains tile_*.npy files."""
    lib: dict[str, list[str]] = {}
    if not root.exists():
        return lib
    for zone_dir in sorted(root.iterdir()):
        if not zone_dir.is_dir():
            continue
        dates = [
            d.name
            for d in sorted(zone_dir.iterdir())
            if d.is_dir() and any(d.glob("tile_*.npy"))
        ]
        if dates:
            lib[zone_dir.name] = dates
    return lib


def pick_interactive(lib: dict) -> tuple[str, str]:
    zones = list(lib.keys())
    if not zones:
        sys.exit("Aucune tuile trouvée dans ./downloads/. Lancez d'abord satellite_tracker.py.")

    print("\nZones disponibles :")
    for i, z in enumerate(zones):
        print(f"  [{i}] {z}  ({len(lib[z])} date(s))")
    try:
        idx_z = int(input("Choisir une zone [0] : ").strip() or 0)
    except (ValueError, EOFError):
        idx_z = 0
    zone = zones[min(idx_z, len(zones) - 1)]

    dates = lib[zone]
    print(f"\nDates disponibles pour {zone} :")
    for i, d in enumerate(dates):
        n = sum(1 for _ in (DOWNLOADS_ROOT / zone / d).glob("tile_*.npy"))
        print(f"  [{i}] {d}  ({n} tuile(s))")
    try:
        idx_d = int(input("Choisir une date [0] : ").strip() or 0)
    except (ValueError, EOFError):
        idx_d = 0
    return zone, dates[min(idx_d, len(dates) - 1)]


# ─────────────────────────────────────────────────────────────────────────────
# Tile indexing & layout
# ─────────────────────────────────────────────────────────────────────────────

def index_tiles(tile_dir: Path) -> dict[tuple[int, int], Path]:
    """Scan tile_*.npy → {(col, row): path}. col=X (W→E), row=Y (S→N)."""
    tiles: dict[tuple[int, int], Path] = {}
    for f in tile_dir.glob("tile_*.npy"):
        m = re.match(r"tile_(\d+)_(\d+)\.npy$", f.name)
        if m:
            tiles[(int(m.group(1)), int(m.group(2)))] = f
    return tiles


def compute_layout(
    tiles: dict[tuple[int, int], Path]
) -> tuple[dict[int, int], dict[int, int], dict[int, int], dict[int, int]]:
    """
    Read each tile's header to get pixel dimensions (mmap, no full load).
    Returns col_widths, row_heights, x_offsets, y_offsets.

    Geographic convention:
      • col increases West → East  → x_offset increases left to right
      • row increases South → North → highest row gets y_offset = 0 (north on top)
    """
    col_widths:  dict[int, int] = {}
    row_heights: dict[int, int] = {}

    for (col, row), path in sorted(tiles.items()):
        arr = np.load(str(path), mmap_mode="r")
        h   = arr.shape[0]
        w   = arr.shape[1] if arr.ndim >= 2 else 1
        # Use the LARGEST dimension seen per col/row as the cell size.
        # _fit_to_cell will stretch smaller tiles to fill — no black gaps.
        col_widths[col]  = max(col_widths.get(col, 0),  w)
        row_heights[row] = max(row_heights.get(row, 0), h)

    # x_offsets: west (col=0) to east
    x_offsets: dict[int, int] = {}
    x = 0
    for col in sorted(col_widths):
        x_offsets[col] = x
        x += col_widths[col]

    # y_offsets: north (highest row) to south
    y_offsets: dict[int, int] = {}
    y = 0
    for row in sorted(row_heights, reverse=True):
        y_offsets[row] = y
        y += row_heights[row]

    return col_widths, row_heights, x_offsets, y_offsets


# ─────────────────────────────────────────────────────────────────────────────
# Assembly
# ─────────────────────────────────────────────────────────────────────────────

def _fit_to_cell(patch: np.ndarray, target_w: int, target_h: int) -> np.ndarray:
    """
    Resize patch to exactly (target_w, target_h).

    Why this is needed: bbox_to_dimensions computes pixel size using cos(lat_mid) to
    convert degrees → metres. Tiles in the same column at slightly different latitudes
    get widths that differ by 1-2 px. Without this, those pixels stay black (gap).
    """
    ph, pw = patch.shape[:2]
    if ph == target_h and pw == target_w:
        return patch
    if PIL_AVAILABLE:
        return np.array(Image.fromarray(patch).resize((target_w, target_h), Image.LANCZOS))
    # No PIL: edge-pad or crop to fill cell without distortion artefacts
    bands = patch.shape[2] if patch.ndim == 3 else 1
    out   = np.zeros((target_h, target_w, bands) if bands > 1 else (target_h, target_w),
                     dtype=patch.dtype)
    ch = min(ph, target_h)
    cw = min(pw, target_w)
    out[:ch, :cw] = patch[:ch, :cw]
    if target_w > cw:                           # pad right edge with last column
        out[:ch, cw:] = patch[:ch, cw - 1:cw]
    if target_h > ch:                           # pad bottom edge with last row
        out[ch:, :] = out[ch - 1:ch, :]
    return out


def _resize_patch(patch: np.ndarray, scale: float) -> np.ndarray:
    """Downscale a uint8 patch. Uses PIL/LANCZOS if available, else nearest."""
    if scale == 1.0:
        return patch
    h, w = patch.shape[:2]
    new_w = max(1, int(round(w * scale)))
    new_h = max(1, int(round(h * scale)))
    if PIL_AVAILABLE:
        return np.array(Image.fromarray(patch).resize((new_w, new_h), Image.LANCZOS))
    step = max(1, int(round(1.0 / scale)))
    return patch[::step, ::step]


def assemble_png(
    tiles:       dict[tuple[int, int], Path],
    col_widths:  dict[int, int],
    row_heights: dict[int, int],
    x_offsets:   dict[int, int],
    y_offsets:   dict[int, int],
    gain: float,
    gamma: float,
    scale: float = 1.0,
) -> np.ndarray:
    """
    Build a uint8 RGB canvas tile by tile (memory-efficient).
    Applies tone mapping + optional downscale per tile before pasting.
    """
    total_w = sum(col_widths.values())
    total_h = sum(row_heights.values())
    out_w   = max(1, int(round(total_w * scale)))
    out_h   = max(1, int(round(total_h * scale)))
    canvas  = np.zeros((out_h, out_w, 3), dtype=np.uint8)

    n = len(tiles)
    for i, ((col, row), path) in enumerate(sorted(tiles.items()), 1):
        print(f"  [{i:3d}/{n}]  tile_{col}_{row}.npy", end="  ", flush=True)

        arr = np.load(str(path)).astype(np.float32)
        if arr.ndim == 2:                                   # grayscale edge case
            arr = np.stack([arr, arr, arr], axis=-1)

        patch = apply_tone(arr, gain, gamma)
        patch = _resize_patch(patch, scale)

        # Target cell size in the (scaled) canvas
        target_w = max(1, int(round(col_widths[col] * scale)))
        target_h = max(1, int(round(row_heights[row] * scale)))

        # Stretch/pad to fill cell exactly — eliminates 1-2px gaps from lat-dependent
        # bbox_to_dimensions rounding
        patch = _fit_to_cell(patch, target_w, target_h)

        y0 = int(round(y_offsets[row] * scale))
        x0 = int(round(x_offsets[col] * scale))
        canvas[y0:y0 + target_h, x0:x0 + target_w] = patch

        print(f"nat={arr.shape[1]}×{arr.shape[0]}px → cell={target_w}×{target_h}px  "
              f"paste=({x0},{y0})  max={patch.max()}")

    return canvas


def assemble_geotiff(
    tiles:       dict[tuple[int, int], Path],
    col_widths:  dict[int, int],
    row_heights: dict[int, int],
    x_offsets:   dict[int, int],
    y_offsets:   dict[int, int],
    out_path:    Path,
    metadata:    dict | None,
    gain: float,
    gamma: float,
) -> None:
    """
    Write a georeferenced uint8 GeoTIFF directly (window-by-window, no full RAM).
    Requires rasterio. Uses metadata.json for spatial reference if available.
    """
    if not RASTERIO_AVAILABLE:
        print("  GeoTIFF ignoré : rasterio absent  (pip install rasterio)")
        return

    total_w = sum(col_widths.values())
    total_h = sum(row_heights.values())

    transform = None
    crs       = None
    if metadata:
        b = metadata.get("bbox", {})
        if b:
            transform = from_bounds(
                b["lon_min"], b["lat_min"],
                b["lon_max"], b["lat_max"],
                width=total_w, height=total_h,
            )
            crs = "EPSG:4326"

    print(f"\n  Écriture GeoTIFF {total_w}×{total_h} px …")
    n = len(tiles)
    with rasterio.open(
        str(out_path), "w",
        driver="GTiff",
        height=total_h, width=total_w,
        count=3,
        dtype=np.uint8,
        crs=crs,
        transform=transform,
        compress="deflate",
        predictor=2,
        zlevel=6,
        tiled=True,
        blockxsize=256,
        blockysize=256,
    ) as dst:
        for i, ((col, row), path) in enumerate(sorted(tiles.items()), 1):
            print(f"  [{i:3d}/{n}]  tile_{col}_{row} …", end="  ", flush=True)
            arr   = np.load(str(path)).astype(np.float32)
            if arr.ndim == 2:
                arr = np.stack([arr, arr, arr], axis=-1)
            patch = apply_tone(arr, gain, gamma)

            # Fill cell exactly — same gap fix as PNG path
            target_w = col_widths[col]
            target_h = row_heights[row]
            patch    = _fit_to_cell(patch, target_w, target_h)

            y0  = y_offsets[row]
            x0  = x_offsets[col]
            win = rasterio.windows.Window(x0, y0, target_w, target_h)
            for band in range(3):
                dst.write(patch[:, :, band], band + 1, window=win)
            print("ok")

    size_mb  = out_path.stat().st_size / (1024 * 1024)
    geo_info = "  [WGS84 EPSG:4326]" if crs else "  [pas de géoréférencement — metadata.json absent]"
    print(f"  GeoTIFF → {out_path}  ({size_mb:.1f} MB){geo_info}")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Assemble Sentinel-2 tiles into a single mosaic.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("-z", "--zone",    help="Zone name (sous-dossier de ./downloads/)")
    parser.add_argument("-d", "--date",    help="Date d'acquisition (YYYY-MM-DD)")
    parser.add_argument("--gain",          type=float, default=2.0,
                        help="Gain luminosité (défaut 2.0)")
    parser.add_argument("--gamma",         type=float, default=1.5,
                        help="Correction gamma (défaut 1.5)")
    parser.add_argument("--scale",         type=float, default=1.0,
                        help="Facteur de réduction, ex. 0.5 = demi-résolution (défaut 1.0)")
    parser.add_argument("--geotiff",       action="store_true",
                        help="Exporter aussi un GeoTIFF géoréférencé (nécessite rasterio)")
    parser.add_argument("--list",          action="store_true",
                        help="Lister les zones/dates disponibles et quitter")
    args = parser.parse_args()

    lib = scan_library(DOWNLOADS_ROOT)

    if args.list:
        if not lib:
            print("Aucune tuile trouvée dans ./downloads/")
        for zone, dates in lib.items():
            for d in dates:
                n = sum(1 for _ in (DOWNLOADS_ROOT / zone / d).glob("tile_*.npy"))
                print(f"  {zone}/{d}  ({n} tuile(s))")
        return

    # Zone + date selection
    if args.zone and args.date:
        zone, acq_date = args.zone, args.date
    else:
        zone, acq_date = pick_interactive(lib)

    tile_dir = DOWNLOADS_ROOT / zone / acq_date
    if not tile_dir.exists():
        sys.exit(f"Dossier introuvable : {tile_dir}")

    tiles = index_tiles(tile_dir)
    if not tiles:
        sys.exit(f"Aucune tuile tile_*.npy dans {tile_dir}")

    all_cols = sorted({c for c, _ in tiles})
    all_rows = sorted({r for _, r in tiles})
    n_cols, n_rows = len(all_cols), len(all_rows)

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\n{'─' * 62}")
    print(f"  Zone       : {zone}")
    print(f"  Date       : {acq_date}")
    print(f"  Grille     : {n_cols} col × {n_rows} lig = {len(tiles)} tuile(s)")
    print(f"  Paramètres : gain={args.gain}  gamma={args.gamma}  scale={args.scale}")

    # Load metadata.json if present
    meta_path = tile_dir / "metadata.json"
    metadata  = json.loads(meta_path.read_text()) if meta_path.exists() else None
    if args.geotiff and metadata:
        b = metadata["bbox"]
        print(f"  BBox       : [{b['lon_min']:.4f}, {b['lat_min']:.4f}, "
              f"{b['lon_max']:.4f}, {b['lat_max']:.4f}]")
    elif args.geotiff:
        print("  ⚠ metadata.json absent : GeoTIFF sans géoréférencement.")

    # ── Layout ────────────────────────────────────────────────────────────────
    print(f"\n[1/2] Calcul du layout …")
    col_widths, row_heights, x_offsets, y_offsets = compute_layout(tiles)

    total_w = sum(col_widths.values())
    total_h = sum(row_heights.values())
    out_w   = max(1, int(round(total_w * args.scale)))
    out_h   = max(1, int(round(total_h * args.scale)))
    ram_est = out_w * out_h * 3 / (1024 * 1024)   # MB for uint8 RGB

    print(f"  Natif      : {total_w} × {total_h} px  "
          f"({total_w * 10 / 1000:.1f} km × {total_h * 10 / 1000:.1f} km @ 10 m/px)")
    if args.scale != 1.0:
        print(f"  Sortie     : {out_w} × {out_h} px  "
              f"({out_w * 10 / args.scale / 1000:.1f} km × {out_h * 10 / args.scale / 1000:.1f} km "
              f"@ {10 / args.scale:.0f} m/px)")
    print(f"  RAM estimée: ~{ram_est:.0f} MB (PNG uint8)")

    if ram_est > 3000:
        print(f"\n  ⚠  Image très volumineuse ({ram_est:.0f} MB).")
        print(f"     Ajoutez --scale 0.5 pour réduire à ~{ram_est / 4:.0f} MB.")
        confirm = input("  Continuer quand même ? [o/N] ").strip().lower()
        if confirm not in ("o", "oui", "y", "yes"):
            sys.exit("Annulé.")

    print(f"{'─' * 62}")

    # ── PNG assembly ──────────────────────────────────────────────────────────
    print(f"\n[2/2] Assemblage PNG …")
    canvas = assemble_png(
        tiles, col_widths, row_heights, x_offsets, y_offsets,
        args.gain, args.gamma, args.scale,
    )

    suffix   = f"_g{args.gain}_y{args.gamma}"
    if args.scale != 1.0:
        suffix += f"_x{args.scale}"
    png_path = tile_dir / f"mosaic{suffix}.png"

    if PIL_AVAILABLE:
        Image.fromarray(canvas).save(str(png_path))
    else:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        plt.imsave(str(png_path), canvas)
    size_kb = png_path.stat().st_size // 1024
    print(f"\n  PNG → {png_path}  ({canvas.shape[1]}×{canvas.shape[0]} px, {size_kb:,} KB)")

    # ── GeoTIFF (optional) ────────────────────────────────────────────────────
    if args.geotiff:
        tif_path = tile_dir / "mosaic.tif"
        assemble_geotiff(
            tiles, col_widths, row_heights, x_offsets, y_offsets,
            tif_path, metadata, args.gain, args.gamma,
        )

    # ── Final report ──────────────────────────────────────────────────────────
    print(f"\n{'─' * 62}")
    print(f"  ✓  Mosaïque terminée")
    print(f"     {canvas.shape[1]} × {canvas.shape[0]} px  |  "
          f"{canvas.shape[1] * 10 * (1/args.scale) / 1000:.1f} km × "
          f"{canvas.shape[0] * 10 * (1/args.scale) / 1000:.1f} km  |  "
          f"{10 / args.scale:.0f} m/px")
    if not args.geotiff and RASTERIO_AVAILABLE:
        print(f"     → Ajouter --geotiff pour exporter un GeoTIFF (QGIS-ready).")
    elif not RASTERIO_AVAILABLE:
        print(f"     → pip install rasterio  puis --geotiff pour un GeoTIFF QGIS-ready.")
    print(f"{'─' * 62}\n")


if __name__ == "__main__":
    main()
