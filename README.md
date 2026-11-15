# hrrr-data

Pipeline, die 10-m-Wind (Open-Meteo `.om`-Dateien) in vor-komprimierte
Web-Mercator-Kacheln umrechnet und nach Bunny Storage hochlaedt.

Das Modell ist per `MODEL` waehlbar. HRRR wird **Ende November 2026** von
Open-Meteo abgeschaltet, Nachfolger ist RRFS:

| `MODEL` | Zeitraum | Horizont |
|---|---|---|
| `ncep_hrrr_conus_15min` (Default) | bis Ende Nov 2026 | +18 h |
| `ncep_rrfs_conus_15min` | ab Ende Nov 2026 | +18 h (erste Datei +0:15, **kein** +0:00) |

Welches Windfeld in der `.om` steckt (u/v oder Betrag/Richtung), erkennt
`process.py` selbst - es gibt keinen weiteren Modellschalter.

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
wird `jetzt - 2h` auf die volle Stunde verwendet. Weitere Env-Variablen:
`MODEL`, `OUTPUT_DIR`, `TEMP_OM_DIR`, `HORIZON_15MIN_HOURS` (Default 6),
`HORIZON_END_HOURS` (Default 18), `PRED_GZIP_LEVEL`.

RRFS-Beispiel:

```
MODEL=ncep_rrfs_conus_15min TARGET_RUN=2026-11-15T09:00 OUTPUT_DIR=./t \
    python pipeline/src/pipeline.py
```

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

## Zwei Workflows, zwei Zonen

HRRR und RRFS laufen **parallel** und schreiben in getrennte Zonen - sonst
kollidieren sie: die Run-IDs sind modellunabhaengig (`YYYYMMDD_HHMM`, beide
Modelle haben einen 08Z-Run), und `index.json` liegt im Root der Zone. Ein
RRFS-Test wuerde sonst den produktiven HRRR-Index umschalten.

| Workflow | Modell | Zone | Secret |
|---|---|---|---|
| `sync.yml` | `ncep_hrrr_conus_15min` | `gdsfgfsdgsfd` | `BUNNY_STORAGE_PASSWORD` |
| `sync-rrfs.yml` | `ncep_rrfs_conus_15min` | `rrfs` | `BUNNY_STORAGE_PASSWORD_RRFS` |

Beide haben eine eigene `concurrency.group`, damit ein manueller RRFS-Lauf
nicht gegen den HRRR-Job serialisiert. Die Edge Rules der Pull Zone
(`Content-Encoding: gzip`, Cache-Override, CORS) muessen fuer die RRFS-Zone
einmal separat angelegt werden (siehe unten).

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
