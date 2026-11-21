# hrrr-data

Pipeline, die 10-m-Wind (Open-Meteo `.om`-Dateien) in vor-komprimierte
Web-Mercator-Kacheln umrechnet und nach Bunny Storage hochlaedt.

Das Modell ist per `MODEL` waehlbar. **Aktiv ist RRFS** - Open-Meteo schaltet
HRRR Ende November 2026 ab:

| `MODEL` | Rolle | Horizont |
|---|---|---|
| `ncep_rrfs_conus_15min` (**aktiv**, gesetzt in `sync.yml`) | Nachfolger, gleiches CONUS-Gitter | +18 h (erste Datei +0:15, **kein** +0:00) |
| `ncep_hrrr_conus_15min` (Default im Code) | Vorgaenger, nur noch Rueckfallebene | +18 h |
| `knmi_harmonie_arome_europe` | KNMI HARMONIE Europe, 5,5 km, stuendlich | bis +60 h verfuegbar; `sync-knmi.yml` liest +0...+28 h |
| `ncep_nbm_conus` | NBM CONUS, 2,54 km, stuendlich | stuendlich bis +36 h, dann 3-stuendlich bis ~+260 h; `sync-nbm.yml` liest +0...+30 h |

Welches Windfeld in der `.om` steckt (u/v oder Betrag/Richtung), erkennt
`process.py` selbst - es gibt keinen weiteren Modellschalter. Ein Wechsel
zurueck auf HRRR ist damit eine Env-Variable.

KNMI-Besonderheiten (in `process.py` als `KnmiWindProcessor` gekapselt):

- rotiertes Lat/Lon (`o_lat_p=35`, `lon_0=-8`), 676 x 564, `dx = dy = -0,05°`
  (= 5,56 km), Daten zeilenweise Sued->Nord
- Zielgitter 0,05° regulaeres Lat/Lon; `max_zoom = 6`, `finest_stride = 1`
  (eine z6-Kachel = 4x Flaeche einer z7-Kachel bei voller Aufloesung)
- geografische Huelle **37,74...69,55 °N / -42,08...38,76 °O**; die `BBOX` im
  `crs_wkt` der .om ist nur die SW-/NE-Ecke und deckt Island/Nordatlantik nicht ab
- Quelle ist `wind_speed_10m`/`wind_direction_10m` (True North, "woher");
  `read_wind_uv()` rechnet das in geografische u/v um
- Erwartung: ~194 Kacheln/Zeitstempel, Ø 7,7 KiB, ~1,5 MiB/ts,
  29 ts (+0...+28) = ~42 MiB und ~5.600 Dateien

NBM-Besonderheiten (in `process.py` als `NbmWindProcessor` gekapselt):

- eigenes LCC-Gitter (Kugel 6371200, SP 25, `lon_0 = -95`, `lat_0 = 0`),
  2345 x 1597, `dx = dy = 2539,7 m` - **nicht** das HRRR-Gitter; die
  Projektion ist deshalb eine parametrisierte `_LambertConformal`-Klasse
- geografische Huelle: 19,23...54,37 °N / -126,28...-59,04 °O (Gitterecken);
  der abgetastete Rand reicht etwas weiter, wie bei HRRR
- Zielgitter **0,0225°** regulaeres Lat/Lon = 2505 m meridional und damit
  ohne Unterabtastung gegenueber den 2539,7 m der Quelle
- `max_zoom = 7`, `finest_stride = 1`: eine z7-Kachel hat hier 125 Knoten pro
  Kante (HRRR: 112,5) - gleiche Groessenordnung, keine Frontend-Aenderung
- Quelle ist `wind_speed_10m`/`wind_direction_10m` (m/s, True North, "woher")
- Erwartung: ~600 Kacheln/Zeitstempel (Domain ist groesser als HRRR),
  31 ts (+0...+30) = ~19.000 Dateien, ~400 MiB

Alle Modelle schreiben dieselbe Format-ID `hrrr-wind-int16-delta` - das
Payload-Layout (24 B Geo-Header, int16-Delta, Sentinel, Skala) ist identisch,
damit braucht der Client nur eine einzige ID in `SUPPORTED_FORMAT_IDS`.
Die Modellunterscheidung passiert ausschliesslich ueber die Pull Zone.

Die Frische unterscheidet sich deutlich: RRFS ist ~2,5 h nach Run-Start
vollstaendig, HRRR war es nach ~1,5 h. Der Trigger (Cloudflare Worker) sucht
deshalb ein 6-h-Fenster ab, nicht mehr nur 6 Stunden ab der vollen Stunde.

## Ablauf

```
pipeline/src/pipeline.py       .om herunterladen -> resampeln -> Tiles schreiben -> index.json
pipeline/src/upload_bunny.py   Run nach runs/<run>/ hochladen -> index.json umschalten -> alte Runs loeschen
.github/workflows/sync.yml     Startet beide Schritte in GitHub Actions
```

Starten (manuell):

```
TARGET_RUN=2026-10-27T12:00 OUTPUT_DIR=./t python pipeline/src/pipeline.py
```

`TARGET_RUN` ist der Run im Format `YYYY-MM-DDTHH:MM` (UTC). Fehlt er,
wird `jetzt - 3h` auf die volle Stunde verwendet (der neueste RRFS-Run ist
~2,5 h nach Run-Start verfuegbar). Weitere Env-Variablen:
`MODEL`, `OUTPUT_DIR`, `TEMP_OM_DIR`, `HORIZON_15MIN_HOURS` (Default 6),
`HORIZON_END_HOURS` (Default 18), `PRED_GZIP_LEVEL`.

RRFS-Beispiel:

```
MODEL=ncep_rrfs_conus_15min TARGET_RUN=2026-11-15T09:00 OUTPUT_DIR=./t \
    python pipeline/src/pipeline.py
```

KNMI-Beispiel (29 stuendliche Schritte, `z6`):

```
MODEL=knmi_harmonie_arome_europe TARGET_RUN=2026-11-21T06:00 \
    HORIZON_END_HOURS=28 OUTPUT_DIR=./t python pipeline/src/pipeline.py
```

NBM-Beispiel (31 stuendliche Schritte, `z7`, 0,0225 Grad):

```
MODEL=ncep_nbm_conus TARGET_RUN=2026-11-21T11:00 OUTPUT_DIR=./t \
    python pipeline/src/pipeline.py
```

`HORIZON_END_HOURS` faellt hier auf `NbmWindProcessor.default_horizon_hours`
(30 h) zurueck; explizit setzen ueberschreibt das.

Schneller Smoke-Test (nur zwei Schritte): `HORIZON_END_HOURS=1` setzen.

## Einzelnen Zeitschritt pruefen (ohne Volumen)

`pipeline/tools/check_wind.py` prueft die ganze Kette an **einem**
Zeitschritt und braucht dafuer nur eine lokale `.om`-Datei (13 MB):

```
python pipeline/tools/check_wind.py --om /pfad/rrfs_1200.om \
    --api-model ncep_rrfs_conus_15min
```

Es laesst die echte Pipeline auf der Datei laufen, dekodiert die erzeugten
Kacheln an sieben Testpunkten und vergleicht sie mit dem Quellwert an der
entsprechenden LCC-Gitterzelle. Geprueft werden damit Format-Erkennung,
Umrechnung, Gitterabbildung, Nordausrichtung, Delta-Kodierung und Skala -
also alles ausser der Zeitreihen-Logik (index.json, Horizont, Pruning).

Die Richtungs*konvention* (True North, "woher") kann der Rueckvergleich
nicht beweisen, weil sie auf dem Rueckweg wieder herausfaellt; dafuer dient
`--api-model` (winziger JSON-Request). Referenzwerte aus dem Betrieb:
max. Abweichung Quelle<->Kachel ca. 0,9 m/s und 3 Grad (bilineares
Resampling von 3 km auf 0,025 Grad).

Ein voller Lauf umfasst ~37 Zeitstempel (25 x 15 min bis +6 h, danach 12
stuendliche bis +18 h) mit ~613 Kacheln je Zeitstempel.

## Layout in der Storage-Zone

```
runs/<run>/tiles/<ts>/<z>/<x>/<y>.bin    vor-komprimiert (gzip)
index.json                               Root: Timestamps, Format, Bounds, Coverage, tiles_url
```

`index.json` ist der einzige Einstiegspunkt und wird erst umgeschaltet, wenn der
komplette Run oben ist (atomarer Switch). Danach werden aeltere Runs geloescht
(`--keep-runs`, Default 1).

## Zone und Workflows

Es gibt **eine** produktive Storage-Zone (`gdsfgfsdgsfd`, Pull Zone
`hrrr.b-cdn.net`). `sync.yml` schreibt dorthin und liefert seit der
Umschaltung RRFS-Daten - der Zonen- und Hostname ist damit historisch, der
Client musste dafuer nicht angefasst werden.

| Workflow | Modell | Zone | Secret | Status |
|---|---|---|---|---|
| `sync.yml` | `ncep_rrfs_conus_15min` | `gdsfgfsdgsfd` | `BUNNY_STORAGE_PASSWORD` | **Produktion** |
| `sync-knmi.yml` | `knmi_harmonie_arome_europe` | `rrfs` (Test) | `BUNNY_STORAGE_PASSWORD_RRFS` | manuell, Testbetrieb |
| `sync-nbm.yml` | `ncep_nbm_conus` | `rrfs` (Test) | `BUNNY_STORAGE_PASSWORD_RRFS` | manuell, Testbetrieb |
| `sync-rrfs.yml` | `ncep_rrfs_conus_15min` | `rrfs` | `BUNNY_STORAGE_PASSWORD_RRFS` | Auslauf, nach dem Cutover loeschen |

`sync-knmi.yml` wird **manuell** per `workflow_dispatch` gestartet (kein
Trigger) und schreibt vorerst in die ungenutzte Zone `rrfs`, damit kein
zusaetzliches Bunny-Setup noetig ist. Fuer den Produktivbetrieb nur
`BUNNY_STORAGE_ZONE` und das Secret auf eine eigene KNMI-Zone umstellen.
Die Run-IDs sind dort 11-stellig (`YYYYMMDD_HH`) - ein Trigger muss das
gegenueber dem 13-stelligen `YYYYMMDD_HHMM` der 15-Minuten-Modelle
unterscheiden.

`sync-nbm.yml` laeuft ebenfalls nur manuell und schreibt vorerst in dieselbe
Testzone `rrfs` (Secret der Zone: `BUNNY_STORAGE_PASSWORD_RRFS`). Achtung: KNMI
und NBM teilen sich dort `index.json` im Root und raeumen sich mit `--prune`
gegenseitig die `runs/` weg - es ist also immer nur ein Modell live. Fuer den
Produktivbetrieb eigene Zone + eigenes Secret.
Horizont 30 h (31 Zeitstempel), Timeout 60 min, ~19.000 Dateien pro Run.

`sync-rrfs.yml` war der Staging-Pfad: gleiche Pipeline, eigene Zone, damit
RRFS getestet werden konnte, ohne den produktiven Index umzuschalten. Nach
dem Cutover ist er redundant und sollte entfernt werden (auch auf `master`),
sonst bleibt ein dispatchbarer Workflow uebrig, der in eine verwaiste Zone
schreibt.

## Rollback

Zurueck auf HRRR = `MODEL` in `sync.yml` auf `ncep_hrrr_conus_15min` setzen
und neu bauen. Das geht nur, solange Open-Meteo HRRR noch vorhaelt. Nach dem
ersten RRFS-Lauf liegen in der Zone `--keep-runs` Runs (Default 1), d. h. das
Rollback-Fenster ueber die Zone selbst ist kurz; fuer den ersten Lauf nach der
Umschaltung empfiehlt sich `--keep-runs 3`.

## Kachel-Format

```
[Geo-Header 24 B][u_len 4 B][v_len 4 B][u_int16][v_int16]   als EIN gzip-Strom
```

- Geo-Header: 6 x float32 LE (`origin_lng`, `origin_lat`, `delta_lng`,
  `delta_lat`, `rows`, `cols`)
- beide Streams row-major, North-Up (Zeile 0 = Nordrand), pro Zeile
  delta-kodiert, Spalte 0 ist der absolute Anker
- Skala 0.05 m/s pro int16-Schritt, Sentinel `-20000` = NaN

## Einmalige Bunny-Konfiguration

Die Storage-HTTP-API kann keine Response-Header speichern - alle Header
existieren ausschliesslich als Edge Rule im Pull Zone:

| Regel | Einstellung |
|---|---|
| Override Cache Time | `0`, Condition *File Extension = json* |
| Set Response Header (`*.bin`) | `Content-Encoding: gzip` |
| Set Response Header (`*.bin`) | `Cache-Control: no-store, no-transform` |
| Set Response Header (`*.json`) | `Cache-Control: no-store` (Browser-Cache, nicht Edge) |
| CORS | `Access-Control-Allow-Origin: *` |

- `Content-Encoding: gzip` ist Pflicht, weil die Kacheln bereits komprimiert
  sind: der Browser entpackt sie dann transparent, `response.arrayBuffer()`
  liefert die rohen Tile-Bytes.
- `no-transform` bleibt Pflicht, solange vor-komprimiert wird - sonst packt der
  CDN die bereits gzip-komprimierten Kacheln ein zweites Mal ein.
- Die Cache-Override-Regel greift nur, wenn das Objekt noch nicht im Cache
  liegt - nach dem Anlegen einmal "Purge Cache" klicken.

Pruefen nach dem ersten Deploy:

```
curl -sI https://<pullzone-host>/runs/<run>/tiles/<ts>/7/20/51.bin
   -> content-encoding: gzip
```
