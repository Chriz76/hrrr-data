#!/usr/bin/env python3
"""Benchmark: parallele HTTP-PUTs nach Bunny Storage.

Misst ausschliesslich den Upload (die Dateien liegen bereits auf der Platte).
Aufruf:

    BUNNY_STORAGE_ZONE=winddata BUNNY_STORAGE_PASSWORD=xxx \
        python3 bench/upload_bench.py --dir /tmp/bench/t \
        --prefix bench/123/http-c48 --concurrency 48

Ausgabe:
  * Fortschritt/Fehler -> stderr (landet im Action-Log)
  * Ergebnis als Markdown-Liste -> stdout (fuer $GITHUB_STEP_SUMMARY)

Exit-Code 1, wenn mindestens ein Upload endgueltig fehlgeschlagen ist.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

import aiohttp

# 429 = Bunny-Connection-Limit (100 pro IP / 250 pro Zone & Server),
# 5xx = transient. Alles andere (401/400) ist ein Konfigurationsfehler und
# wird sofort gemeldet, statt sinnlos zu wiederholen.
RETRY_STATUSES = {429, 500, 502, 503, 504}


def log(message: str = "") -> None:
    """Meldungen nach stderr – stdout ist fuer die Job-Summary reserviert."""
    print(message, file=sys.stderr)


async def upload_one(
    session: aiohttp.ClientSession,
    sem: asyncio.Semaphore,
    url: str,
    path: Path,
    headers: dict[str, str],
    max_attempts: int,
) -> str | None:
    """Ein PUT pro Datei. None = Erfolg, sonst Fehlertext."""
    async with sem:
        last_error = "unbekannt"
        for attempt in range(1, max_attempts + 1):
            try:
                # Datei pro Versuch neu oeffnen: der Stream ist nach einem
                # Fehlversuch verbraucht. aiohttp setzt Content-Length per fstat.
                with path.open("rb") as fh:
                    async with session.put(
                        url, data=fh, headers=headers
                    ) as resp:
                        if resp.status in (200, 201):
                            return None
                        last_error = f"HTTP {resp.status}: {(await resp.text())[:160]}"
                        if resp.status not in RETRY_STATUSES:
                            return f"{path.name} -> {last_error}"
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"

            if attempt < max_attempts:
                await asyncio.sleep(min(2 ** (attempt - 1), 8))

        return f"{path.name} -> {last_error} (nach {max_attempts} Versuchen)"


async def run(files: list[Path], base_url: str, concurrency: int,
              max_attempts: int, timeout_s: float, root: Path,
              headers: dict[str, str]) -> dict:
    sem = asyncio.Semaphore(concurrency)
    # Keep-alive ist der entscheidende Punkt bei 15k kleinen Dateien:
    # ohne Pool zahlt man pro Datei einen TLS-Handshake.
    connector = aiohttp.TCPConnector(limit=concurrency, ttl_dns_cache=300)
    timeout = aiohttp.ClientTimeout(total=timeout_s, sock_connect=15)

    done = 0
    total = len(files)
    step = max(1, total // 10)

    async def task(path: Path) -> str | None:
        nonlocal done
        rel = path.relative_to(root).as_posix()
        error = await upload_one(
            session, sem, f"{base_url}/{rel}", path, headers, max_attempts
        )
        if error is None:
            done += 1
            if done % step == 0:
                rate = done / (time.perf_counter() - started)
                print(f"  {done}/{total} ({rate:.0f} Dateien/s)", file=sys.stderr)
        return error

    started = time.perf_counter()
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        results = await asyncio.gather(*(task(p) for p in files))
    elapsed = time.perf_counter() - started

    errors = [r for r in results if r is not None]
    return {
        "files": total,
        "bytes": sum(p.stat().st_size for p in files),
        "seconds": elapsed,
        "errors": errors,
        "concurrency": concurrency,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", required=True, help="Lokaler Ordner mit den Testdateien")
    parser.add_argument("--prefix", required=True, help="Zielpfad in der Zone (ohne fuehrenden Slash)")
    parser.add_argument("--zone", default=os.environ.get("BUNNY_STORAGE_ZONE"))
    parser.add_argument("--password", default=os.environ.get("BUNNY_STORAGE_PASSWORD"))
    parser.add_argument(
        "--endpoint",
        default=os.environ.get("BUNNY_ENDPOINT", "https://storage.bunnycdn.com"),
        help="Regions-Endpoint der Zone, z.B. https://ny.storage.bunnycdn.com",
    )
    parser.add_argument("--concurrency", type=int, default=64)
    parser.add_argument("--max-attempts", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args()

    if not args.zone or not args.password:
        parser.error("--zone/--password (oder BUNNY_STORAGE_ZONE/BUNNY_STORAGE_PASSWORD) fehlen")

    # Secrets aus GitHub Actions schleppen gern ein Leerzeichen/Newline mit –
    # ein Header mit Whitespace ergibt 401.
    if args.zone != args.zone.strip() or args.password != args.password.strip():
        log("  Hinweis: Leerzeichen am Rand von Zone/Passwort entfernt.")
    args.zone = args.zone.strip()
    args.password = args.password.strip()
    args.endpoint = (args.endpoint or "").strip() or "https://storage.bunnycdn.com"

    root = Path(args.dir).resolve()
    files = sorted(p for p in root.rglob("*") if p.is_file())
    if not files:
        parser.error(f"keine Dateien in {root}")

    # Auth fuer die HTTP-API: Storage-Zone-Passwort als AccessKey
    # (nicht der Account-API-Key, der gilt nur fuer api.bunny.net).
    headers = {
        "AccessKey": args.password,
        "Content-Type": "application/octet-stream",
    }
    asyncio.run(_amain(args, files, root, headers))
    return 0


async def _amain(args, files, root, headers):
    base_url = f"{args.endpoint.rstrip('/')}/{args.zone}/{args.prefix.strip('/')}"

    log(
        f"PUT {len(files)} Dateien -> {base_url} "
        f"(concurrency={args.concurrency})"
    )

    result = await run(
        files, base_url, args.concurrency, args.max_attempts, args.timeout,
        root, headers,
    )

    mib = result["bytes"] / 1048576
    secs = result["seconds"]
    failed = len(result["errors"])
    print(f"- Dateien: {result['files']} ({mib:.0f} MiB)")
    print(f"- Dauer: {secs:.1f} s")
    if failed == result["files"]:
        print("- **Alle Uploads fehlgeschlagen – Kennzahlen unbrauchbar**")
    else:
        print(f"- Rate: {result['files'] / secs:.0f} Dateien/s, {mib / secs:.1f} MiB/s")
    print(f"- Parallelitaet: {result['concurrency']}")
    print(f"- Fehler: {failed}")
    if result["errors"]:
        for err in result["errors"][:5]:
            log(f"  FEHLER: {err}")
        sys.exit(1)
    if result["errors"]:
        for err in result["errors"][:10]:
            print(f"  FEHLER: {err}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    sys.exit(main())
