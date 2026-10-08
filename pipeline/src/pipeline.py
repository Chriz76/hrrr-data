import glob
import json
import os
import shutil
import sys
import time
from datetime import datetime, timedelta, timezone
import requests

# Erlaubt den Import von process.py im selben Ordner
if "__file__" in globals():
    sys.path.append(os.path.dirname(__file__))
else:
    sys.path.append(os.path.abspath("."))

try:
    from process import HrrrWindProcessor
except ImportError:
    pass  # Die Klasse 'HrrrWindProcessor' ist in Colab bereits im RAM!


# --- KONFIGURATION ---
OUTPUT_DIR = "./output"
TEMP_OM_DIR = "./om_temp"
API_VERSION = "1.1.0"

# Open-Meteo Modellname aus S3. Es gibt nur noch EINEN Datensatz: der
# 15-Minuten-Datensatz deckt denselben Zeitraum ab wie der stündliche
# ('ncep_hrrr_conus'), also werden auch die ganzen Stunden aus ihm geholt.
MODEL_NAME_15MIN = "ncep_hrrr_conus_15min"  # HRRR: stündliche Runs, 15-Min-Schritte
MAX_PMTILES_COUNT = 200  # Maximal zu behaltende PMTiles-Dateien
# Auskommentiert: das Feld-WebP entfaellt, es gibt keine WebP-Dateien mehr zu zaehlen.
# MAX_WEBP_COUNT = 200  # Maximal zu behaltende WebP-Dateien

# Horizont:
#   +0 .. +HORIZON_15MIN_HOURS   -> alle 15 Minuten (HHMM)
#   danach .. +HORIZON_END_HOURS -> nur ganze Stunden (HH), gleicher Datensatz
HORIZON_15MIN_HOURS = 6    # 15-Min-Schritte bis +6h (einschließlich)
HORIZON_END_HOURS = 18     # letzter Schritt (volle Stunde) bei +18h

os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(TEMP_OM_DIR, exist_ok=True)


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


def cleanup_old_files(output_dir, pattern, max_keep, label):
    """Löscht ältere Dateien im Ausgabeordner basierend auf dem Dateinamen."""
    files = glob.glob(os.path.join(output_dir, pattern))

    if len(files) > max_keep:
        print(f"\n🧹 Bereinige alte {label} ({len(files)} vorhanden, maximal {max_keep} erlaubt)...")
        # Alphabethische Sortierung nach Dateinamen (älteste Timestamps stehen vorne)
        files.sort()

        # Alle Dateien bis auf die letzten max_keep (die neuesten) löschen
        files_to_delete = files[:-max_keep]
        for file_path in files_to_delete:
            try:
                os.remove(file_path)
                print(f"   🗑️ Gelöscht: {os.path.basename(file_path)}")
            except Exception as e:
                print(f"   ⚠️ Fehler beim Löschen von {file_path}: {e}")
        print(f"✅ Bereinigung abgeschlossen. Es verbleiben {max_keep} {label}.")


# Auskommentiert: nur fuer das Feld-WebP relevant.
# def cleanup_old_webps(output_dir, max_keep=200):
#     """Löscht ältere WebP-Dateien im Ausgabeordner basierend auf dem Dateinamen."""
#     cleanup_old_files(output_dir, "*.webp", max_keep, "WebP-Dateien")


def cleanup_old_pmtiles(output_dir, max_keep=200):
    """Löscht ältere PMTiles-Dateien im Ausgabeordner basierend auf dem Dateinamen."""
    cleanup_old_files(output_dir, "*_dir.pmtiles", max_keep, "PMTiles-Dateien")
        

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

    # Initialisiere den echten HrrrWindProcessor
    processor = HrrrWindProcessor(output_folder=OUTPUT_DIR, width=2000)

    # Key im HHMM-Format für den 15m Run
    detected_current_time_key = target_time.strftime("%Y%m%d_%H%M")
    processed_timestamps_15m = []

    current_step_time = target_time
    consecutive_missing = 0

    # 1. 15-MINUTEN RUN VERARBEITEN (alle 15 Min bis +HORIZON_15MIN_HOURS)
    horizon_15min = target_time + timedelta(hours=HORIZON_15MIN_HOURS)
    while consecutive_missing < 2 and current_step_time <= horizon_15min:
        time_key = current_step_time.strftime("%Y%m%d_%H%M")  # HHMM-Format
        # Traegt nur noch den Zeitstempel als Namensbasis; es wird ausschliesslich
        # eine PMTiles-Datei ("...Z_dir.pmtiles") geschrieben.
        output_name = f"{time_key}Z"
        iso_file_name = f"{current_step_time.strftime('%Y-%m-%dT%H%M')}.om"

        url_om = f"https://openmeteo.s3.amazonaws.com/data_spatial/{MODEL_NAME_15MIN}/{year}/{month}/{day}/{run_hour_z}/{iso_file_name}"
        om_path = os.path.join(TEMP_OM_DIR, iso_file_name)

        print(f"   -> Downloade 15m-Schritt: {iso_file_name}...")

        if download_file(url_om, om_path):
            consecutive_missing = 0
            success = processor.process_om_file(
                om_path, output_filename=output_name
            )

            if os.path.exists(om_path):
                os.remove(om_path)

            if success:
                processed_timestamps_15m.append(time_key)
                print(f"      ✅ Erfolgreich prozessiert -> {time_key}Z_dir.pmtiles")
        else:
            consecutive_missing += 1
            print(
                f"   ℹ️ Datei {iso_file_name} im Run {run_hour_z} nicht mehr vorhanden."
            )

        current_step_time += timedelta(minutes=15)

    # 2. NUR NOCH GANZE STUNDEN AUS DEMSELBEN 15-MINUTEN-RUN (bis +18h)
    #
    #    Kein Wechsel mehr auf 'ncep_hrrr_conus' (stündlich): der 15-Minuten-
    #    Datensatz deckt denselben Zeitraum ab, enthält also auch die :00-Schritte.
    #    Ab hier wird nur noch jede volle Stunde geladen (Key im HH-Format), damit
    #    die Dateizahl nicht explodiert. Es gibt KEINE separate Run-Rückwärtssuche:
    #    es gelten dieselben year/month/day/run_hour_z wie für die 15-Min-Schritte.
    timestamps_hourly = []

    horizon_end = target_time + timedelta(hours=HORIZON_END_HOURS)

    # Erste volle Stunde NACH dem 15-Min-Horizont. horizon_15min selbst wurde im
    # 15-Min-Loop bereits als HHMM-Schritt verarbeitet -> keine Doppelverarbeitung.
    current_hour_time = horizon_15min + timedelta(hours=1)
    consecutive_missing = 0

    while current_hour_time <= horizon_end and consecutive_missing < 2:
        time_key = current_hour_time.strftime("%Y%m%d_%H")  # HH-Format
        output_name = f"{time_key}Z"
        iso_file_name = f"{current_hour_time.strftime('%Y-%m-%dT%H%M')}.om"

        url_om = f"https://openmeteo.s3.amazonaws.com/data_spatial/{MODEL_NAME_15MIN}/{year}/{month}/{day}/{run_hour_z}/{iso_file_name}"
        om_path = os.path.join(TEMP_OM_DIR, iso_file_name)

        print(f"   -> Downloade Stundenschritt (15m-Datensatz): {iso_file_name}...")

        if download_file(url_om, om_path):
            consecutive_missing = 0
            success = processor.process_om_file(
                om_path, output_filename=output_name
            )

            if os.path.exists(om_path):
                os.remove(om_path)

            if success:
                timestamps_hourly.append(time_key)
                print(f"      ✅ Erfolgreich prozessiert -> {time_key}Z_dir.pmtiles")
        else:
            consecutive_missing += 1
            print(
                f"   ℹ️ Datei {iso_file_name} im Run {run_hour_z} nicht (mehr) vorhanden."
            )

        current_hour_time += timedelta(hours=1)

    # 3. BEIDE TIMESTAMPS VERBINDEN & INDEX.JSON EINMALIG GENERIEREN
    all_timestamps = sorted(
        list(set(processed_timestamps_15m + timestamps_hourly))
    )

    if all_timestamps:
        index_path = os.path.join(OUTPUT_DIR, "index.json")

        current_hour = (
            detected_current_time_key
            if detected_current_time_key in all_timestamps
            else all_timestamps[0]
        )

        index_data = {
            "generated_at": datetime.now(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "available_timestamps": all_timestamps,
            "current_hour": current_hour,
            "step_type": "15min",
            "api_version": API_VERSION,
        }

        print(f"\n📝 Generiere {index_path}...")
        print(f"   -> 15-Min-Schritte (HHMM): {len(processed_timestamps_15m)}")
        print(f"   -> Stunden-Schritte (HH, 15m-Datensatz): {len(timestamps_hourly)}")
        print(f"   -> Gesamt kombiniert:      {len(all_timestamps)}")
        print(f"   -> Standard-Fokus (current_hour): {current_hour}")

        with open(index_path, "w") as f:
            json.dump(index_data, f, indent=2)
        print("✅ index.json erfolgreich erstellt!")
    else:
        print("⚠️ Warnung: Keine Daten-Timestamps erzeugt.")

    # Alte WebP-Dateien nach Dateinamen-Sortierung bereinigen
    # (auskommentiert: es werden keine Feld-WebPs mehr erzeugt)
    # cleanup_old_webps(OUTPUT_DIR, max_keep=MAX_WEBP_COUNT)

    # Alte PMTiles-Dateien nach Dateinamen-Sortierung bereinigen
    cleanup_old_pmtiles(OUTPUT_DIR, max_keep=MAX_PMTILES_COUNT)
    
    # Aufräumen
    if os.path.exists(TEMP_OM_DIR):
        shutil.rmtree(TEMP_OM_DIR)
        print("🧹 Temporärer Ordner bereinigt!")

    print("\n🎉 PIPELINE SAUBER BEENDET!")


if __name__ == "__main__":
    run_hrrr_pipeline()