import os
import gzip
import math
import time
import numpy as np
from omfiles import OmFileReader
from scipy.ndimage import map_coordinates
from pmtiles.writer import Writer
from pmtiles.tile import TileType, Compression, zxy_to_tileid


# --- 1. GEO-HILFSFUNKTIONEN ---

def tile_bounds_wgs84(z, x, y):
    """Berechnet die exakte Bounding-Box (lon_min, lat_min, lon_max, lat_max)
    einer Web-Mercator Kachel in WGS84 Grad.
    """
    n = 2.0 ** z
    lon_min = x / n * 360.0 - 180.0
    lon_max = (x + 1) / n * 360.0 - 180.0

    lat_rad_max = math.atan(math.sinh(math.pi * (1.0 - 2.0 * y / n)))
    lat_rad_min = math.atan(math.sinh(math.pi * (1.0 - 2.0 * (y + 1) / n)))

    return lon_min, math.degrees(lat_rad_min), lon_max, math.degrees(lat_rad_max)


# --- 2. HRRR-PROJEKTION (Lambert Conformal Conic) ---
#
# Das HRRR-Dataset von Open-Meteo ("ncep_hrrr_conus") liegt NICHT auf einem
# regulären Lat/Lon-Gitter, sondern auf dem nativen Lambert-Conformal-Gitter
# (identisch zur Open-Meteo-Griddefinition):
#
#   ProjectionGrid(nx=1799, ny=1059,
#                  latitude=21.138...47.8424, longitude=-122.72...-60.918,
#                  projection=LambertConformalConicProjection(
#                      λ0=-97.5, ϕ0=0, ϕ1=38.5, ϕ2=38.5, radius=6371229))
#
# Die folgenden Funktionen reproduzieren diese Projektion exakt.

HRRR_NX = 1799
HRRR_NY = 1059
HRRR_LAT_ORIGIN = 21.138      # SW-Ecke (Gitterpunkt 0,0)
HRRR_LON_ORIGIN = -122.72
HRRR_LAT_NE = 47.8424         # NE-Ecke (dient nur der dx/dy-Berechnung)
HRRR_LON_NE = -60.918

_LCC_PHI1 = 38.5              # 1. Standardparallel (Latin1)
_LCC_PHI2 = 38.5              # 2. Standardparallel (Latin2)
_LCC_LAM0 = -97.5             # Zentralmeridian (LoV)
_LCC_PHI0 = 0.0               # Referenzbreite (LaD)
_LCC_RADIUS = 6371229.0


def _lcc_constants():
    """Berechnet die LCC-Konstanten n, F und ρ0 (Kugel)."""
    phi1 = math.radians(_LCC_PHI1)
    phi2 = math.radians(_LCC_PHI2)
    phi0 = math.radians(_LCC_PHI0)

    if abs(phi1 - phi2) < 1e-12:
        n = math.sin(phi1)
    else:
        n = math.log(math.cos(phi1) / math.cos(phi2)) / math.log(
            math.tan(math.pi / 4 + phi2 / 2) / math.tan(math.pi / 4 + phi1 / 2)
        )

    f = (math.cos(phi1) * math.tan(math.pi / 4 + phi1 / 2) ** n) / n
    rho0 = f / math.tan(math.pi / 4 + phi0 / 2) ** n
    return n, f, rho0


_LCC_N, _LCC_F, _LCC_RHO0 = _lcc_constants()
_LCC_LAM0_RAD = math.radians(_LCC_LAM0)


def _lcc_forward(lat, lon):
    """Lambert-Conformal-Vorwärtsprojektion (Kugel) nach Metern."""
    lat = np.asarray(lat, dtype=np.float64)
    lon = np.asarray(lon, dtype=np.float64)

    theta = _LCC_N * (np.radians(lon) - _LCC_LAM0_RAD)
    p = _LCC_F / np.tan(np.pi / 4 + np.radians(lat) / 2) ** _LCC_N

    x = _LCC_RADIUS * p * np.sin(theta)
    y = _LCC_RADIUS * (_LCC_RHO0 - p * np.cos(theta))
    return x, y


def _lcc_inverse(x, y):
    """Umkehrung der Lambert-Conformal-Projektion (Kugel)."""
    x = np.asarray(x, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)

    x_sk = x / _LCC_RADIUS
    y_sk = y / _LCC_RADIUS

    theta = np.arctan2(x_sk, _LCC_RHO0 - y_sk)
    rho = np.sqrt(x_sk ** 2 + (_LCC_RHO0 - y_sk) ** 2)

    phi = 2 * np.arctan((_LCC_F / rho) ** (1.0 / _LCC_N)) - np.pi / 2
    lam = _LCC_LAM0_RAD + theta / _LCC_N

    lat = np.degrees(phi)
    lon = (np.degrees(lam) + 180.0) % 360.0 - 180.0
    return lat, lon


# Ursprung (SW-Ecke) und effektive Rasterweite des nativen HRRR-Gitters
_x_origin, _y_origin = _lcc_forward(HRRR_LAT_ORIGIN, HRRR_LON_ORIGIN)
_x_ne, _y_ne = _lcc_forward(HRRR_LAT_NE, HRRR_LON_NE)
HRRR_ORIGIN_X = float(_x_origin)
HRRR_ORIGIN_Y = float(_y_origin)
HRRR_DX = float((_x_ne - _x_origin) / (HRRR_NX - 1))
HRRR_DY = float((_y_ne - _y_origin) / (HRRR_NY - 1))


# --- 3. PMTiles-PAYLOAD (int16-Delta + Ganz-Tile-gzip) ---
#
# Pro Kachel werden U und V als je ein zeilenweise delta-kodierter int16-Stream
# abgelegt. Danach wird das KOMPLETTE Tile (Geo-Header + Laengen + beide Streams)
# als EIN gzip-Strom komprimiert; der PMTiles-Header deklariert deshalb
# tile_compression = GZIP (= gzip ueber das ganze Tile, wie in der Spec und im
# Verhalten konformer Reader, z.B. pmtiles JS: getZxy -> decompress(ganzes Tile)).
#
# Kachel-Aufbau VOR der gzip-Komprimierung:
#     [Geo-Header 24B][u_len 4B][v_len 4B][u_int16_delta][v_int16_delta]


# --- 4. KACHEL-PYRAMIDE ---
#
# Das reguläre Lat/Lon-Zwischengitter wird pyramidal abgelegt:
#
#     Level 0..max_zoom,   stride = finest_stride * 2^(max_zoom - z)
#
# PMTILES_MAX_ZOOM = 7 passt zu 512-px-Kacheln im Frontend: deck.gl liest dann pro
# Kamerazoomstufe eine Stufe gröber (z = round(cameraZoom) - 1), Level 8 wird also
# nicht mehr gebraucht. Das reduziert die Kachel-/Request-Anzahl (4x weniger Kacheln
# und damit 4x weniger Encode-Aufrufe), ändert aber die Bytes NICHT.
#
# Die Bytes hängen allein an PMTILES_FINEST_STRIDE (Abtastschritt der feinsten Stufe)
# und skalieren mit dessen Quadrat:
#
#     finest_stride = 1  ->  feinste Auflösung 0,025°  ->  Gesamtdaten ~4N/3
#     finest_stride = 2  ->  feinste Auflösung 0,050°  ->  Gesamtdaten ~ N/3 (4x kleiner)
#
# Das native HRRR-Raster ist 3 km = ~0,027°: 0,025° überabtastet es also noch,
# 0,05° unterabtastet es um ~1,85x. finest_stride = 2 ist daher ECHTER Detailverlust.
PMTILES_MAX_ZOOM = 8
PMTILES_FINEST_STRIDE = 1

# Jede Kachel bekommt PMTILES_TILE_PADDING Zellen Halo auf allen 4 Seiten.
# Der Client interpoliert linear; ohne Halo fehlt ihm an den Kachelrändern der
# äußere Stützpunkt und die Interpolation wird dort ungenau (identisch zur
# D2-Pipeline). 1 = eine Zelle Überlappung. Der Geo-Header wächst mit
# (origin = gepolsterte Nord-/West-Ecke, rows/cols aus dem Ausschnitt) – der
# Client liest die Lage ohnehin aus dem Header, es muss nichts weiter angepasst
# werden.
PMTILES_TILE_PADDING = 1


# --- 5. PAYLOAD-VERFAHREN (Delta-Praediktor + Ganz-Tile-gzip) ---
#
# Der Praediktor liefert dem Kompressor raeumliche Praediktion (wie sie ein
# Bild-Codec intern selbst anwendet). gzip allein scheitert an int16-Winddaten,
# weil das Low-Byte bei 0,01-m/s-Quantisierung im Kreis laeuft und wie Rauschen
# aussieht (~11 Bit/Wert). Nach der Differenz sind die High-Bytes fast immer
# 0x00/0xFF und die Low-Bytes um 0 gepikert (~7 Bit/Wert).

# Gueltiger Wertebereich des Praediktors wird auf +-100 m/s geklemmt (physikalisch
# nie erreicht). Das garantiert, dass jedes Delta sicher in int16 passt:
#     |Sentinel| + max|q| = 20000 + 10000 = 30000 < 32767
PRED_SCALE = 0.05              # m/s pro int16-Schritt
PRED_VALID_MAX = 10000         # entspricht +-100 m/s
PRED_SENTINEL = -20000         # int16-Sentinel fuer NaN (~ -200 m/s)
PRED_GZIP_LEVEL = int(os.environ.get("PRED_GZIP_LEVEL", 1))   # 1 (schnell) .. 9


class HrrrWindProcessor:
    #: Reihenfolge und Beschriftung der Zeitmessungen pro Timestep
    _STEP_LABELS = (
        ("read", "Read .om"),
        ("resample", "Resample LCC->Gitter"),
        # --- folgende zwei Schritte gehoeren nur zum Feld-WebP und sind auskommentiert ---
        # ("field", "Feldbild rendern"),
        # ("webp_save", "Feld-WebP speichern"),
        ("pmtiles", "PMTiles bauen"),
        ("cleanup", "Temp loeschen"),
    )

    def __init__(self, output_folder="./wind_tiles_hrrr", width=2000, src_resolution=0.025):
        init_start_time = time.perf_counter()
        self.output_folder = output_folder
        os.makedirs(self.output_folder, exist_ok=True)

        # Messwerte des letzten Timesteps (werden von process_om_file bzw.
        # _create_wind_direction_pmtiles gefüllt)
        self.last_step_timings = {}
        self.last_pmtiles_stats = {}
        # Diagnose des Resamples: hatte die Quelldatei NaN?
        # (siehe _resample_to_regular_grid)
        self.source_had_nan = False
        self._nan_warned = False

        self.width = width
        # Auflösung des regulären Lat/Lon-Zwischengitters (Open-Meteo/PMTiles-kompatibel)
        self.src_resolution = src_resolution

        # Geografische Hülle des nativen HRRR-CONUS-Rasters (aus der Projektion bestimmt)
        self.lat_min, self.lat_max, self.lon_min, self.lon_max = self._hrrr_geographic_bounds()

        # Reguläres Lat/Lon-Zielgitter (Süd->Nord bzw. West->Ost)
        self.src_lat_shape = int(round((self.lat_max - self.lat_min) / self.src_resolution)) + 1
        self.src_lon_shape = int(round((self.lon_max - self.lon_min) / self.src_resolution)) + 1
        self.src_lats = np.linspace(self.lat_min, self.lat_max, self.src_lat_shape)
        self.src_lons = np.linspace(self.lon_min, self.lon_max, self.src_lon_shape)

        # Mapping reguläres Gitter -> natives HRRR-Gitter (projizierte Row/Col-Indizes)
        self._build_resample_coords()

        # --- NUR FUER DAS FELD-WEBP (auskommentiert) ---------------------------------
        # Das PMTiles-Artefakt braucht keines davon: es arbeitet direkt auf dem
        # regulären Lat/Lon-Zwischengitter. Gebraucht wurde es ausschliesslich fuer
        # das farbcodierte Windgeschwindigkeits-Overlay ("...Z.webp"), das ein
        # Mercator-Zielgitter mit fester Pixelbreite plus eine 13-Farben-Palette
        # voraussetzt.
        #
        # Mercator-Y Berechnungen
        # y_min_merc = np.degrees(np.log(np.tan(np.pi / 4.0 + np.radians(self.lat_min) / 2.0)))
        # y_max_merc = np.degrees(np.log(np.tan(np.pi / 4.0 + np.radians(self.lat_max) / 2.0)))
        #
        # # Höhe proportional zur Mercator-Verzerrung
        # self.height = int(self.width * (y_max_merc - y_min_merc) / (self.lon_max - self.lon_min))
        #
        # # 1. Ziel-Grid in Web-Mercator definieren (Pixel 0 oben = Nord = y_max_merc)
        # grid_x_linear = np.linspace(self.lon_min, self.lon_max, self.width)
        # grid_y_merc = np.linspace(y_max_merc, y_min_merc, self.height)
        #
        # grid_x, grid_y = np.meshgrid(grid_x_linear, grid_y_merc)
        #
        # # 2. Rücktransformation der Mercator-Y-Pixel in echte WGS84-Latitudes
        # lat_source = np.degrees(2 * np.arctan(np.exp(np.radians(grid_y))) - np.pi / 2.0)
        # lon_source = grid_x
        #
        # # 3. Indizes für das reguläre Lat/Lon-Quellgitter
        # row_indices = (lat_source - self.lat_min) / (self.lat_max - self.lat_min) * (self.src_lat_shape - 1)
        # col_indices = (lon_source - self.lon_min) / (self.lon_max - self.lon_min) * (self.src_lon_shape - 1)
        #
        # # Fertige Lookup-Matrix (Ziel-Pixel -> Zeile/Spalte im Zwischengitter)
        # self.interp_coords = np.array([row_indices, col_indices], dtype=np.float32)
        #
        # # Farbschema
        # self.color_palette = np.array([
        #     [0, 0, 0, 0],         # 0: Out of bounds / NaN
        #     [230, 255, 255, 255], # 1: < 3 Knots
        #     [0, 191, 255, 255],   # 2: < 5
        #     [0, 255, 204, 255],   # 3: < 6
        #     [0, 204, 0, 255],     # 4: < 7
        #     [153, 255, 0, 255],   # 5: < 8
        #     [255, 255, 0, 255],   # 6: < 9
        #     [209, 158, 0, 255],   # 7: < 10
        #     [255, 85, 0, 255],    # 8: < 12
        #     [255, 0, 0, 255],     # 9: < 15
        #     [255, 51, 153, 255],  # 10: < 20
        #     [153, 0, 204, 255],   # 11: < 25
        #     [0, 0, 255, 255]      # 12: >= 25
        # ], dtype=np.uint8)
        # --- ENDE NUR FUER DAS FELD-WEBP --------------------------------------------

        init_duration = time.perf_counter() - init_start_time
        print(
            f"✅ [HrrrProcessor] Gitter initialisiert: "
            f"Quelle {self.src_lon_shape}x{self.src_lat_shape} @ {self.src_resolution}° "
            f"(Init: {init_duration:.4f}s)"
        )
        print(
            f"   🌍 HRRR-Gebiet / Frontend-Anker (SW): lat {self.lat_min:.5f}..{self.lat_max:.5f}, "
            f"lon {self.lon_min:.5f}..{self.lon_max:.5f}"
        )
        print(
            f"   📦 Payload: int16-Delta (zeilenweise) + Ganz-Tile-gzip "
            f"L{PRED_GZIP_LEVEL}"
        )

    @staticmethod
    def _hrrr_geographic_bounds(n=256):
        """Ermittelt die geografische Hülle des nativen HRRR-LCC-Rasters.

        Dafür wird der Gitterrand abgetastet und rückprojiziert. Liefert
        (lat_min, lat_max, lon_min, lon_max).
        """
        xs = np.linspace(0.0, HRRR_NX - 1, n)
        ys = np.linspace(0.0, HRRR_NY - 1, n)

        grid_i = np.concatenate([
            xs, xs,
            np.zeros_like(ys), np.full_like(ys, HRRR_NX - 1),
        ])
        grid_j = np.concatenate([
            np.full_like(xs, HRRR_NY - 1), np.zeros_like(xs),
            ys, ys,
        ])

        x = HRRR_ORIGIN_X + grid_i * HRRR_DX
        y = HRRR_ORIGIN_Y + grid_j * HRRR_DY
        lat, lon = _lcc_inverse(x, y)

        return float(lat.min()), float(lat.max()), float(lon.min()), float(lon.max())

    def _build_resample_coords(self):
        """Berechnet für jeden Punkt des regulären Lat/Lon-Zwischengitters die
        Zeilen-/Spaltenindizes im nativen HRRR-Gitter (inkl. In-Bounds-Maske)."""
        lon_grid, lat_grid = np.meshgrid(self.src_lons, self.src_lats)

        x, y = _lcc_forward(lat_grid, lon_grid)
        row = (y - HRRR_ORIGIN_Y) / HRRR_DY
        col = (x - HRRR_ORIGIN_X) / HRRR_DX

        self.resample_coords = np.array([row, col], dtype=np.float32)
        self.resample_in_bounds = (
            (row >= 0) & (row <= HRRR_NY - 1)
            & (col >= 0) & (col <= HRRR_NX - 1)
        )

    def _resample_to_regular_grid(self, u_raw, v_raw):
        """Resampelt die projizierten HRRR-Felder (ny, nx) auf das reguläre
        Lat/Lon-Zwischengitter. Punkte außerhalb des HRRR-Rasters werden NaN.

        Rückgabe: (u_reg, v_reg, valid) – valid ist True innerhalb des Rasters.

        Zur Gültigkeit: das native HRRR-Array ist deckungsgleich mit der Modell-
        domain, Open-Meteo legt es ohne Randmaske ab. Es sollte also gar keine NaN
        geben, und dann ist die Gültigkeit rein geometrisch (resample_in_bounds).
        Weil das eine Annahme über Fremddaten ist, wird sie pro Timestep geprüft
        (~2 ms) statt vorausgesetzt: nur bei tatsächlich vorhandenem NaN wird die
        datengetriebene Maske gebaut. Die beiden Pfade sind ergebnisgleich, weil
        nan_to_num ohne NaN eine identische Kopie liefert und
        (alle Knoten gültig) AND resample_in_bounds == resample_in_bounds.
        """
        u_raw = np.asarray(u_raw)
        v_raw = np.asarray(v_raw)
        if u_raw.shape == (HRRR_NX, HRRR_NY) and u_raw.shape != (HRRR_NY, HRRR_NX):
            # Defensive: falls das .om transponiert vorliegt (Spalten zuerst)
            u_raw = u_raw.T
            v_raw = v_raw.T

        self.source_had_nan = bool(np.isnan(u_raw).any()) or bool(np.isnan(v_raw).any())

        if self.source_had_nan:
            # --- Langsamer Pfad: NaN in den Quelldaten -> Maske mitsampeln ---
            if not self._nan_warned:
                print(
                    "   ⚠️ NaN in den HRRR-Quelldaten -> "
                    "datengetriebene Resample-Maske aktiv."
                )
                self._nan_warned = True

            u_clean = np.nan_to_num(u_raw, nan=0.0)
            v_clean = np.nan_to_num(v_raw, nan=0.0)
            u_reg = map_coordinates(u_clean, self.resample_coords, order=1, mode="nearest")
            v_reg = map_coordinates(v_clean, self.resample_coords, order=1, mode="nearest")

            src_valid = (~np.isnan(u_raw)).astype(np.float32)
            valid = map_coordinates(
                src_valid, self.resample_coords, order=0, mode="nearest"
            ) > 0.5
            valid &= self.resample_in_bounds
        else:
            # --- Schneller Pfad: keine NaN -> Gültigkeit == Geometrie ---
            # Spar gegenueber dem langsamen Pfad: nan_to_num (2x 7,6 MB), die
            # float64-Maske (~15 MB) und eine komplette map_coordinates-Runde
            # ueber die 3,7 M Zielpunkte.
            u_reg = map_coordinates(u_raw, self.resample_coords, order=1, mode="nearest")
            v_reg = map_coordinates(v_raw, self.resample_coords, order=1, mode="nearest")
            valid = self.resample_in_bounds

        u_reg[~valid] = np.nan
        v_reg[~valid] = np.nan
        # copy(): im schnellen Pfad ist valid die gecachte statische Maske und darf
        # vom Aufrufer nicht in-place veraendert werden.
        return u_reg, v_reg, valid.copy()

    @staticmethod
    def _encode_field_pred(field, stats=None):
        """Kodiert ein 2D-Feld (float32, NaN = ungültig) als int16-Delta-Stream.

        Aufbau: int16-Werte, pro ZEILE horizontal differenziert. Die erste Spalte
        jeder Zeile bleibt absolut (Anker für die Kumulation im Client).
        NaN wird zu PRED_SENTINEL; weil der gültige Bereich auf +-PRED_VALID_MAX
        geklemmt ist, passt jedes Delta sicher in int16 (siehe Konstanten oben).

        Rückgabe sind die ROHEN int16-Bytes. Die gzip-Komprimierung passiert
        anschliessend über das KOMPLETTE Tile in ``_create_wind_direction_pmtiles``,
        passend zu ``tile_compression = GZIP`` im PMTiles-Header.

        Wird ``stats`` (dict) übergeben, wird die Teilzeit aufaddiert:
        "prep" = Quantisierung + Differenz.
        """
        _t0 = time.perf_counter()

        q = np.full(field.shape, PRED_SENTINEL, dtype=np.int32)
        valid = np.isfinite(field)
        if np.any(valid):
            q[valid] = np.clip(
                np.round(field[valid] / PRED_SCALE), -PRED_VALID_MAX, PRED_VALID_MAX
            ).astype(np.int32)

        # Horizontale Differenz; die erste Spalte jeder Zeile bleibt absolut.
        d = np.empty_like(q)
        d[:, 0] = q[:, 0]
        d[:, 1:] = q[:, 1:] - q[:, :-1]

        raw = d.astype("<i2").tobytes()

        if stats is not None:
            stats["prep"] = stats.get("prep", 0.0) + (time.perf_counter() - _t0)
        return raw

    def _create_wind_direction_pmtiles(
        self,
        u_raw,
        v_raw,
        output_pmtiles_path,
        min_zoom=0,
        max_zoom=PMTILES_MAX_ZOOM,
        finest_stride=PMTILES_FINEST_STRIDE,
    ):
        """Erzeugt PMTiles, deren Kacheln je einen U- und V-Stream enthalten.

        Kachel-Aufbau VOR der gzip-Komprimierung:
            [Geo-Header 24B][u_len 4B][v_len 4B][u_int16_delta][v_int16_delta]

        Das komplette Tile wird als EIN gzip-Strom abgelegt; der PMTiles-Header
        deklariert daher ``tile_compression = GZIP`` (gilt fuer das ganze Tile).

        Als Gitter dient das reguläre Lat/Lon-Zwischengitter. Es wird bis ``max_zoom``
        pyramidal abgelegt. ``finest_stride`` ist der Abtastschritt auf der feinsten
        Stufe: 1 = volle Auflösung (0,025°, kein Detailverlust), 2 = halbe Auflösung
        (0,05°, 4x kleiner, dafür sichtbar unschärfer).

        Jede Kachel bekommt ``PMTILES_TILE_PADDING`` Zellen Halo auf allen 4 Seiten
        (siehe Konstante), damit der Client an den Kachelrändern einen äußeren
        Stützpunkt für die lineare Interpolation hat.
        """

        # 1D Koordinatenvektoren des regulären Zwischengitters
        lats = self.src_lats
        lons = self.src_lons

        tiles_dict = {}
        total_payload_bytes = 0

        # Zeitmessung und Zähler
        t_tiles_start = time.perf_counter()
        t_encode = 0.0
        enc_stats = {}
        tiles_considered = 0
        tiles_skipped = 0

        # Schleife über Web-Mercator Kacheln je Zoomstufe
        for z in range(min_zoom, max_zoom + 1):
            stride = finest_stride * 2 ** (max_zoom - z)

            # Subsampling je nach Zoom-Level
            u_lod = u_raw[::stride, ::stride]
            v_lod = v_raw[::stride, ::stride]
            lats_lod = lats[::stride]
            lons_lod = lons[::stride]

            n = 2 ** z
            for x in range(n):
                for y in range(n):
                    t_lon_min, t_lat_min, t_lon_max, t_lat_max = tile_bounds_wgs84(z, x, y)

                    if (t_lon_max < self.lon_min or t_lon_min > self.lon_max or
                        t_lat_max < self.lat_min or t_lat_min > self.lat_max):
                        continue

                    tiles_considered += 1

                    col_indices = np.where((lons_lod >= t_lon_min) & (lons_lod <= t_lon_max))[0]
                    row_indices = np.where((lats_lod >= t_lat_min) & (lats_lod <= t_lat_max))[0]

                    if len(col_indices) == 0 or len(row_indices) == 0:
                        tiles_skipped += 1
                        continue

                    # Teilbereich ausschneiden, mit Halo auf allen 4 Seiten.
                    # Das Padding gibt dem Client an den Kachelrändern genug
                    # Stützpunkte für die lineare Interpolation.
                    c_start = max(0, col_indices[0] - PMTILES_TILE_PADDING)
                    c_end = min(
                        len(lons_lod),
                        col_indices[-1] + 1 + PMTILES_TILE_PADDING,
                    )

                    # lats_lod ist Süd->Nord sortiert: row_indices[0] = Südrand,
                    # row_indices[-1] = Nordrand. (Lat-Indizes für North-Up)
                    r_end_idx = max(0, row_indices[0] - PMTILES_TILE_PADDING)
                    r_start_idx = min(
                        len(lats_lod) - 1,
                        row_indices[-1] + PMTILES_TILE_PADDING,
                    )

                    sub_u = u_lod[r_end_idx:r_start_idx + 1, c_start:c_end]
                    sub_v = v_lod[r_end_idx:r_start_idx + 1, c_start:c_end]

                    # Zeilen umkehren für North-Up (nördlichste Breite = Zeile 0)
                    sub_u = np.flipud(sub_u)
                    sub_v = np.flipud(sub_v)

                    rows, cols = sub_u.shape

                    if rows == 0 or cols == 0:
                        tiles_skipped += 1
                        continue

                    # Bounding-Check auf valide Vektoren
                    valid_mask = ~np.isnan(sub_u) & ~np.isnan(sub_v)
                    if not np.any(valid_mask):
                        tiles_skipped += 1
                        continue

                    # 1. GEO-HEADER (6x Float32 = 24 Bytes)
                    origin_lng = float(lons_lod[c_start])
                    origin_lat = float(lats_lod[r_start_idx]) # Nördlichste Breite

                    delta_lng = float(lons_lod[1] - lons_lod[0]) if len(lons_lod) > 1 else self.src_resolution * stride
                    delta_lat = float(lats_lod[1] - lats_lod[0]) if len(lats_lod) > 1 else self.src_resolution * stride

                    header_meta = np.array([
                        origin_lng,
                        origin_lat,
                        delta_lng,
                        delta_lat,
                        float(rows),
                        float(cols)
                    ], dtype=np.float32)

                    # 2. PAYLOAD: U und V als rohe int16-Delta-Streams, danach das
                    #    KOMPLETTE Tile in EINEN gzip-Strom packen.
                    _t_enc = time.perf_counter()

                    u_stream = self._encode_field_pred(sub_u, enc_stats)
                    v_stream = self._encode_field_pred(sub_v, enc_stats)

                    # [Geo-Header 24B][u_len 4B][v_len 4B][u_int16][v_int16]
                    length_meta = np.array(
                        [len(u_stream), len(v_stream)], dtype=np.uint32
                    )

                    raw_tile = (
                        header_meta.tobytes()
                        + length_meta.tobytes()
                        + u_stream
                        + v_stream
                    )

                    # Ganz-Tile-gzip -> passt zu tile_compression = GZIP im Header.
                    # mtime=0 -> reproduzierbare Bytes (kein Zeitstempel im Header)
                    _t_gz = time.perf_counter()
                    tile_bytes = gzip.compress(
                        raw_tile, compresslevel=PRED_GZIP_LEVEL, mtime=0
                    )
                    enc_stats["codec"] = enc_stats.get("codec", 0.0) + (
                        time.perf_counter() - _t_gz
                    )

                    t_encode += time.perf_counter() - _t_enc

                    tiles_dict[zxy_to_tileid(z, x, y)] = tile_bytes
                    total_payload_bytes += len(tile_bytes)

        t_tiles = time.perf_counter() - t_tiles_start

        # PMTiles schreiben
        t_write_start = time.perf_counter()
        with open(output_pmtiles_path, "wb") as f:
            writer = Writer(f)

            for tile_id in sorted(tiles_dict.keys()):
                writer.write_tile(tile_id, tiles_dict[tile_id])

            header = {
                "tile_type": TileType.UNKNOWN,
                # Das ganze Tile ist gzip-komprimiert (siehe tile_bytes-Erzeugung)
                "tile_compression": Compression.GZIP,
                "min_zoom": min_zoom,
                "max_zoom": max_zoom,
                "min_lon": self.lon_min,
                "min_lat": self.lat_min,
                "max_lon": self.lon_max,
                "max_lat": self.lat_max,
                "center_zoom": 5,
                "center_lon": (self.lon_min + self.lon_max) / 2.0,
                "center_lat": (self.lat_min + self.lat_max) / 2.0
            }

            metadata = {
                "name": "HRRR Wind Vector PMTiles (int16-Delta + Ganz-Tile-gzip)",
                "format": "binary",
                "description": (
                    "Komplettes Tile gzip-komprimiert (tile_compression = GZIP). "
                    "Inhalt unkomprimiert: 24 Byte Geo-Header (6x float32) + 2x "
                    "uint32 Laenge + je ein int16-Delta-Stream (zeilenweise, erste "
                    "Spalte je Zeile absolut) fuer U und V. "
                    f"gzip L{PRED_GZIP_LEVEL}. Skala {PRED_SCALE} m/s, Sentinel "
                    f"{PRED_SENTINEL}, gueltig bis +-{PRED_VALID_MAX * PRED_SCALE:.0f} m/s."
                ),
            }

            writer.finalize(header, metadata)

        t_write = time.perf_counter() - t_write_start

        file_size_kb = os.path.getsize(output_pmtiles_path) / 1024.0
        print(f"✅ PMTiles Container erfolgreich erstellt: {output_pmtiles_path}")
        print(f"💾 Gesamtgröße: {file_size_kb:.2f} KB")
        print(
            f"   📦 Tile-Payload (gzip, ganzes Tile): "
            f"{total_payload_bytes / 1048576.0:.2f} MiB "
            f"in {len(tiles_dict)} Kacheln (L{PRED_GZIP_LEVEL})"
        )
        per_tile_ms = (t_encode / len(tiles_dict) * 1000.0) if tiles_dict else 0.0
        print(
            f"   ⏱️  Tiles: {t_tiles:.3f}s = Fenster/Slice "
            f"{t_tiles - t_encode:.3f}s + Encode {t_encode:.3f}s | "
            f"Schreiben {t_write:.3f}s"
        )
        print(
            f"      Encode-Aufteilung: Quantisieren/Differenz "
            f"{enc_stats.get('prep', 0.0):.3f}s | gzip "
            f"{enc_stats.get('codec', 0.0):.3f}s"
        )
        print(
            f"      Kacheln: {len(tiles_dict)} geschrieben | {tiles_skipped} leer "
            f"übersprungen | {tiles_considered} im Gebiet | Ø {per_tile_ms:.2f} ms/Kachel"
        )

        self.last_pmtiles_stats = {
            "tiles": t_tiles,
            "window": t_tiles - t_encode,
            "encode": t_encode,
            "write": t_write,
            "encode_prep": enc_stats.get("prep", 0.0),
            "encode_codec": enc_stats.get("codec", 0.0),
            "tiles_written": len(tiles_dict),
            "tiles_skipped": tiles_skipped,
            "tiles_considered": tiles_considered,
            "payload_bytes": total_payload_bytes,
            "file_size_kb": file_size_kb,
        }
        return True

    def _print_step_timings(self, step_duration):
        """Gibt die gemessenen Zeitanteile eines Timesteps als kleine Tabelle aus."""
        rows = [
            (label, self.last_step_timings.get(key, 0.0))
            for key, label in self._STEP_LABELS
        ]
        rest = step_duration - sum(value for _, value in rows)
        rows.append(("Sonstiges", max(rest, 0.0)))

        name_width = max(len(name) for name, _ in rows)
        print(f"       Zeitanteile ({step_duration:.3f}s):")
        for name, value in rows:
            share = (value / step_duration * 100.0) if step_duration > 0 else 0.0
            bar = "█" * int(round(share / 5.0))
            print(f"         {name:<{name_width}} {value:7.3f}s {share:5.1f}% {bar}")

    def process_om_file(self, om_path, output_filename=None):
        step_start_time = time.perf_counter()
        self.last_step_timings = {}
        self.last_pmtiles_stats = {}
        _t_mark = step_start_time

        def _lap(name):
            """Bucht die Zeit seit dem letzten Aufruf auf ``name``."""
            nonlocal _t_mark
            _now = time.perf_counter()
            self.last_step_timings[name] = (
                self.last_step_timings.get(name, 0.0) + (_now - _t_mark)
            )
            _t_mark = _now

        if not os.path.exists(om_path):
            print(f"⚠️ Datei {om_path} nicht gefunden.")
            return False

        if output_filename is None:
            base_name = os.path.splitext(os.path.basename(om_path))[0]
            output_filename = base_name

        print(f"-> Verarbeite: {os.path.basename(om_path)} -> {output_filename}...")

        # --- A. .om einlesen ---
        with OmFileReader(om_path) as root:
            u_node = root.get_child_by_name("wind_u_component_10m")
            v_node = root.get_child_by_name("wind_v_component_10m")

            u_raw = u_node.read_array(...)
            v_raw = v_node.read_array(...)
        _lap("read")

        # --- B. HRRR liegt in Lambert-Conformal-Projektion vor -> auf das reguläre
        # Lat/Lon-Zwischengitter resampeln (Open-Meteo/PMTiles-kompatibles Format)
        u_grid, v_grid, valid_mask = self._resample_to_regular_grid(u_raw, v_raw)
        _lap("resample")

        # --- NUR FUER DAS FELD-WEBP (auskommentiert) ---------------------------------
        # Farbcodiertes Windgeschwindigkeits-Overlay: Windgeschwindigkeit berechnen,
        # auf das Mercator-Zielgitter ziehen, in 13 Farbstufen quantisieren und als
        # verlustfreies WebP speichern. Fuer die PMTiles nicht noetig.
        #
        # # --- C. Feldbild (Windgeschwindigkeit in Knoten, farbcodiert) rendern ---
        # u_clean = np.nan_to_num(u_grid, nan=0.0)
        # v_clean = np.nan_to_num(v_grid, nan=0.0)
        #
        # wind_speed_knots = np.sqrt(u_clean**2 + v_clean**2) * 1.94384
        #
        # grid_data = map_coordinates(wind_speed_knots, self.interp_coords, order=1, mode='nearest')
        # grid_valid = map_coordinates(valid_mask.astype(float), self.interp_coords, order=0, mode='nearest')
        #
        # conditions = [
        #     (grid_data < 3) & (grid_valid > 0.5),
        #     (grid_data < 5) & (grid_valid > 0.5),
        #     (grid_data < 6) & (grid_valid > 0.5),
        #     (grid_data < 7) & (grid_valid > 0.5),
        #     (grid_data < 8) & (grid_valid > 0.5),
        #     (grid_data < 9) & (grid_valid > 0.5),
        #     (grid_data < 10) & (grid_valid > 0.5),
        #     (grid_data < 12) & (grid_valid > 0.5),
        #     (grid_data < 15) & (grid_valid > 0.5),
        #     (grid_data < 20) & (grid_valid > 0.5),
        #     (grid_data < 25) & (grid_valid > 0.5),
        #     (grid_data >= 25) & (grid_valid > 0.5)
        # ]
        #
        # choices_indices = np.arange(1, len(conditions) + 1)
        # selected_color_indices = np.select(conditions, choices_indices, default=0)
        # img_array = self.color_palette[selected_color_indices]
        #
        # img = Image.fromarray(img_array)  # RGBA wird aus dem uint8-Array abgeleitet
        # _lap("field")
        #
        # # --- D. Feldbild (Windgeschwindigkeits-Overlay) speichern ---
        # output_webp_path = os.path.join(self.output_folder, output_filename)
        # img.save(output_webp_path, format="WEBP", lossless=True, method=4)
        # _lap("webp_save")
        # --- ENDE NUR FUER DAS FELD-WEBP --------------------------------------------

        # --- E. PMTiles (Vektorkacheln mit u/v für die Pfeil-Darstellung) ---
        # output_filename traegt nur den Zeitstempel ("...Z") als Namensbasis;
        # das Feld-WebP wird nicht mehr geschrieben, es entsteht nur die PMTiles.
        pmtiles_filename = f"{output_filename}_dir.pmtiles"
        output_pmtiles_path = os.path.join(self.output_folder, pmtiles_filename)
        success_pmtiles = self._create_wind_direction_pmtiles(u_grid, v_grid, output_pmtiles_path)
        _lap("pmtiles")

        if os.path.exists(om_path):
            os.remove(om_path)
        _lap("cleanup")

        step_duration = time.perf_counter() - step_start_time
        print(f"    ⏱️ Dauer: {step_duration:.3f}s")
        self._print_step_timings(step_duration)

        pmtiles_kb = os.path.getsize(output_pmtiles_path) / 1024.0 if os.path.exists(output_pmtiles_path) else 0.0
        print(f"    📁 PMTiles {pmtiles_kb:.1f} KB")
        return success_pmtiles
