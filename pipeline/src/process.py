import os
import gzip
import math
import shutil
import time
import numpy as np
from omfiles import OmFileReader
from scipy.ndimage import map_coordinates


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
# Die folgenden Funktionen reproduzieren diese Projektion exakt. Die
# Konstanten stehen in einer Klasse, weil NBM CONUS dasselbe Verfahren mit
# anderen Parametern nutzt (siehe Abschnitt 2c).

HRRR_NX = 1799
HRRR_NY = 1059
HRRR_LAT_ORIGIN = 21.138      # SW-Ecke (Gitterpunkt 0,0)
HRRR_LON_ORIGIN = -122.72
HRRR_LAT_NE = 47.8424         # NE-Ecke (dient nur der dx/dy-Berechnung)
HRRR_LON_NE = -60.918

# Parameter der beiden LCC-Gitter, die diese Pipeline bedient:
#
#                 Radius      SP1/SP2   lon_0     lat_0
#   HRRR / RRFS   6371229 m   38.5      -97.5     0.0
#   NBM CONUS     6371200 m   25.0      -95.0     0.0
#
# lat_0 (Latitude of false origin) ist bei beiden 0.0 und wirkt damit nur als
# y-Offset; die Herkunft ist ohnehin die SW-Ecke des jeweiligen Gitters.
# Die Werte stammen aus dem crs_wkt der .om (verifiziert gegen
# api.open-meteo.com/data/<modell>/static/meta.json).


class _LambertConformal:
    """Lambert-Conformal-Projektion (2SP, Kugel) mit eigenen Konstanten."""

    def __init__(self, phi1_deg, phi2_deg, lam0_deg, phi0_deg, radius):
        phi1 = math.radians(phi1_deg)
        phi2 = math.radians(phi2_deg)
        phi0 = math.radians(phi0_deg)

        if abs(phi1 - phi2) < 1e-12:
            n = math.sin(phi1)
        else:
            n = math.log(math.cos(phi1) / math.cos(phi2)) / math.log(
                math.tan(math.pi / 4 + phi2 / 2) / math.tan(math.pi / 4 + phi1 / 2)
            )

        self.n = n
        self.f = (math.cos(phi1) * math.tan(math.pi / 4 + phi1 / 2) ** n) / n
        self.rho0 = self.f / math.tan(math.pi / 4 + phi0 / 2) ** n
        self.lam0 = math.radians(lam0_deg)
        self.radius = float(radius)

    def forward(self, lat, lon):
        """Geografisch (Grad) -> projizierte Meter."""
        lat = np.asarray(lat, dtype=np.float64)
        lon = np.asarray(lon, dtype=np.float64)

        theta = self.n * (np.radians(lon) - self.lam0)
        p = self.f / np.tan(np.pi / 4 + np.radians(lat) / 2) ** self.n

        x = self.radius * p * np.sin(theta)
        y = self.radius * (self.rho0 - p * np.cos(theta))
        return x, y

    def inverse(self, x, y):
        """Projizierte Meter -> geografisch (Grad)."""
        x = np.asarray(x, dtype=np.float64)
        y = np.asarray(y, dtype=np.float64)

        x_sk = x / self.radius
        y_sk = y / self.radius

        theta = np.arctan2(x_sk, self.rho0 - y_sk)
        rho = np.sqrt(x_sk ** 2 + (self.rho0 - y_sk) ** 2)

        phi = 2 * np.arctan((self.f / rho) ** (1.0 / self.n)) - np.pi / 2
        lam = self.lam0 + theta / self.n

        lat = np.degrees(phi)
        lon = (np.degrees(lam) + 180.0) % 360.0 - 180.0
        return lat, lon


HRRR_LCC = _LambertConformal(38.5, 38.5, -97.5, 0.0, 6371229.0)


def _lcc_forward(lat, lon):
    """HRRR/RRFS-Vorwärtsprojektion (Kompatibilitäts-Wrapper)."""
    return HRRR_LCC.forward(lat, lon)


def _lcc_inverse(x, y):
    """HRRR/RRFS-Umkehrung (Kompatibilitäts-Wrapper)."""
    return HRRR_LCC.inverse(x, y)


# Ursprung (SW-Ecke) und effektive Rasterweite des nativen HRRR-Gitters
_x_origin, _y_origin = _lcc_forward(HRRR_LAT_ORIGIN, HRRR_LON_ORIGIN)
_x_ne, _y_ne = _lcc_forward(HRRR_LAT_NE, HRRR_LON_NE)
HRRR_ORIGIN_X = float(_x_origin)
HRRR_ORIGIN_Y = float(_y_origin)
HRRR_DX = float((_x_ne - _x_origin) / (HRRR_NX - 1))
HRRR_DY = float((_y_ne - _y_origin) / (HRRR_NY - 1))


# --- 2a. KNMI-PROJEKTION (Rotated Lat/Lon) ---
#
# KNMI HARMONIE AROME Europe ("knmi_harmonie_arome_europe") liegt auf einem
# rotierten Lat/Lon-Gitter. Open-Meteo definiert es als
#
#   ProjectionGrid(nx=676, ny=564,
#                  latitude=39.740627...62.619324,
#                  longitude=-25.162262...38.75702,
#                  projection=RotatedLatLonProjection(latitude: -35, longitude: -8))
#
# Die .om-Datei bestaetigt das in ihrem crs_wkt (o_lat_p=35, lon_0=-8,
# Kugelradius 6371229, BBOX = SW-/NE-Ecke). Die Daten liegen zeilenweise
# Sued->Nord (Zeile 0 = Suedrand), Spalte 0 = Westrand; dx und dy sind negativ,
# weil die rotierte Laenge nach Osten abnimmt.
#
# Die BBOX im WKT ist NICHT die geografische Huelle: sie enthaelt nur die Ecken
# (0,0) und (nx-1, ny-1). Die tatsaechliche Huelle (Rand abgetastet) reicht bis
# Island/Nordatlantik (lat 37.74...69.55, lon -42.08...38.76).

KNMI_NX = 676
KNMI_NY = 564
KNMI_LAT_SW = 39.740627
KNMI_LON_SW = -25.162262
KNMI_LAT_NE = 62.619324
KNMI_LON_NE = 38.75702

_KNMI_POLE_LAT = -35.0   # RotatedLatLonProjection(latitude: -35)
_KNMI_POLE_LON = -8.0    # RotatedLatLonProjection(longitude: -8)

_ROT_THETA = math.radians(90.0 + _KNMI_POLE_LAT)
_ROT_PHI = math.radians(_KNMI_POLE_LON)


def _rot_forward(lat, lon):
    """Rotierte Lat/Lon-Vorwaertsprojektion (Kugel) -> (x, y) in Grad."""
    lat = np.asarray(lat, dtype=np.float64)
    lon = np.asarray(lon, dtype=np.float64)

    lam = np.radians(lon)
    phi = np.radians(lat)

    x = np.cos(lam) * np.cos(phi)
    y = np.sin(lam) * np.cos(phi)
    z = np.sin(phi)

    x2 = (
        math.cos(_ROT_THETA) * math.cos(_ROT_PHI) * x
        + math.cos(_ROT_THETA) * math.sin(_ROT_PHI) * y
        + math.sin(_ROT_THETA) * z
    )
    y2 = -math.sin(_ROT_PHI) * x + math.cos(_ROT_PHI) * y
    z2 = (
        -math.sin(_ROT_THETA) * math.cos(_ROT_PHI) * x
        - math.sin(_ROT_THETA) * math.sin(_ROT_PHI) * y
        + math.cos(_ROT_THETA) * z
    )

    return -np.degrees(np.arctan2(y2, x2)), -np.degrees(np.arcsin(z2))


def _rot_inverse(x, y):
    """Umkehrung der rotierten Lat/Lon-Projektion (Kugel)."""
    x = np.asarray(x, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)

    lon = np.radians(x)
    lat = np.radians(y)

    lat2 = -np.arcsin(
        math.cos(_ROT_THETA) * np.sin(lat)
        - np.cos(lon) * math.sin(_ROT_THETA) * np.cos(lat)
    )
    lon2 = -(
        np.arctan2(
            np.sin(lon),
            np.tan(lat) * math.sin(_ROT_THETA) + np.cos(lon) * math.cos(_ROT_THETA),
        )
        - _ROT_PHI
    )
    return np.degrees(lat2), (np.degrees(lon2) + 180.0) % 360.0 - 180.0


# Ursprung (SW-Ecke) und effektive Rasterweite des nativen KNMI-Gitters
_x_sw, _y_sw = _rot_forward(KNMI_LAT_SW, KNMI_LON_SW)
_x_ne, _y_ne = _rot_forward(KNMI_LAT_NE, KNMI_LON_NE)
KNMI_ORIGIN_X = float(_x_sw)
KNMI_ORIGIN_Y = float(_y_sw)
KNMI_DX = float((_x_ne - _x_sw) / (KNMI_NX - 1))   # -0.05
KNMI_DY = float((_y_ne - _y_sw) / (KNMI_NY - 1))   # -0.05


# --- 2c. NBM-PROJEKTION (Lambert Conformal Conic) ---
#
# NBM CONUS ("ncep_nbm_conus") nutzt dasselbe Projektionsverfahren wie HRRR,
# aber mit eigenen Parametern:
#
#   ProjectionGrid(nx=2345, ny=1597, dx=dy=2539.7 m,
#                  projection=LambertConformalConic(
#                      λ0=-95.0, ϕ0=0, ϕ1=25.0, ϕ2=25.0, radius=6371200))
#
# Die BBOX im crs_wkt nennt die Gitterecken (0,0) und (nx-1, ny-1):
#   19.228985, -126.27699 .. 54.372913, -59.042786
#
# Anders als beim KNMI ist das KEIN rotiertes Gitter: die geografische Huelle
# wird wie bei HRRR aus dem abgetasteten Gitterrand bestimmt.
#
# Rasterweite 2539,7 m = 0,0228 Grad meridional. Meridional ist das die
# Referenz fuer die Zielaufloesung; zonal ist LCC bei 25 Grad Standardparallel
# ebenfalls 0,0252 Grad und weiter noerdlich feiner (Stauchung).

NBM_NX = 2345
NBM_NY = 1597
NBM_LAT_SW = 19.228985
NBM_LON_SW = -126.27699
NBM_LAT_NE = 54.372913
NBM_LON_NE = -59.042786

NBM_LCC = _LambertConformal(25.0, 25.0, -95.0, 0.0, 6371200.0)

_nbm_x_sw, _nbm_y_sw = NBM_LCC.forward(NBM_LAT_SW, NBM_LON_SW)
_nbm_x_ne, _nbm_y_ne = NBM_LCC.forward(NBM_LAT_NE, NBM_LON_NE)
NBM_ORIGIN_X = float(_nbm_x_sw)
NBM_ORIGIN_Y = float(_nbm_y_sw)
NBM_DX = float((_nbm_x_ne - _nbm_x_sw) / (NBM_NX - 1))   # ~2539.7
NBM_DY = float((_nbm_y_ne - _nbm_y_sw) / (NBM_NY - 1))   # ~2539.7


# --- 2b. WIND-QUELLFORMAT ---
#
# Open-Meteo legt die 10-m-Winde je Modell unterschiedlich ab:
#
#   ncep_hrrr_conus_15min   wind_u_component_10m / wind_v_component_10m
#                           (m/s, grid-relativ)
#   ncep_rrfs_conus_15min   wind_speed_10m / wind_direction_10m
#   ncep_nbm_conus          (m/s, meteorologische Richtung "woher",
#                            bereits auf True North gedreht)
#
# Die Kacheln speichern u/v, und eine Richtung darf wegen des 0/360-Sprungs
# nicht interpoliert werden. Das Quellformat wird deshalb pro Datei am Inhalt
# erkannt - die Pipeline braucht keinen Modellschalter.

WIND_U_VARIABLE = "wind_u_component_10m"
WIND_V_VARIABLE = "wind_v_component_10m"
WIND_SPEED_VARIABLE = "wind_speed_10m"
WIND_DIRECTION_VARIABLE = "wind_direction_10m"


def _speed_direction_to_uv(speed, direction):
    """(Betrag [m/s], Richtung "woher" [Grad, True North]) -> (u, v) [m/s].

    u zeigt nach Osten, v nach Norden. Wind aus Nord (0/360 Grad) weht nach
    Sueden -> v negativ; Wind aus Ost (90 Grad) weht nach Westen -> u negativ.
    """
    spd = np.asarray(speed, dtype=np.float32)
    rad = np.deg2rad(np.asarray(direction, dtype=np.float32))
    u = -spd * np.sin(rad)
    v = -spd * np.cos(rad)
    missing = ~np.isfinite(spd) | ~np.isfinite(rad)
    if missing.any():
        u[missing] = np.nan
        v[missing] = np.nan
    return u, v


def read_wind_uv(root):
    """Liest das 10-m-Windfeld eines geoeffneten .om-Containers als (u, v).

    Erkennt das Format automatisch (siehe oben) und liefert zusaetzlich einen
    Namen fuer die Log-Ausgabe zurueck: ``(u, v, layout)``.

    Wirft ``ValueError``, wenn kein bekanntes Windfeld enthalten ist.
    """
    names = {root.get_child_by_index(i).name for i in range(root.num_children)}

    if WIND_U_VARIABLE in names and WIND_V_VARIABLE in names:
        return (
            np.asarray(root.get_child_by_name(WIND_U_VARIABLE).read_array(...)),
            np.asarray(root.get_child_by_name(WIND_V_VARIABLE).read_array(...)),
            "u/v",
        )

    if WIND_SPEED_VARIABLE in names and WIND_DIRECTION_VARIABLE in names:
        u, v = _speed_direction_to_uv(
            root.get_child_by_name(WIND_SPEED_VARIABLE).read_array(...),
            root.get_child_by_name(WIND_DIRECTION_VARIABLE).read_array(...),
        )
        return u, v, "speed/direction"

    raise ValueError(
        "kein bekanntes Windfeld "
        f"(weder {WIND_U_VARIABLE}/{WIND_V_VARIABLE} noch "
        f"{WIND_SPEED_VARIABLE}/{WIND_DIRECTION_VARIABLE}); "
        f"vorhanden: {sorted(names)}"
    )


# --- 3. TILE-PAYLOAD (int16-Delta + Ganz-Tile-gzip) ---
#
# Pro Kachel werden U und V als je ein zeilenweise delta-kodierter int16-Stream
# abgelegt. Danach wird das KOMPLETTE Tile (Geo-Header + Laengen + beide Streams)
# als EIN gzip-Strom komprimiert und als eigene Datei abgelegt:
#
#     <output>/tiles/<timestamp>/<z>/<x>/<y>.bin
#
# Die Datei ist also ein fertiger gzip-Container. Der Host MUSS sie mit
# "Content-Encoding: gzip" ausliefern (Bunny: Edge Rule im Pull Zone), sonst
# sieht der Browser die gzip-Bytes als Nutzdaten. Mit dem Header entpackt der
# Browser transparent, d.h. fetch(...).arrayBuffer() liefert wieder die rohen
# Tile-Bytes (Geo-Header + Laengen + int16-Streams).
#
# Kachel-Aufbau VOR der gzip-Komprimierung:
#     [Geo-Header 24B][u_len 4B][v_len 4B][u_int16_delta][v_int16_delta]


# --- 4. KACHEL-PYRAMIDE ---
#
# Die Werte unten sind die HRRR-Defaults. Das Gitter-Profil einer Klasse
# (z.B. KnmiWindProcessor: max_zoom = 6) kann sie ueberschreiben; der Tiler
# liest sie dann aus self.max_zoom / self.finest_stride.
#
# Das reguläre Lat/Lon-Zwischengitter wird pyramidal abgelegt:
#
#     Level 0..max_zoom,   stride = finest_stride * 2^(max_zoom - z)
#
# TILE_MAX_ZOOM = 7 -> 8 Zoomstufen (0..7). Die feinste Stufe (z = 7) liegt bei
# stride = 1, also weiterhin auf voller Auflösung (0,025°). Gegenüber max_zoom = 8
# bedeutet das: eine Stufe weniger, dafür deckt jede Kachel 2x2 Kacheln der alten
# feinsten Stufe ab -> 4x mehr Zellen (und damit ~4x mehr Bytes) pro Kachel und 4x
# weniger Kacheln/Requests auf der feinsten Stufe. Die Gesamtdatenmenge bleibt gleich.
# Das passt zu 512-px-Kacheln im Frontend: deck.gl liest pro Kamerazoomstufe eine
# Stufe gröber (z = round(cameraZoom) - 1).
#
# Die Bytes hängen allein an TILE_FINEST_STRIDE (Abtastschritt der feinsten Stufe)
# und skalieren mit dessen Quadrat:
#
#     finest_stride = 1  ->  feinste Auflösung 0,025°  ->  Gesamtdaten ~4N/3
#     finest_stride = 2  ->  feinste Auflösung 0,050°  ->  Gesamtdaten ~ N/3 (4x kleiner)
#
# Das native HRRR-Raster ist 3 km = ~0,027°: 0,025° überabtastet es also noch,
# 0,05° unterabtastet es um ~1,85x. finest_stride = 2 ist daher ECHTER Detailverlust.
TILE_MAX_ZOOM = 7
TILE_FINEST_STRIDE = 1

# Ablage der Kacheln als einzelne Dateien (klassisches Tileserver-Layout):
#
#     <output>/tiles/<timestamp>/<z>/<x>/<y>.bin
#
# Format-, Bounds- und Coverage-Infos stehen einmal pro Run in index.json.
#
# Zur Dateiendung: ein CDN erkennt an der Endung NICHT, dass die Datei bereits
# gzip-komprimiert ist. Es gibt KEINE Endung, die automatisch
# "Content-Encoding: gzip" setzt – auch ".gz" nicht (das liefert lediglich
# "Content-Type: application/gzip", der Browser entpackt dann nichts). Der Header
# kommt ausschliesslich aus einer Edge Rule im Pull Zone. ".bin" ist daher
# neutral und wird dort auf application/octet-stream + Content-Encoding: gzip gemappt.
TILE_FILE_EXT = "bin"
TILES_SUBDIR = "tiles"   # NUR gzip-komprimierte Tiles (Edge Rule: Content-Encoding)

# Jede Kachel bekommt TILE_PADDING Zellen Halo auf allen 4 Seiten.
# Der Client interpoliert linear; ohne Halo fehlt ihm an den Kachelrändern der
# äußere Stützpunkt und die Interpolation wird dort ungenau (identisch zur
# D2-Pipeline). 1 = eine Zelle Überlappung. Der Geo-Header wächst mit
# (origin = gepolsterte Nord-/West-Ecke, rows/cols aus dem Ausschnitt) – der
# Client liest die Lage ohnehin aus dem Header, es muss nichts weiter angepasst
# werden.
TILE_PADDING = 1


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
    # --- Gitter-Profil -------------------------------------------------------
    # Diese Attribute beschreiben die Quelle. Unterklassen (z.B.
    # KnmiWindProcessor) ueberschreiben sie; alles andere (Resampling,
    # Tile-Encoding, index.json) bleibt modellunabhaengig.
    name = "HRRR"
    grid_nx = HRRR_NX
    grid_ny = HRRR_NY
    origin_x = HRRR_ORIGIN_X
    origin_y = HRRR_ORIGIN_Y
    dx = HRRR_DX
    dy = HRRR_DY
    default_resolution = 0.025
    max_zoom = TILE_MAX_ZOOM
    finest_stride = TILE_FINEST_STRIDE
    format_id = "hrrr-wind-int16-delta"
    run_key_format = "%Y%m%d_%H%M"
    step_type = "15min"

    #: Reihenfolge und Beschriftung der Zeitmessungen pro Timestep
    _STEP_LABELS = (
        ("read", "Read .om"),
        ("resample", "Resample -> Gitter"),
        ("tiles", "Tiles schreiben"),
        ("cleanup", "Temp loeschen"),
    )

    @staticmethod
    def _forward(lat, lon):
        return _lcc_forward(lat, lon)

    @staticmethod
    def _inverse(x, y):
        return _lcc_inverse(x, y)

    def __init__(self, output_folder="./wind_tiles_hrrr", src_resolution=None):
        init_start_time = time.perf_counter()
        self.output_folder = output_folder
        os.makedirs(self.output_folder, exist_ok=True)

        # Zeitanteile des letzten Timesteps (siehe process_om_file)
        self.last_step_timings = {}
        # Alle (z, x, y) des zuletzt geschriebenen Tile-Sets. Daraus baut die
        # Pipeline die Coverage fuer index.json.
        self.last_tile_coords = []
        # Diagnose des Resamples: hatte die Quelldatei NaN?
        # (siehe _resample_to_regular_grid)
        self.source_had_nan = False
        self._nan_warned = False
        # Zuletzt gesehenes Windfeld-Format ("u/v" oder "speed/direction") fuer
        # eine einmalige Log-Zeile statt einer pro Timestep.
        self._layout_logged = None

        # Auflösung des regulären Lat/Lon-Zwischengitters (Open-Meteo-kompatibel)
        self.src_resolution = src_resolution or self.default_resolution

        # Geografische Hülle des nativen Gitters (aus der Projektion bestimmt)
        self.lat_min, self.lat_max, self.lon_min, self.lon_max = self._geographic_bounds()

        # Reguläres Lat/Lon-Zielgitter (Süd->Nord bzw. West->Ost)
        self.src_lat_shape = int(round((self.lat_max - self.lat_min) / self.src_resolution)) + 1
        self.src_lon_shape = int(round((self.lon_max - self.lon_min) / self.src_resolution)) + 1
        self.src_lats = np.linspace(self.lat_min, self.lat_max, self.src_lat_shape)
        self.src_lons = np.linspace(self.lon_min, self.lon_max, self.src_lon_shape)

        # Mapping reguläres Gitter -> natives HRRR-Gitter (projizierte Row/Col-Indizes)
        self._build_resample_coords()

        init_duration = time.perf_counter() - init_start_time
        print(
            f"✅ [{type(self).__name__}] Gitter initialisiert: "
            f"Quelle {self.src_lon_shape}x{self.src_lat_shape} @ {self.src_resolution}° "
            f"(Init: {init_duration:.4f}s)"
        )
        print(
            f"   🌍 {self.name}-Gebiet / Frontend-Anker (SW): "
            f"lat {self.lat_min:.5f}..{self.lat_max:.5f}, "
            f"lon {self.lon_min:.5f}..{self.lon_max:.5f}"
        )
        print(
            f"   📦 Payload: int16-Delta (zeilenweise) + Ganz-Tile-gzip "
            f"L{PRED_GZIP_LEVEL}"
        )

    def _geographic_bounds(self, n=256):
        """Ermittelt die geografische Huelle des nativen Gitters.

        Dafuer wird der Gitterrand abgetastet und rueckprojiziert. Liefert
        (lat_min, lat_max, lon_min, lon_max).

        Die BBOX im ``crs_wkt`` einer .om-Datei ist dafuer NICHT geeignet: sie
        enthaelt nur die Gitterecken (0,0) und (nx-1, ny-1), nicht die Extrema
        der Huelle (beim rotierten KNMI-Gitter z.B. Island/Nordatlantik).
        """
        xs = np.linspace(0.0, self.grid_nx - 1, n)
        ys = np.linspace(0.0, self.grid_ny - 1, n)

        grid_i = np.concatenate([
            xs, xs,
            np.zeros_like(ys), np.full_like(ys, self.grid_nx - 1),
        ])
        grid_j = np.concatenate([
            np.full_like(xs, self.grid_ny - 1), np.zeros_like(xs),
            ys, ys,
        ])

        x = self.origin_x + grid_i * self.dx
        y = self.origin_y + grid_j * self.dy
        lat, lon = self._inverse(x, y)

        return float(lat.min()), float(lat.max()), float(lon.min()), float(lon.max())

    def _build_resample_coords(self):
        """Berechnet für jeden Punkt des regulären Lat/Lon-Zwischengitters die
        Zeilen-/Spaltenindizes im nativen Gitter (inkl. In-Bounds-Maske)."""
        lon_grid, lat_grid = np.meshgrid(self.src_lons, self.src_lats)

        x, y = self._forward(lat_grid, lon_grid)
        row = (y - self.origin_y) / self.dy
        col = (x - self.origin_x) / self.dx

        self.resample_coords = np.array([row, col], dtype=np.float32)
        self.resample_in_bounds = (
            (row >= 0) & (row <= self.grid_ny - 1)
            & (col >= 0) & (col <= self.grid_nx - 1)
        )

    def _resample_to_regular_grid(self, u_raw, v_raw):
        """Resampelt die projizierten HRRR-Felder (ny, nx) auf das reguläre
        Lat/Lon-Zwischengitter. Punkte außerhalb des HRRR-Rasters werden NaN.

        Rückgabe: (u_reg, v_reg)

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
        if u_raw.shape == (self.grid_nx, self.grid_ny) and u_raw.shape != (self.grid_ny, self.grid_nx):
            # Defensive: falls das .om transponiert vorliegt (Spalten zuerst)
            u_raw = u_raw.T
            v_raw = v_raw.T

        if u_raw.shape != (self.grid_ny, self.grid_nx):
            raise ValueError(
                f"unerwartete Gitterform {u_raw.shape}, "
                f"erwartet {(self.grid_ny, self.grid_nx)} ({self.name})"
            )

        self.source_had_nan = bool(np.isnan(u_raw).any()) or bool(np.isnan(v_raw).any())

        if self.source_had_nan:
            # --- Langsamer Pfad: NaN in den Quelldaten -> Maske mitsampeln ---
            if not self._nan_warned:
                print(
                    "   ⚠️ NaN in den Wind-Quelldaten -> "
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
        return u_reg, v_reg

    @staticmethod
    def _encode_field_pred(field, stats=None):
        """Kodiert ein 2D-Feld (float32, NaN = ungültig) als int16-Delta-Stream.

        Aufbau: int16-Werte, pro ZEILE horizontal differenziert. Die erste Spalte
        jeder Zeile bleibt absolut (Anker für die Kumulation im Client).
        NaN wird zu PRED_SENTINEL; weil der gültige Bereich auf +-PRED_VALID_MAX
        geklemmt ist, passt jedes Delta sicher in int16 (siehe Konstanten oben).

        Rückgabe sind die ROHEN int16-Bytes. Die gzip-Komprimierung passiert
        anschliessend über das KOMPLETTE Tile in ``_create_wind_direction_tiles``
        (ein gzip-Strom pro Datei).

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

    def _create_wind_direction_tiles(
        self,
        u_raw,
        v_raw,
        output_dir,
        min_zoom=0,
        max_zoom=None,
        finest_stride=None,
    ):
        """Schreibt je Zoomstufe/Kachel eine eigene, bereits gzip-komprimierte Datei.

        ``max_zoom`` und ``finest_stride`` kommen per Default aus dem Gitter-Profil
        der Klasse (``self.max_zoom`` / ``self.finest_stride``).

        Layout relativ zu ``output_dir``::

            <z>/<x>/<y>.<TILE_FILE_EXT>

        z.B. ``output/tiles/20261008_2000Z/7/20/51.bin``.

        Der Dateiinhalt ist ein einzelnes Tile ohne Container:
        ``[Geo-Header 24B][u_len 4B][v_len 4B][u_int16][v_int16]``,
        als EIN gzip-Strom. Weil das gzip nicht vom Webserver nachtraeglich
        angewendet wird, muss der Host ``Content-Encoding: gzip`` setzen
        (Edge Rule im Pull Zone). Danach entpackt der Browser transparent
        und ``response.arrayBuffer()`` liefert wieder die rohen Tile-Bytes.

        Als Gitter dient das reguläre Lat/Lon-Zwischengitter. Es wird bis ``max_zoom``
        pyramidal abgelegt. ``finest_stride`` ist der Abtastschritt auf der feinsten
        Stufe: 1 = volle Auflösung (``self.src_resolution``, kein Detailverlust),
        2 = doppelter Abtastschritt (4x kleiner, dafür sichtbar unschärfer).

        Jede Kachel bekommt ``TILE_PADDING`` Zellen Halo auf allen 4 Seiten
        (siehe Konstante), damit der Client an den Kachelrändern einen äußeren
        Stützpunkt für die lineare Interpolation hat.
        """

        if max_zoom is None:
            max_zoom = self.max_zoom
        if finest_stride is None:
            finest_stride = self.finest_stride

        # 1D Koordinatenvektoren des regulären Zwischengitters
        lats = self.src_lats
        lons = self.src_lons

        # Ein evtl. vorhandenes Set desselben Timesteps komplett ersetzen, damit
        # keine veralteten Kacheln liegen bleiben (z.B. aus einer frueheren
        # Zoom-Konfiguration mit max_zoom = 8).
        if os.path.isdir(output_dir):
            shutil.rmtree(output_dir)

        total_payload_bytes = 0
        max_tile_bytes = 0

        # Zeitmessung und Zähler
        t_tiles_start = time.perf_counter()
        t_encode = 0.0
        t_write = 0.0
        enc_stats = {}
        tiles_considered = 0
        tiles_skipped = 0
        tiles_written = 0
        # Koordinaten der tatsaechlich geschriebenen Kacheln (Coverage fuer den Client)
        self.last_tile_coords = []

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
                    c_start = max(0, col_indices[0] - TILE_PADDING)
                    c_end = min(
                        len(lons_lod),
                        col_indices[-1] + 1 + TILE_PADDING,
                    )

                    # lats_lod ist Süd->Nord sortiert: row_indices[0] = Südrand,
                    # row_indices[-1] = Nordrand. (Lat-Indizes für North-Up)
                    r_end_idx = max(0, row_indices[0] - TILE_PADDING)
                    r_start_idx = min(
                        len(lats_lod) - 1,
                        row_indices[-1] + TILE_PADDING,
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

                    # Ganz-Tile-gzip -> die Datei ist ein gzip-Container, der Host
                    # liefert sie mit Content-Encoding: gzip aus (Edge Rule).
                    # mtime=0 -> reproduzierbare Bytes (kein Zeitstempel im Header)
                    _t_gz = time.perf_counter()
                    tile_bytes = gzip.compress(
                        raw_tile, compresslevel=PRED_GZIP_LEVEL, mtime=0
                    )
                    enc_stats["codec"] = enc_stats.get("codec", 0.0) + (
                        time.perf_counter() - _t_gz
                    )

                    t_encode += time.perf_counter() - _t_enc

                    # 3. Datei schreiben: <output_dir>/<z>/<x>/<y>.<ext>
                    tile_dir = os.path.join(output_dir, str(z), str(x))
                    tile_path = os.path.join(tile_dir, f"{y}.{TILE_FILE_EXT}")
                    _t_wr = time.perf_counter()
                    os.makedirs(tile_dir, exist_ok=True)
                    with open(tile_path, "wb") as fh:
                        fh.write(tile_bytes)
                    t_write += time.perf_counter() - _t_wr

                    total_payload_bytes += len(tile_bytes)
                    max_tile_bytes = max(max_tile_bytes, len(tile_bytes))
                    tiles_written += 1
                    self.last_tile_coords.append((z, x, y))

        t_tiles = time.perf_counter() - t_tiles_start
        per_tile_kb = (
            total_payload_bytes / tiles_written / 1024.0 if tiles_written else 0.0
        )
        per_tile_ms = (t_encode / tiles_written * 1000.0) if tiles_written else 0.0

        print(f"✅ {tiles_written} Tile-Dateien geschrieben: {output_dir}")
        print(
            f"   📦 Payload (gzip, ganzes Tile): {total_payload_bytes / 1048576.0:.2f} MiB "
            f"in {tiles_written} Kacheln (L{PRED_GZIP_LEVEL}) | "
            f"Ø {per_tile_kb:.1f} KiB, max {max_tile_bytes / 1024.0:.1f} KiB"
        )
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
            f"      Kacheln: {tiles_written} geschrieben | {tiles_skipped} leer "
            f"übersprungen | {tiles_considered} im Gebiet | Ø {per_tile_ms:.2f} ms/Kachel"
        )

    def format_descriptor(self):
        """Format-, Pyramiden- und Gebietsbeschreibung fuer ``index.json``.

        Diese Werte sind pro Run konstant (Modulkonstanten + Gitterableitung)
        und werden deshalb EINMAL in den Index geschrieben statt pro Zeitstempel
        in eine eigene meta-Datei. Die Geo-Referenz jeder einzelnen Kachel steht
        ohnehin in deren 24-Byte-Header.
        """
        return {
            "format": {
                "id": self.format_id,
                "version": 1,
                "encoding": "gzip",
                "layout": (
                    "24B Geo-Header (6x float32 LE: origin_lng, origin_lat, "
                    "delta_lng, delta_lat, rows, cols) + 2x uint32 LE (u_len, "
                    "v_len in Bytes) + u_int16 LE + v_int16 LE. Beide Streams "
                    "row-major, North-Up (Zeile 0 = Nordrand) und pro Zeile "
                    "delta-kodiert; Spalte 0 jeder Zeile ist der absolute Anker."
                ),
                "scale": PRED_SCALE,
                "unit": "m/s",
                "sentinel": PRED_SENTINEL,
                "valid_max": PRED_VALID_MAX,
            },
            "pyramid": {
                "min_zoom": 0,
                "max_zoom": self.max_zoom,
                "finest_stride": self.finest_stride,
                "padding_cells": TILE_PADDING,
                "resolution_deg": self.src_resolution,
                "tile_ext": TILE_FILE_EXT,
            },
            "bounds": {
                "west": self.lon_min,
                "south": self.lat_min,
                "east": self.lon_max,
                "north": self.lat_max,
            },
        }

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
        # Das Windfeld liegt je Modell als u/v oder als Betrag/Richtung vor ->
        # read_wind_uv() erkennt das am Inhalt und liefert immer u/v.
        with OmFileReader(om_path) as root:
            try:
                u_raw, v_raw, layout = read_wind_uv(root)
            except ValueError as exc:
                print(f"❌ {os.path.basename(om_path)}: {exc}")
                return False

            if layout != self._layout_logged:
                print(f"   ℹ️ Windfeld-Format: {layout}")
                self._layout_logged = layout
        _lap("read")

        # --- B. Das native Gitter (LCC bzw. rotierte Lat/Lon) auf das reguläre
        # Lat/Lon-Zwischengitter resampeln (Open-Meteo-kompatibles Format)
        u_grid, v_grid = self._resample_to_regular_grid(u_raw, v_raw)
        _lap("resample")

        # --- C. Tiles (einzelne gzip-Dateien pro Kachel, Tileserver-Layout) ---
        # output_filename traegt nur den Zeitstempel ("...Z") als Namensbasis.
        # Ergebnis: <output>/tiles/<timestamp>/<z>/<x>/<y>.bin
        # Die Geo-/Format-Infos stehen im 24-Byte-Kachelkopf; die Liste der
        # vorhandenen Kacheln liefert self.last_tile_coords.
        tiles_dir = os.path.join(self.output_folder, TILES_SUBDIR, output_filename)
        self._create_wind_direction_tiles(u_grid, v_grid, tiles_dir)
        _lap("tiles")

        if os.path.exists(om_path):
            os.remove(om_path)
        _lap("cleanup")

        step_duration = time.perf_counter() - step_start_time
        print(f"    ⏱️ Dauer: {step_duration:.3f}s")
        self._print_step_timings(step_duration)

        tile_files = 0
        tile_bytes_total = 0
        if os.path.isdir(tiles_dir):
            for root_dir, _subdirs, filenames in os.walk(tiles_dir):
                for name in filenames:
                    tile_files += 1
                    tile_bytes_total += os.path.getsize(os.path.join(root_dir, name))
        print(
            f"    📁 Tiles {tile_files} Dateien, "
            f"{tile_bytes_total / 1048576.0:.2f} MiB -> {tiles_dir}"
        )
        return True


class KnmiWindProcessor(HrrrWindProcessor):
    """KNMI HARMONIE AROME Europe (``knmi_harmonie_arome_europe``).

    - 676 x 564 auf rotiertem Lat/Lon (5,56 km), Zielgitter 0,05° regular lat/lon
    - Quellfeld ist ``wind_speed_10m``/``wind_direction_10m`` (True North,
      "woher"); ``read_wind_uv()`` rechnet das bereits in u/v um.
    - Kachel-Pyramide bis z6: eine z6-Kachel deckt 4x so viel Flaeche ab wie
      eine z7-Kachel bei gleicher Aufloesung (0,05°, ``finest_stride=1``).
    """

    name = "KNMI"
    grid_nx = KNMI_NX
    grid_ny = KNMI_NY
    origin_x = KNMI_ORIGIN_X
    origin_y = KNMI_ORIGIN_Y
    dx = KNMI_DX
    dy = KNMI_DY
    default_resolution = 0.05
    max_zoom = 6
    finest_stride = 1
    # Bewusst dieselbe Format-ID wie HRRR/RRFS: das Payload-Layout ist
    # byte-identisch (24B Geo-Header, int16-Delta, sentinel, scale). Damit
    # braucht der Client keine zweite ID in SUPPORTED_FORMAT_IDS.
    format_id = "hrrr-wind-int16-delta"
    run_key_format = "%Y%m%d_%H"
    step_type = "hourly"

    _forward = staticmethod(_rot_forward)
    _inverse = staticmethod(_rot_inverse)


class NbmWindProcessor(HrrrWindProcessor):
    """NBM CONUS (``ncep_nbm_conus``).

    - 2345 x 1597 auf eigenem LCC-Gitter (2539,7 m), Zielgitter 0,0225°
      regulaeres Lat/Lon -> keine Unterabtastung gegenueber der Quelle.
    - Stuendliche Runs; stuendliche Daten bis +36 h, danach 3-stuendlich
      bis ~+260 h. Diese Pipeline liest standardmaessig +0 ... +30 h
      (31 stuendliche Zeitstempel).
    - Quellfeld ist ``wind_speed_10m``/``wind_direction_10m`` (m/s, True North,
      "woher"); ``read_wind_uv()`` rechnet das bereits in u/v um.
    - Kachel-Pyramide bis z7 wie HRRR. Bei 0,0225° hat eine z7-Kachel 125
      Knoten pro Kante (HRRR: 112,5) -> gleiche Groessenordnung, gleiche
      Pfeildichte im Frontend.
    """

    name = "NBM"
    grid_nx = NBM_NX
    grid_ny = NBM_NY
    origin_x = NBM_ORIGIN_X
    origin_y = NBM_ORIGIN_Y
    dx = NBM_DX
    dy = NBM_DY
    default_resolution = 0.0225
    max_zoom = 7
    finest_stride = 1
    # Payload-Layout identisch zu HRRR/RRFS/KNMI (siehe KNMI-Kommentar).
    format_id = "hrrr-wind-int16-delta"
    run_key_format = "%Y%m%d_%H"
    step_type = "hourly"
    # Nur ein Default, per HORIZON_END_HOURS weiterhin ueberschreibbar.
    default_horizon_hours = 30

    _forward = staticmethod(NBM_LCC.forward)
    _inverse = staticmethod(NBM_LCC.inverse)
