#!/usr/bin/env python3

from __future__ import annotations

import time
from pathlib import Path

from flask import Flask, jsonify, render_template, request

from china_airway_resolver import ChinaAirwayResolver, ResolverError


BASE_DIR = Path(__file__).parent
AIRWAY_DIR = BASE_DIR / "Airway_FIles"

app = Flask(__name__, template_folder="templates", static_folder="static")


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/health")
def health():
    pdf_count = len(list(AIRWAY_DIR.glob("*.pdf")))
    return jsonify({"status": "ok", "pdfCount": pdf_count})


@app.post("/api/resolve")
def resolve():
    payload = request.get_json(silent=True) or {}
    query = str(payload.get("query", "")).strip()
    if not query:
        return jsonify({"ok": False, "error": "Input is required"}), 400

    started = time.perf_counter()
    resolver = ChinaAirwayResolver(AIRWAY_DIR)

    try:
        result = resolver.resolve(query)
    except ResolverError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 422

    elapsed_ms = round((time.perf_counter() - started) * 1000.0, 2)
    pdf_names = sorted([p.name for p in AIRWAY_DIR.glob("*.pdf")])

    return jsonify(
        {
            "ok": True,
            "result": result,
            "latencyMs": elapsed_ms,
            "pdfsUsed": pdf_names,
            "computedFresh": resolver.last_data_refresh == "rebuilt-from-pdfs",
            "dataRefreshMode": resolver.last_data_refresh,
        }
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)
