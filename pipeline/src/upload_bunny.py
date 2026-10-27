#!/usr/bin/env python3
"""Laedt einen Pipeline-Run nach Bunny Storage und schaltet index.json um.

Beispiel:
    BUNNY_STORAGE_ZONE=gdsfgfsdgsfd BUNNY_STORAGE_PASSWORD=... \
        python pipeline/src/upload_bunny.py --dir ./t --run 20261027_1200 --prune

Layout in der Zone:

    runs/<run>/tiles/<timestamp>/<z>/<x>/<y>.bin   (vor-komprimiert, gzip)
    runs/<run>/meta/<timestamp>.json
    index.json                                     (Root, zeigt auf runs/<run>/)

Ablauf
  1. alle Dateien unter runs/<run>/ hochladen
  2. index.json zuletzt in den Root -> das ist der atomare Switch. Bis dahin
     zeigt der alte Index weiter auf den alten, unangetasteten Run.
  3. mit --prune: Runs loeschen, die nicht zu den letzten --keep-runs gehoeren.
     Geloescht wird pro Zeitstempel-Verzeichnis parallel; ein einzelner
     rekursiver DELETE auf einen ganzen Run (~23k Objekte) dauert ~1 Minute.

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


async def list_runs(
    base_url: str, headers: dict[str, str], timeout_s: float
) -> list[str]:
    """Alle Run-Ordner unter runs/ (sortiert = chronologisch)."""
    entries = await list_dir(base_url, "runs", headers, timeout_s)
    return sorted(
        entry["ObjectName"] for entry in entries if entry.get("IsDirectory")
    )


async def prune_old_runs(
    base_url: str,
    headers: dict[str, str],
    current_run: str,
    keep_runs: int,
    concurrency: int,
    timeout_s: float,
    attempts: int,
) -> tuple[list[str], list[str]]:
    """Loescht alte Runs; `keep_runs` vorherige bleiben als Rollback-Fenster."""
    runs = await list_runs(base_url, headers, timeout_s)
    if current_run not in runs:
        # Der neue Run ist noch nicht sichtbar -> lieber nichts anfassen.
        log(f"::warning::Run {current_run} nicht unter runs/ gefunden, kein Aufraeumen")
        return [], []

    older = [name for name in runs if name != current_run]
    stale = older[:-keep_runs] if keep_runs > 0 else older
    if not stale:
        log("  keine alten Runs zu loeschen")
        return [], []

    log(f"  {len(stale)} alte Run(s) entfernen, {len(older) - len(stale)} bleiben")

    # Pro Run zuerst die Zeitstempel-Verzeichnisse einsammeln: die werden
    # parallel geloescht, weil ein rekursiver DELETE auf den ganzen Run
    # (~23k Objekte) gut eine Minute braucht.
    tile_targets: list[str] = []
    for run in stale:
        tiles = await list_dir(base_url, f"runs/{run}/tiles", headers, timeout_s)
        tile_targets += [
            f"runs/{run}/tiles/{entry['ObjectName']}/"
            for entry in tiles
            if entry.get("IsDirectory")
        ]

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

    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        # Phase 1: Kachel-Verzeichnisse parallel.
        await asyncio.gather(*(one(target) for target in tile_targets))
        # Phase 2: pro Run nur noch meta/*.json + leere Verzeichnisse.
        await asyncio.gather(*(one(f"runs/{run}/") for run in stale))
    return stale, errors


def collect_items(root: Path) -> list[tuple[Path, str]]:
    """Alle Dateien des Runs einsammeln (ohne Cloudflare-Artefakte)."""
    items: list[tuple[Path, str]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if rel in SKIP_FILES:
            log(f"  uebersprungen: {rel} (auf Bunny per Edge Rule gesetzt)")
            continue
        items.append((path, rel))
    return items


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
    parser.add_argument("--delete-concurrency", type=int, default=24)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--attempts", type=int, default=5)
    parser.add_argument(
        "--run",
        required=True,
        help="Run-ID, wird zum Ordnernamen unter runs/ (z.B. 20261027_1200)",
    )
    parser.add_argument(
        "--keep-runs",
        type=int,
        default=1,
        help="So viele vorherige Runs als Rollback-Fenster behalten",
    )
    parser.add_argument(
        "--prune", action="store_true", help="Alte Runs in der Zone loeschen"
    )
    args = parser.parse_args()

    if not args.zone or not args.password:
        parser.error("--zone/--password (oder BUNNY_STORAGE_ZONE/BUNNY_STORAGE_PASSWORD) fehlen")

    run = args.run.strip().strip("/")
    if not run:
        parser.error("--run ist leer")
    if "/" in run:
        parser.error("--run darf keine Schraegstriche enthalten")

    zone = args.zone.strip()
    base_url = f"{(args.endpoint or '').strip().rstrip('/')}/{zone}"
    headers = {"AccessKey": args.password.strip()}

    root = Path(args.dir).resolve()
    items = collect_items(root)
    if not items:
        parser.error(f"keine Dateien in {root}")

    # Alles unter runs/<run>/, nur index.json geht in den Root.
    run_prefix = f"runs/{run}"
    index_items: list[tuple[Path, str]] = []
    run_items: list[tuple[Path, str]] = []
    for local, rel in items:
        if rel == "index.json":
            index_items.append((local, "index.json"))
        else:
            run_items.append((local, f"{run_prefix}/{rel}"))
    total_bytes = sum(p.stat().st_size for p, _ in items)

    log(
        f"Upload {len(items)} Dateien ({total_bytes / 1048576:.1f} MiB) "
        f"-> {base_url}/{run_prefix}/  (concurrency={args.concurrency})"
    )

    started = time.perf_counter()
    errors = asyncio.run(
        upload_items(run_items, base_url, headers, args.concurrency,
                     args.timeout, args.attempts)
    )

    # Atomarer Switch: index.json erst, wenn der komplette Run oben ist.
    if errors:
        log(f"  {len(errors)} Fehler -> index.json bleibt unveraendert")
    else:
        errors += asyncio.run(
            upload_items(index_items, base_url, headers, 1,
                         args.timeout, args.attempts)
        )
    elapsed = time.perf_counter() - started

    deleted: list[str] = []
    # Aufraeumen erst, wenn der neue Index live ist – sonst wuerde der alte
    # Index auf geloeschte Daten zeigen.
    if args.prune and not errors:
        try:
            deleted, prune_errors = asyncio.run(
                prune_old_runs(base_url, headers, run, args.keep_runs,
                               args.delete_concurrency, args.timeout, args.attempts)
            )
            errors += prune_errors
        except RuntimeError as exc:
            log(f"::warning::{exc}")
            log("  Aufraeumen uebersprungen.")

    print(
        f"- Run: {run} ({len(items)} Dateien, {total_bytes / 1048576:.1f} MiB "
        f"in {elapsed:.1f} s)"
    )
    print(f"- Alte Runs entfernt: {len(deleted)}")
    switched = "ja" if (not errors and index_items) else "nein"
    print(f"- index.json umgestellt: {switched}")
    print(f"- Fehler: {len(errors)}")

    if errors:
        for error in errors[:10]:
            log(f"  FEHLER: {error}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
