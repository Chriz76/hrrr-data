import json
import os
import shutil
import time
from datetime import datetime, timedelta, timezone

import requests

# process.py liegt im selben Ordner: beim Aufruf `python pipeline/src/pipeline.py`
# ist dieses Verzeichnis bereits sys.path[0].
from process import HrrrWindProcessor


# --- KONFIGURATION ---
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "./output")
TEMP_OM_DIR = os.environ.get("TEMP_OM_DIR", "./om_temp")
API_VERSION = "1.1.0"

# Open-Meteo Modellname aus S3. Es gibt nur noch EINEN Datensatz: der
# 15-Minuten-Datensatz deckt denselben Zeitraum ab wie der stündliche
# ('ncep_hrrr_conus'), also werden auch die ganzen Stunden aus ihm geholt.
MODEL_NAME_15MIN = "ncep_hrrr_conus_15min"  # HRRR: stündliche Runs, 15-Min-Schritte

# Ablage der Tiles im Deploy-Ordner (siehe process.py):
#     tiles/<timestamp>/<z>/<x>/<y>.bin   (bereits gzip-komprimiert)
# Format-, Bounds- und Coverage-Infos stehen einmalig in index.json.
TILES_DIR_NAME = "tiles"

# Budget eines vollen Runs: ~37 Zeitstempel (25 x 15-min bis +6h, dann 12
# stuendliche bis +18h) x ~613 Kacheln = ~22.700 Dateien + index.json.
#
# Der Deploy-Ordner enthaelt immer nur den aktuellen Run: alte Tile-Sets werden
# zu Beginn des Laufs entfernt. Alte Runs in der Zone raeumt upload_bunny.py
# mit --prune ab.

# Horizont (per Env ueberschreibbar):
#   +0 .. +HORIZON_15MIN_HOURS   -> alle 15 Minuten (HHMM)
#   danach .. +HORIZON_END_HOURS -> nur ganze Stunden (HH), gleicher Datensatz
HORIZON_15MIN_HOURS = int(os.environ.get("HORIZON_15MIN_HOURS", "6"))
HORIZON_END_HOURS = int(os.environ.get("HORIZON_END_HOURS", "18"))


def download_file(url, local_path):
    """Hilfsfunktion für Downloads.

    Gibt bei HTTP 404 sofort False zurück (kein unnötiges Warten/Retry).
    """
    max_retries = 3
    for attempt in range(1, max_retries + 1):
        try:
            response = requests.get(url, timeout=15, stream=True)
            if response.status_code == 200:
                with open(local_path, "wb") as f:
                    for chunk in response.iter_content(chunk_size=16384):
                        f.write(chunk)
                return True
            elif response.status_code == 404:
                return False
            else:
                print(
                    f"   ⚠️ Download-Versuch {attempt} fehlgeschlagen (Status: {response.status_code})"
                )
        except Exception as e:
            print(f"   ⚠️ Download-Fehler bei Versuch {attempt}: {e}")

        if attempt < max_retries:
            wait_time = attempt * 2
            print(f"   ⏳ Warte {wait_time} Sekunden vor nächstem Versuch...")
            time.sleep(wait_time)

    return False


def build_steps(target_time):
    """Zeitschritte des Laufs als Liste von (Zeit, Zeitstempel-Format, Label).

    Erst alle 15 Minuten bis +HORIZON_15MIN_HOURS, danach nur ganze Stunden bis
    +HORIZON_END_HOURS - beides aus demselben 15-Minuten-Datensatz.
    """
    steps = []

    horizon_15min = target_time + timedelta(hours=HORIZON_15MIN_HOURS)
    step_time = target_time
    while step_time <= horizon_15min:
        steps.append((step_time, "%Y%m%d_%H%M", "15-Minuten"))
        step_time += timedelta(minutes=15)

    # Erste volle Stunde NACH dem 15-Min-Horizont: horizon_15min selbst wurde
    # oben schon als HHMM-Schritt verarbeitet -> keine Doppelverarbeitung.
    horizon_end = target_time + timedelta(hours=HORIZON_END_HOURS)
    step_time = horizon_15min + timedelta(hours=1)
    while step_time <= horizon_end:
        steps.append((step_time, "%Y%m%d_%H", "Stunden"))
        step_time += timedelta(hours=1)

    return steps


def publish_run_id(run_id):
    """Gibt die Run-ID an GitHub Actions weiter (Ordnername unter runs/).

    Ausserhalb von Actions (lokal) ist GITHUB_OUTPUT nicht gesetzt - dann
    passiert einfach nichts.
    """
    output_file = os.environ.get("GITHUB_OUTPUT")
    if output_file:
        with open(output_file, "a", encoding="utf-8") as fh:
            fh.write(f"run_id={run_id}\n")


def build_coverage(coords):
    """(z, x, y)-Tripel -> {"z": {"x": [[y0, y1], ...]}}.

    Pro Spalte x werden die vorhandenen y zu Laeufen zusammengefasst; normaler-
    weise ist das genau ein Paar [y0, y1], weil das Gebiet zusammenhaengend ist.
    Das Format ist exakt (keine Mercator-Nachrechnung im Client noetig) und
    kompakt: fuer den ganzen Run wenige hundert Byte.
    """
    per_zoom = {}
    for z, x, y in coords:
        per_zoom.setdefault(int(z), {}).setdefault(int(x), []).append(int(y))

    coverage = {}
    for z, columns in sorted(per_zoom.items()):
        cov_z = {}
        for x, ys in sorted(columns.items()):
            runs = []
            start = prev = min(ys)
            for y in sorted(ys)[1:]:
                if y == prev + 1:
                    prev = y
                    continue
                runs.append([start, prev])
                start = prev = y
            runs.append([start, prev])
            cov_z[str(x)] = runs
        coverage[str(z)] = cov_z
    return coverage


def run_hrrr_pipeline():
    hrrr_run_env = os.environ.get("HRRR_TARGET_RUN") or os.environ.get("TARGET_RUN")

    if not hrrr_run_env:
        # Fallback: Falls keine Umgebungsvariable gesetzt ist, nutzen wir vorangehende UTC-Stunde
        fallback_time = datetime.now(timezone.utc) - timedelta(hours=2)
        hrrr_run_env = fallback_time.strftime("%Y-%m-%dT%H:00")
        print(f"ℹ️ Keine 'HRRR_TARGET_RUN'/'TARGET_RUN' Variable gesetzt. Verwende automatischen Fallback-Run: {hrrr_run_env}")

    target_time = datetime.strptime(
        hrrr_run_env, "%Y-%m-%dT%H:%M"
    ).replace(tzinfo=timezone.utc)

    year = target_time.strftime("%Y")
    month = target_time.strftime("%m")
    day = target_time.strftime("%d")
    run_hour_z = target_time.strftime("%H") + "00Z"

    print(f"\n========================================================")
    print(
        f"START HRRR-PIPELINE (15-Minuten) - Run: {target_time.strftime('%Y-%m-%dT%H:%M')}Z"
    )
    print(f"========================================================")

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    os.makedirs(TEMP_OM_DIR, exist_ok=True)

    # Der Deploy-Ordner enthaelt nur den aktuellen Run: Tile-Sets eines frueheren
    # (z.B. lokalen) Laufs wuerden sonst mit hochgeladen.
    tiles_root = os.path.join(OUTPUT_DIR, TILES_DIR_NAME)
    if os.path.isdir(tiles_root):
        shutil.rmtree(tiles_root)
        print(f"🧹 Alte Tile-Sets entfernt: {tiles_root}")

    processor = HrrrWindProcessor(output_folder=OUTPUT_DIR)

    # Key im HHMM-Format; dient gleichzeitig als Ordnername unter runs/ in der
    # Storage-Zone (siehe upload_bunny.py).
    detected_current_time_key = target_time.strftime("%Y%m%d_%H%M")
    publish_run_id(detected_current_time_key)

    processed_timestamps = []
    # Kachelmengen je Zeitstempel (fuer die Coverage in index.json)
    coords_per_step = []
    consecutive_missing = 0

    for step_time, key_format, label in build_steps(target_time):
        if consecutive_missing >= 2:
            print(
                f"   ℹ️ Zwei Schritte hintereinander fehlend - Horizont endet vor "
                f"{step_time.strftime('%Y-%m-%dT%H:%M')}Z"
            )
            break

        time_key = step_time.strftime(key_format)
        # Traegt nur noch den Zeitstempel als Namensbasis; pro Kachel entsteht
        # eine einzelne gzip-Datei unter tiles/<timestamp>/<z>/<x>/<y>.bin.
        output_name = f"{time_key}Z"
        iso_file_name = f"{step_time.strftime('%Y-%m-%dT%H%M')}.om"

        url_om = f"https://openmeteo.s3.amazonaws.com/data_spatial/{MODEL_NAME_15MIN}/{year}/{month}/{day}/{run_hour_z}/{iso_file_name}"
        om_path = os.path.join(TEMP_OM_DIR, iso_file_name)

        print(f"   -> Downloade {label}-Schritt: {iso_file_name}...")

        if not download_file(url_om, om_path):
            consecutive_missing += 1
            print(
                f"   ℹ️ Datei {iso_file_name} im Run {run_hour_z} nicht (mehr) vorhanden."
            )
            continue

        consecutive_missing = 0
        # process_om_file loescht die .om-Datei am Ende selbst.
        if processor.process_om_file(om_path, output_filename=output_name):
            processed_timestamps.append(time_key)
            print(f"      ✅ Erfolgreich prozessiert -> {TILES_DIR_NAME}/{time_key}Z/")
            coords_per_step.append(set(processor.last_tile_coords))

    # --- INDEX.JSON ---
    all_timestamps = sorted(set(processed_timestamps))

    if not all_timestamps:
        print("⚠️ Warnung: Keine Daten-Timestamps erzeugt.")
    else:
        index_path = os.path.join(OUTPUT_DIR, "index.json")

        current_hour = (
            detected_current_time_key
            if detected_current_time_key in all_timestamps
            else all_timestamps[0]
        )

        # Jeder Run liegt unter runs/<run>/; index.json im Root ist der einzige
        # Einstiegspunkt: Datenliste, Format, Bounds und Coverage in einer Datei.
        run_prefix = f"runs/{detected_current_time_key}"

        coverage_set = set().union(*coords_per_step) if coords_per_step else set()
        if coords_per_step and any(
            coords != coords_per_step[0] for coords in coords_per_step
        ):
            print(
                "⚠️ Kachelmenge unterscheidet sich zwischen Zeitstempeln "
                "-> Coverage ist die Vereinigung"
            )

        index_data = {
            "generated_at": datetime.now(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "run": detected_current_time_key,
            "available_timestamps": all_timestamps,
            "current_hour": current_hour,
            "step_type": "15min",
            "api_version": API_VERSION,
            # {timestamp} = "20261008_2000Z"; {z}/{x}/{y} setzt die App ein.
            "tiles_url": f"{run_prefix}/{TILES_DIR_NAME}/{{timestamp}}/{{z}}/{{x}}/{{y}}.bin",
            # Format/Pyramide/Bounds sind pro Run konstant -> einmal hier.
            **processor.format_descriptor(),
            # Vorhandene Kacheln: {z: {x: [[y0, y1], ...]}}
            "coverage": build_coverage(coverage_set),
        }

        print(f"\n📝 Generiere {index_path}...")
        # HHMM-Keys sind 13 Zeichen ("YYYYMMDD_HHMM"), Stunden-Keys 11.
        count_15min = sum(1 for ts in all_timestamps if len(ts) > 11)
        print(f"   -> 15-Min-Schritte (HHMM): {count_15min}")
        print(f"   -> Stunden-Schritte (HH, 15m-Datensatz): {len(all_timestamps) - count_15min}")
        print(f"   -> Gesamt kombiniert:      {len(all_timestamps)}")
        print(f"   -> Standard-Fokus (current_hour): {current_hour}")
        if coords_per_step:
            print(f"   -> Kacheln je Zeitstempel:      {len(coords_per_step[0])}")

        with open(index_path, "w") as f:
            json.dump(index_data, f, indent=2)
        print("✅ index.json erfolgreich erstellt!")

    # Aufräumen
    if os.path.exists(TEMP_OM_DIR):
        shutil.rmtree(TEMP_OM_DIR)
        print("🧹 Temporärer Ordner bereinigt!")

    print("\n🎉 PIPELINE SAUBER BEENDET!")


if __name__ == "__main__":
    run_hrrr_pipeline()
