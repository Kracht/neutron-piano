#!/usr/bin/env python3
"""Neutron Piano dev server: serves the static page and proxies NMDB.

NMDB (www.nmdb.eu/nest) sends no CORS headers and answers with an HTML page
that wraps a semicolon-separated ASCII table, so the browser cannot read it
directly. This server fetches it, parses the table and returns JSON.

    python3 serve.py [port]      # default 8765
"""

import json
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# Stations with real-time 1-min data at the time of writing. NMDB silently
# drops stations without data, so the header line of the reply is the truth.
STATIONS = [
    "TERA", "SOPO", "DOMC", "THUL", "INVK", "FSMT", "NAIN", "PWNK", "TXBY",
    "APTY", "OULU", "KERG", "YKTK", "KIEL2", "NEWK", "DRBS", "IRKT", "LMKS",
    "JUNG1", "BKSN", "ROME", "AATB", "CALM", "MXCO", "ATHN",
]

NEST = "https://www.nmdb.eu/nest/draw_graph.php"
CACHE_SECONDS = 55
_cache: dict[int, tuple[float, bytes]] = {}
_lock = threading.Lock()


def fetch_nmdb(days: int) -> dict:
    params = [("formchk", "1")]
    params += [("stations[]", s) for s in STATIONS]
    params += [
        ("tabchoice", "revori"),
        ("dtype", "corr_for_efficiency"),
        ("tresolution", "1"),
        ("force", "1"),
        ("yunits", "0"),
        ("date_choice", "last"),
        ("last_days", str(days)),
        ("output", "ascii"),
    ]
    url = NEST + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "neutron-piano"})
    with urllib.request.urlopen(req, timeout=40) as r:
        text = r.read().decode("utf-8", "replace")

    stations: list[str] = []
    rows: list[list] = []
    for line in text.splitlines():
        if re.match(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d;", line):
            parts = line.split(";")
            # NMDB times are UTC
            t = parts[0].replace(" ", "T") + "Z"
            vals = [None if p.strip() == "null" else float(p) for p in parts[1:]]
            rows.append([t] + vals)
        elif not rows and re.fullmatch(r"\s+([A-Z0-9]{3,6}\s*)+", line):
            stations = line.split()
    if rows and len(rows[0]) - 1 != len(stations):
        raise ValueError("NMDB header/column mismatch")
    return {"stations": stations, "rows": rows, "fetched": time.time()}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(ROOT), **kw)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path not in ("/api/nmdb", "/nmdb.php"):
            return super().do_GET()
        q = urllib.parse.parse_qs(u.query)
        days = max(1, min(7, int(q.get("days", ["1"])[0])))
        with _lock:
            hit = _cache.get(days)
            if hit and time.time() - hit[0] < CACHE_SECONDS:
                body = hit[1]
            else:
                try:
                    body = json.dumps(fetch_nmdb(days)).encode()
                    _cache[days] = (time.time(), body)
                except Exception as e:  # serve stale data rather than nothing
                    if hit:
                        body = hit[1]
                    else:
                        self.send_error(502, f"NMDB: {e}")
                        return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        if "/api/" in (args[0] if args else ""):
            super().log_message(fmt, *args)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    print(f"Neutron Piano: http://localhost:{port}/")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
