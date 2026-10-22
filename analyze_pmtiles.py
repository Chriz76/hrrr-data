"""Analysiert einen PMTiles-Container: Header, Lookup-Tabelle (Directory) und Tiles.

Die "Lookup-Tabelle" im PMTiles-Format ist der Directory-Baum
(Root-Directory + optionale Leaf-Directories), der TileID -> (Offset, Laenge)
aufloest. Aufruf:  python analyze_pmtiles.py "<datei.pmtiles>"
"""
import gzip
import json
import sys

from pmtiles.tile import deserialize_directory, deserialize_header

PATH = sys.argv[1] if len(sys.argv) > 1 else "20261008_2000Z_dir.pmtiles"
data = open(PATH, "rb").read()
total = len(data)

h = deserialize_header(data[:127])
root_off = h["root_offset"]
root_len = h["root_length"]
root_raw = data[root_off:root_off + root_len]
root_dec = gzip.decompress(root_raw)

meta_raw = data[h["metadata_offset"]:h["metadata_offset"] + h["metadata_length"]]
meta = json.loads(gzip.decompress(meta_raw).decode())

ents = deserialize_directory(root_raw)
sz = [e.length for e in ents]

print("=" * 60)
print(f"Datei: {PATH}")
print("=" * 60)
print(f"Gesamtdatei             : {total:>10} B  ({total/1048576:.3f} MiB)")
print(f"Header                  : {127:>10} B")
print(f"Root-Directory (LOOKUP) : {root_len:>10} B  ({root_len/1024:.2f} KiB)")
print(f"   -> dekomprimiert     : {len(root_dec):>10} B")
print(f"Leaf-Directories        : {h['leaf_directory_length']:>10} B")
print(f"Metadata                : {h['metadata_length']:>10} B")
print(f"Tile-Daten              : {h['tile_data_length']:>10} B  ({h['tile_data_length']/1048576:.3f} MiB)")
print("-" * 60)
print(f"Anteil Lookup an Datei  : {root_len/total*100:.3f} %")
print(f"Eintraege               : {len(ents)}")
print(f"Tile-Groesse min/avg/max: {min(sz)} / {sum(sz)//len(sz)} / {max(sz)} B")
print(f"Summe Tile-Laengen      : {sum(sz)} B  (== tile_data_length: {sum(sz) == h['tile_data_length']})")
print(f"zoom                    : {h['min_zoom']}..{h['max_zoom']}")
print(f"internal_compression    : {h['internal_compression']}")
print(f"tile_compression        : {h['tile_compression']}")
print("-" * 60)
print("Metadata:")
print(json.dumps(meta, indent=2, ensure_ascii=False))
print("=" * 60)
