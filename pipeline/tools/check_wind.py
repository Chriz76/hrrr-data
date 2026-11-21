#!/usr/bin/env python3
"""Punktuelle Pruefung der Kette Quelle -> Resample -> Kachel.

Ein einziger Zeitschritt genuegt, um alles ausser der Zeitreihen-Logik
(index.json, Horizont, Pruning) zu verifizieren. Es wird nichts heruntergeladen,
wenn eine lokale .om-Datei vorliegt.

Geprueft wird:
  * Erkennung des Windfeld-Formats (u/v oder Betrag/Richtung)
  * Umrechnung Betrag/Richtung -> u/v
  * Gitterabbildung lat/lon -> natives LCC-Gitter -> Resample
  * Nordausrichtung, Delta-Kodierung und Skala der Kachel (0,05 m/s)

Vorgehen:
  1. Quelldatei in den Arbeitsordner kopieren (process_om_file loescht sie)
  2. HrrrWindProcessor.process_om_file() darauf anwenden
  3. Fuer die Testpunkte die Kachel dekodieren und mit dem Quellwert an der
     entsprechenden LCC-Gitterzelle vergleichen

Die Richtungs*konvention* (True North, "woher") kann dieser Test nicht
beweisen, weil sie beim Rueckweg wieder herausfaellt - dafuer dient --api
(winziger JSON-Request gegen die Open-Meteo-API).

Beispiel:
    python pipeline/tools/check_wind.py --om C:/tmp/rrfs1200.om
    python pipeline/tools/check_wind.py --om C:/tmp/rrfs1200.om \
        --api-model ncep_rrfs_conus_15min
"""

from __future__ import annotations

import argparse
import gzip
import shutil
import struct
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from omfiles import OmFileReader

_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(_SRC))

from pipeline import PROCESSORS  # noqa: E402
from process import (  # noqa: E402
    PRED_SCALE,
    PRED_VALID_MAX,
    read_wind_uv,
)

DEFAULT_POINTS = (
    "47.61,-122.33",   # Seattle
    "34.05,-118.24",   # Los Angeles
    "39.74,-104.99",   # Denver
    "41.88,-87.63",    # Chicago
    "40.71,-74.00",    # New York
    "29.76,-95.37",    # Houston
    "25.76,-80.19",    # Miami
)


def speed_direction(u, v):
    """u/v [m/s] -> (Betrag [m/s], Richtung "woher" [Grad, True North])."""
    spd = float(np.hypot(u, v))
    direction = float((np.degrees(np.arctan2(-u, -v)) + 360.0) % 360.0)
    return spd, direction


def tile_xy(lat, lon, z):
    """Web-Mercator-Kachel, in der der Punkt liegt."""
    n = 2 ** z
    x = (lon + 180.0) / 360.0 * n
    y = (1.0 - np.arcsinh(np.tan(np.radians(lat))) / np.pi) / 2.0 * n
    return int(np.floor(x)), int(np.floor(y))


def decode_tile(path):
    """[24B Geo-Header][u_len][v_len][u int16 delta][v int16 delta] (gzip)."""
    blob = gzip.decompress(path.read_bytes())
    origin_lng, origin_lat, d_lng, d_lat, rows, cols = struct.unpack("<6f", blob[:24])
    u_len, v_len = struct.unpack("<2I", blob[24:32])
    rows, cols = int(rows), int(cols)

    u_raw = np.frombuffer(blob[32:32 + u_len], dtype="<i2").reshape(rows, cols)
    v_raw = np.frombuffer(
        blob[32 + u_len:32 + u_len + v_len], dtype="<i2"
    ).reshape(rows, cols)

    # Zeilenweise Delta-Kodierung: Spalte 0 ist der absolute Anker.
    u = np.cumsum(u_raw, axis=1) * PRED_SCALE
    v = np.cumsum(v_raw, axis=1) * PRED_SCALE
    return {
        "origin_lng": origin_lng,
        "origin_lat": origin_lat,
        "d_lng": d_lng,
        "d_lat": d_lat,
        "rows": rows,
        "cols": cols,
        "u": u,
        "v": v,
    }


def tile_value(path, lat, lon):
    """u/v an einem Punkt aus einer Kachel (naechster Gitterpunkt)."""
    tile = decode_tile(path)
    col = int(round((lon - tile["origin_lng"]) / tile["d_lng"]))
    row = int(round((tile["origin_lat"] - lat) / tile["d_lat"]))
    if not (0 <= row < tile["rows"] and 0 <= col < tile["cols"]):
        return None, (row, col)
    u = float(tile["u"][row, col])
    v = float(tile["v"][row, col])
    if abs(u) > PRED_VALID_MAX * PRED_SCALE or abs(v) > PRED_VALID_MAX * PRED_SCALE:
        return None, (row, col)
    return (u, v), (row, col)


def angular_delta(a, b):
    return abs((a - b + 180.0) % 360.0 - 180.0)


def api_reference(model, valid_time, lat, lon):
    """Einzelwert der Open-Meteo-API fuer denselben Zeitpunkt (informativ)."""
    import json
    import urllib.request

    ts = datetime.fromtimestamp(valid_time, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M")
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        "&hourly=wind_speed_10m,wind_direction_10m"
        f"&start_hour={ts}&end_hour={ts}"
        f"&models={model}&cell_selection=nearest&elevation=nan"
    )
    with urllib.request.urlopen(url, timeout=30) as fh:
        data = json.load(fh)
    return (
        data["hourly"]["wind_speed_10m"][0],      # km/h
        data["hourly"]["wind_direction_10m"][0],
        data["latitude"],
        data["longitude"],
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--om", required=True, help="Lokale .om-Datei (ein Zeitschritt)")
    parser.add_argument("--model", default="ncep_hrrr_conus_15min",
                        choices=sorted(PROCESSORS),
                        help="Modell: bestimmt Processor, Gitter und Zoomprofil")
    parser.add_argument("--points", default=";".join(DEFAULT_POINTS),
                        help="Testpunkte 'lat,lon;lat,lon;...'")
    parser.add_argument("--zoom", type=int, default=None,
                        help="Zoomstufe fuer den Rueckvergleich "
                             "(Default: max_zoom des Modells)")
    parser.add_argument("--api-model", default=None,
                        help="Optional: Modellname fuer den API-Vergleich, "
                             "z.B. ncep_rrfs_conus_15min oder ncep_nbm_conus")
    parser.add_argument("--keep", action="store_true", help="Arbeitsordner behalten")
    args = parser.parse_args()

    processor_cls = PROCESSORS[args.model]
    zoom = processor_cls.max_zoom if args.zoom is None else args.zoom

    source = Path(args.om)
    if not source.is_file():
        print(f"❌ {source} nicht gefunden")
        return 1

    work = Path(tempfile.mkdtemp(prefix="check_wind_"))
    staged = work / source.name
    shutil.copyfile(source, staged)   # process_om_file loescht die Datei danach

    # Zeitstempel + Layout vorab auslesen (process_om_file loescht die Datei).
    with OmFileReader(str(staged)) as root:
        valid_time = int(root.get_child_by_name("valid_time").read_scalar())
        reference_time = int(root.get_child_by_name("forecast_reference_time").read_scalar())
        u_src, v_src, layout = read_wind_uv(root)

    if u_src.shape == (processor_cls.grid_nx, processor_cls.grid_ny) and u_src.shape != (
        processor_cls.grid_ny, processor_cls.grid_nx
    ):
        # Defensive: falls das .om transponiert vorliegt (Spalten zuerst)
        u_src = u_src.T
        v_src = v_src.T

    if u_src.shape != (processor_cls.grid_ny, processor_cls.grid_nx):
        print(
            f"❌ unerwartete Gitterform {u_src.shape}, "
            f"erwartet {(processor_cls.grid_ny, processor_cls.grid_nx)} ({args.model})"
        )
        return 1

    print(f"Quelle   : {source}")
    print(f"Modell   : {args.model} -> {processor_cls.name}")
    print(f"Format   : {layout}")
    print(f"Run/gueltig: {datetime.fromtimestamp(reference_time, timezone.utc):%Y-%m-%dT%H:%M}Z"
          f" / {datetime.fromtimestamp(valid_time, timezone.utc):%Y-%m-%dT%H:%M}Z")

    started = time.perf_counter()
    processor = processor_cls(output_folder=str(work))
    if not processor.process_om_file(str(staged), output_filename="check"):
        print("❌ process_om_file fehlgeschlagen")
        return 1
    elapsed = time.perf_counter() - started
    tiles_dir = work / "tiles" / "check"
    n_tiles = sum(1 for _ in tiles_dir.rglob("*.bin"))
    print(f"Tiles    : {n_tiles} Kacheln in {elapsed:.2f}s -> {tiles_dir}")

    points = [p for p in args.points.split(";") if p.strip()]
    header = (f"\n{'lat':>7} {'lon':>8} | {'Quelle m/s':>10} {'dir':>5} | "
              f"{'Kachel m/s':>10} {'dir':>5} | {'dSpeed':>6} {'dDir':>5}")
    print(header)
    print("-" * len(header))

    worst_speed = 0.0
    worst_dir = 0.0
    for spec in points:
        lat, lon = (float(x) for x in spec.split(","))

        x, y = processor_cls._forward(lat, lon)
        row_src = int(round((float(y) - processor_cls.origin_y) / processor_cls.dy))
        col_src = int(round((float(x) - processor_cls.origin_x) / processor_cls.dx))

        tx, ty = tile_xy(lat, lon, zoom)
        tile_path = tiles_dir / str(zoom) / str(tx) / f"{ty}.bin"

        if not (0 <= row_src < processor_cls.grid_ny
                and 0 <= col_src < processor_cls.grid_nx):
            print(f"{lat:7.2f} {lon:8.2f} | ausserhalb des Modellgitters")
            continue
        s_u, s_v = float(u_src[row_src, col_src]), float(v_src[row_src, col_src])
        if not tile_path.is_file():
            print(f"{lat:7.2f} {lon:8.2f} | Quelle {s_u:6.2f}/{s_v:6.2f} | "
                  f"keine Kachel {zoom}/{tx}/{ty}")
            continue

        uv, (row_t, col_t) = tile_value(tile_path, lat, lon)
        if uv is None:
            print(f"{lat:7.2f} {lon:8.2f} | Quelle {s_u:6.2f}/{s_v:6.2f} | "
                  f"Kachelwert ungueltig (row={row_t} col={col_t})")
            continue

        s_spd, s_dir = speed_direction(s_u, s_v)
        t_spd, t_dir = speed_direction(*uv)
        d_spd = t_spd - s_spd
        d_dir = angular_delta(t_dir, s_dir)
        worst_speed = max(worst_speed, abs(d_spd))
        worst_dir = max(worst_dir, d_dir)
        print(f"{lat:7.2f} {lon:8.2f} | {s_spd:10.2f} {s_dir:5.0f} | "
              f"{t_spd:10.2f} {t_dir:5.0f} | {d_spd:+6.2f} {d_dir:5.1f}")

    print(f"\nmax. Abweichung: {worst_speed:.2f} m/s, {worst_dir:.1f} Grad")
    print(f"(Bilinear-Resampling auf {processor_cls.default_resolution} Grad -> "
          "kleine Abweichungen sind normal;\n"
          " bei sehr schwachem Wind ist die Richtung ohnehin instabil.)")

    if args.api_model:
        print(f"\nAPI-Referenz ({args.api_model}, aktuell verfuegbarer Run):")
        print(f"{'lat':>7} {'lon':>8} | {'API km/h':>8} {'dir':>5} | {'Kachel m/s':>10} {'dir':>5}")
        for spec in points:
            lat, lon = (float(x) for x in spec.split(","))
            try:
                a_spd, a_dir, _, _ = api_reference(args.api_model, valid_time, lat, lon)
            except Exception as exc:      # noqa: BLE001 - nur Diagnose
                print(f"{lat:7.2f} {lon:8.2f} | API-Fehler: {exc}")
                continue
            tx, ty = tile_xy(lat, lon, zoom)
            uv, _ = tile_value(tiles_dir / str(zoom) / str(tx) / f"{ty}.bin", lat, lon)
            if uv is None:
                print(f"{lat:7.2f} {lon:8.2f} | {a_spd:8.1f} {a_dir:5.0f} | -")
                continue
            t_spd, t_dir = speed_direction(*uv)
            print(f"{lat:7.2f} {lon:8.2f} | {a_spd:8.1f} {a_dir:5.0f} | "
                  f"{t_spd:10.2f} {t_dir:5.0f}")
        print("(API nutzt den aktuell verfuegbaren Run -> kann ein anderer sein als die Datei.)")

    if args.keep:
        print(f"\nArbeitsordner: {work}")
    else:
        shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
