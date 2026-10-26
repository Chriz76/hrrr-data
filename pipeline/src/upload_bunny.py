#!/usr/bin/env python3
"""Laedt den Pipeline-Output nach Bunny Storage und raeumt alte Tile-Sets ab.

Beispiel:
    BUNNY_STORAGE_ZONE=gdsfgfsdgsfd BUNNY_STORAGE_PASSWORD=... \
        python pipeline/src/upload_bunny.py --dir ./t --prune

Abbildung 1:1, die Pfade bleiben wie in index.json deklariert:

    tiles/<timestamp>/<z>/<x>/<y>.bin   (vor-komprimiert, gzip)
    meta/<timestamp>.json
    index.json

Ablauf
  1. alle Dateien parallel hochladen (index.json zuletzt, damit der Index nie
     auf noch fehlende Kacheln zeigt)
  2. mit --prune: Zeitstempel-Verzeichnisse unter tiles/ und deren
     meta/<ts>.json loeschen, die im aktuellen Lauf nicht vorkommen.
     Pro altem Set ist das EIN rekursiver DELETE (kein 600er-Loop) und
     mehrere Sets laufen parallel (Limit: 30 gleichzeitige DELETEs).
  3. andere Pfade in der Zone (z.B. bench/) werden nie angefasst.

Nicht hochgeladen wird die Cloudflare-Datei `_headers` – auf Bunny werden die
Response-Header per Edge Rule im Pull Zone gesetzt.

Ausgabe: Fortschritt/Fehler -> stderr, Zusammenfassung -> stdout.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

import aiohttp

RETRY_STATUSES = {429, 500, 502, 503, 504}

CONTENT_TYPES = {
    ".json": "application/json; charset=utf-8",
    ".bin": "application/octet-stream",
}

# Cloudflare-Artefakt: auf Bunny wirkungslos.
SKIP_FILES = {"_headers"}


def log(message: str = "") -> None:
    print(message, file=sys.stderr, flush=True)


def content_type(path: Path) -> str:
    return CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")


async def upload_one(
    session: aiohttp.ClientSession,
    sem: asyncio.Semaphore,
    url: str,
    path: Path,
    headers: dict[str, str],
    attempts: int,
) -> str | None:
    """Ein PUT pro Datei. None = Erfolg, sonst Fehlertext."""
    async with sem:
        last = "unbekannt"
        for n in range(1, attempts + 1):
            try:
                # Datei je Versuch neu oeffnen: der Stream ist verbraucht.
                with path.open("rb") as fh:
                    async with session.put(
                        url,
                        data=fh,
                        headers={**headers, "Content-Type": content_type(path)},
                    ) as resp:
                        if resp.status in (200, 201):
                            return None
                        last = f"HTTP {resp.status}: {(await resp.text())[:200]}"
                        if resp.status not in RETRY_STATUSES:
                            return last
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last = f"{type(exc).__name__}: {exc}"
            if n < attempts:
                await asyncio.sleep(min(2 ** (n - 1), 8))
        return f"{last} (nach {attempts} Versuchen)"


async def delete_one(
    session: aiohttp.ClientSession,
    sem: asyncio.Semaphore,
    url: str,
    headers: dict[str, str],
    attempts: int,
) -> str | None:
    """DELETE (Verzeichnis = rekursiv). 404 gilt als Erfolg."""
    async with sem:
        last = "unbekannt"
        for n in range(1, attempts + 1):
            try:
                async with session.delete(url, headers=headers) as resp:
                    if resp.status in (200, 204, 404):
                        return None
                    last = f"HTTP {resp.status}: {(await resp.text())[:200]}"
                    if resp.status not in RETRY_STATUSES:
                        return last
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last = f"{type(exc).__name__}: {exc}"
            if n < attempts:
                await asyncio.sleep(min(2 ** (n - 1), 8))
        return f"{last} (nach {attempts} Versuchen)"


async def upload_items(
    items: list[tuple[Path, str]],
    base_url: str,
    headers: dict[str, str],
    concurrency: int,
    timeout_s: float,
    attempts: int,
) -> list[str]:
    sem = asyncio.Semaphore(concurrency)
    connector = aiohttp.TCPConnector(limit=concurrency, ttl_dns_cache=300)
    timeout = aiohttp.ClientTimeout(total=timeout_s, sock_connect=15)
    errors: list[str] = []
    done = 0
    step = max(1, len(items) // 10)

    async def one(local: Path, rel: str) -> None:
        nonlocal done
        error = await upload_one(
            session, sem, f"{base_url}/{rel}", local, headers, attempts
        )
        if error:
            errors.append(f"{rel} -> {error}")
        else:
            done += 1
            if done % step == 0:
                log(f"  {done}/{len(items)} Dateien")

    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        await asyncio.gather(*(one(local, rel) for local, rel in items))
    return errors


async def list_dir(
    base_url: str, path: str, headers: dict[str, str], timeout_s: float
) -> list[dict]:
    """Inhalt eines Verzeichnisses; fehlendes Verzeichnis = leer."""
    timeout = aiohttp.ClientTimeout(total=timeout_s, sock_connect=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(f"{base_url}/{path}/", headers=headers) as resp:
            if resp.status == 404:
                return []
            if resp.status != 200:
                raise RuntimeError(f"Listing {path}/ fehlgeschlagen: HTTP {resp.status}")
            return json.loads(await resp.text())


async def prune_old_sets(
    base_url: str,
    headers: dict[str, str],
    keep: set[str],
    concurrency: int,
    timeout_s: float,
    attempts: int,
) -> tuple[list[str], list[str]]:
    """Loescht tiles/<ts>/ und meta/<ts>.json, die es lokal nicht mehr gibt."""
    tiles = await list_dir(base_url, "tiles", headers, timeout_s)
    stale_dirs = sorted(
        entry["ObjectName"]
        for entry in tiles
        if entry.get("IsDirectory") and entry["ObjectName"] not in keep
    )

    meta = await list_dir(base_url, "meta", headers, timeout_s)
    keep_meta = {f"{name}.json" for name in keep}
    stale_meta = sorted(
        entry["ObjectName"]
        for entry in meta
        if not entry.get("IsDirectory")
        and entry["ObjectName"].startswith("20")
        and entry["ObjectName"] not in keep_meta
    )

    targets = [f"tiles/{name}/" for name in stale_dirs]
    targets += [f"meta/{name}" for name in stale_meta]
    if not targets:
        return [], []

    log(f"  {len(stale_dirs)} alte Tile-Set(s) + {len(stale_meta)} Metadatei(en) entfernen")

    sem = asyncio.Semaphore(concurrency)
    connector = aiohttp.TCPConnector(limit=concurrency)
    timeout = aiohttp.ClientTimeout(total=timeout_s, sock_connect=15)
    errors: list[str] = []

    async def one(target: str) -> None:
        error = await delete_one(
            session, sem, f"{base_url}/{target}", headers, attempts
        )
        if error:
            errors.append(f"{target} -> {error}")
        else:
            log(f"  geloescht: {target}")

    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        await asyncio.gather(*(one(target) for target in targets))
    return targets, errors


def collect_items(root: Path) -> tuple[list[tuple[Path, str]], set[str]]:
    """Dateien einsammeln; liefert (Upload-Liste, vorhandene Zeitstempel)."""
    items: list[tuple[Path, str]] = []
    keep: set[str] = set()
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if rel in SKIP_FILES:
            log(f"  uebersprungen: {rel} (auf Bunny per Edge Rule gesetzt)")
            continue
        items.append((path, rel))
        parts = rel.split("/")
        # tiles/<timestamp>/<z>/<x>/<y>.bin -> Zeitstempel fuer das Aufraeumen
        if parts[0] == "tiles" and len(parts) >= 2:
            keep.add(parts[1])
    return items, keep


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", required=True, help="Lokaler Deploy-Ordner (z.B. ./t)")
    parser.add_argument("--zone", default=os.environ.get("BUNNY_STORAGE_ZONE"))
    parser.add_argument("--password", default=os.environ.get("BUNNY_STORAGE_PASSWORD"))
    parser.add_argument(
        "--endpoint",
        default=os.environ.get("BUNNY_ENDPOINT", "https://storage.bunnycdn.com"),
        help="Regions-Endpoint der Zone (DE: https://storage.bunnycdn.com)",
    )
    parser.add_argument("--concurrency", type=int, default=48)
    parser.add_argument("--delete-concurrency", type=int, default=12)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--attempts", type=int, default=5)
    parser.add_argument(
        "--prune", action="store_true", help="Alte Tile-Sets in der Zone loeschen"
    )
    args = parser.parse_args()

    if not args.zone or not args.password:
        parser.error("--zone/--password (oder BUNNY_STORAGE_ZONE/BUNNY_STORAGE_PASSWORD) fehlen")

    zone = args.zone.strip()
    base_url = f"{(args.endpoint or '').strip().rstrip('/')}/{zone}"
    headers = {"AccessKey": args.password.strip()}

    root = Path(args.dir).resolve()
    items, keep = collect_items(root)
    if not items:
        parser.error(f"keine Dateien in {root}")

    # index.json zuletzt: der Index darf nie vor seinen Kacheln live sein.
    index_items = [it for it in items if it[1] == "index.json"]
    data_items = [it for it in items if it[1] != "index.json"]
    total_bytes = sum(p.stat().st_size for p, _ in items)

    log(
        f"Upload {len(items)} Dateien ({total_bytes / 1048576:.1f} MiB) "
        f"-> {base_url}/  (concurrency={args.concurrency})"
    )

    started = time.perf_counter()
    errors = asyncio.run(
        upload_items(data_items, base_url, headers, args.concurrency,
                     args.timeout, args.attempts)
    )
    errors += asyncio.run(
        upload_items(index_items, base_url, headers, args.concurrency,
                     args.timeout, args.attempts)
    )
    elapsed = time.perf_counter() - started

    deleted: list[str] = []
    # Erst aufraeumen, wenn alles angekommen ist – sonst zeigt der neue Index
    # auf Kacheln, die schon geloescht wurden.
    if args.prune and not errors:
        try:
            deleted, prune_errors = asyncio.run(
                prune_old_sets(base_url, headers, keep, args.delete_concurrency,
                               args.timeout, args.attempts)
            )
            errors += prune_errors
        except RuntimeError as exc:
            log(f"::warning::{exc}")
            log("  Aufraeumen uebersprungen.")

    print(
        f"- Dateien: {len(items)} ({total_bytes / 1048576:.1f} MiB) "
        f"in {elapsed:.1f} s ({len(items) / elapsed:.0f}/s)"
    )
    print(f"- Alte Sets entfernt: {len(deleted)}")
    print(f"- Fehler: {len(errors)}")

    if errors:
        for error in errors[:10]:
            log(f"  FEHLER: {error}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
