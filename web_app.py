#!/usr/bin/env python3

from __future__ import annotations

import re
import time
from pathlib import Path

from flask import Flask, jsonify, render_template, request

from china_airway_resolver import ChinaAirwayResolver, ResolverError


BASE_DIR = Path(__file__).parent
AIRWAY_DIR = BASE_DIR / "Airway_FIles"

app = Flask(__name__, template_folder="templates", static_folder="static")

DAY_TO_INDEX = {
    "MON": 0,
    "TUE": 1,
    "WED": 2,
    "THU": 3,
    "FRI": 4,
    "SAT": 5,
    "SUN": 6,
}
INDEX_TO_DAY = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
NZDT_OFFSET_MINUTES = 13 * 60


def _compress_day_indices(day_indices: set[int]) -> str:
    if not day_indices:
        return ""
    ordered = sorted(day_indices)
    groups: list[list[int]] = [[ordered[0]]]
    for idx in ordered[1:]:
        if idx == groups[-1][-1] + 1:
            groups[-1].append(idx)
        else:
            groups.append([idx])

    parts: list[str] = []
    for group in groups:
        if len(group) == 1:
            parts.append(INDEX_TO_DAY[group[0]])
        elif len(group) == 2:
            parts.append(INDEX_TO_DAY[group[0]])
            parts.append(INDEX_TO_DAY[group[1]])
        else:
            parts.append(f"{INDEX_TO_DAY[group[0]]}-{INDEX_TO_DAY[group[-1]]}")
    return " ".join(parts)


def _compress_day_sequence(day_indices: list[int]) -> str:
    if not day_indices:
        return ""
    unique_sorted = sorted(set(day_indices))
    best_repr = None

    for pivot in range(7):
        rotated = sorted(((d - pivot) % 7) for d in unique_sorted)
        ranges: list[tuple[int, int]] = []
        start = rotated[0]
        end = rotated[0]
        for idx in rotated[1:]:
            if idx == end + 1:
                end = idx
            else:
                ranges.append((start, end))
                start = idx
                end = idx
        ranges.append((start, end))

        parts: list[str] = []
        for left, right in ranges:
            left_day = (left + pivot) % 7
            right_day = (right + pivot) % 7
            if left == right:
                parts.append(INDEX_TO_DAY[left_day])
            else:
                parts.append(f"{INDEX_TO_DAY[left_day]}-{INDEX_TO_DAY[right_day]}")

        candidate = " ".join(parts)
        key = (len(parts), len(candidate), candidate)
        if best_repr is None or key < best_repr[0]:
            best_repr = (key, candidate)

    return best_repr[1]


def _parse_day_tokens(day_text: str) -> set[int]:
    tokens = [tok.strip().upper() for tok in day_text.split() if tok.strip()]
    parsed: set[int] = set()
    for tok in tokens:
        if "-" in tok:
            left, right = tok.split("-", 1)
            if left not in DAY_TO_INDEX or right not in DAY_TO_INDEX:
                continue
            start = DAY_TO_INDEX[left]
            end = DAY_TO_INDEX[right]
            if start <= end:
                parsed.update(range(start, end + 1))
            else:
                parsed.update(range(start, 7))
                parsed.update(range(0, end + 1))
        elif tok in DAY_TO_INDEX:
            parsed.add(DAY_TO_INDEX[tok])
    return parsed


def _shift_hhmm_utc(hhmm: str) -> tuple[str, int]:
    minutes = int(hhmm[:2]) * 60 + int(hhmm[2:])
    utc_minutes = minutes - NZDT_OFFSET_MINUTES
    day_shift = 0
    while utc_minutes < 0:
        utc_minutes += 24 * 60
        day_shift -= 1
    while utc_minutes >= 24 * 60:
        utc_minutes -= 24 * 60
        day_shift += 1
    hour = utc_minutes // 60
    minute = utc_minutes % 60
    return f"{hour:02d}{minute:02d}", day_shift


def _convert_line_nzdt_to_utc(line: str) -> str:
    # Supports common NOTAM lines like "0600-2035 MON-FRI" and keeps line shape.
    pattern = re.compile(r"\b(\d{4})-(\d{4})\b(?:\s+([A-Z\- ]+))?", re.IGNORECASE)
    match = pattern.search(line)
    if not match:
        return line

    start_local = match.group(1)
    end_local = match.group(2)
    day_text = (match.group(3) or "").strip().upper()

    start_utc, start_shift = _shift_hhmm_utc(start_local)
    end_utc, end_shift = _shift_hhmm_utc(end_local)

    replacement = f"{start_utc}-{end_utc}"

    if day_text:
        day_indices = _parse_day_tokens(day_text)
        if day_indices:
            shifted_start_days = {(idx + start_shift) % 7 for idx in day_indices}
            shifted_end_days = {(idx + end_shift) % 7 for idx in day_indices}
            merged_days = shifted_start_days.union(shifted_end_days)
            replacement = f"{replacement} {_compress_day_indices(merged_days)}"

    return f"{line[:match.start()]}{replacement}{line[match.end():]}"


def convert_nz_notam_to_utc(text: str) -> str:
    lines = text.splitlines()

    schedule_pattern = re.compile(r"^\s*(\d{4})-(\d{4})\s+([A-Z\- ]+)\s*$", re.IGNORECASE)
    schedule_items: list[tuple[str, str, set[int]]] = []

    for line in lines:
        match = schedule_pattern.match(line)
        if not match:
            continue
        start_local = match.group(1)
        end_local = match.group(2)
        day_text = match.group(3).strip().upper()
        day_indices = _parse_day_tokens(day_text)
        if not day_indices:
            continue
        start_utc, start_shift = _shift_hhmm_utc(start_local)
        end_utc, end_shift = _shift_hhmm_utc(end_local)
        # For overnight windows, output is tied to the UTC start day only.
        start_days = {(idx + start_shift) % 7 for idx in day_indices}
        schedule_items.append((start_utc, end_utc, start_days))

    if not schedule_items:
        converted = [_convert_line_nzdt_to_utc(line) for line in lines]
        return "\n".join(converted)

    grouped: dict[tuple[str, str], set[int]] = {}
    for start_utc, end_utc, days in schedule_items:
        key = (start_utc, end_utc)
        grouped.setdefault(key, set()).update(days)

    output_lines: list[str] = []
    for (start_utc, end_utc), days in sorted(grouped.items(), key=lambda item: min(item[1])):
        day_text = _compress_day_sequence(sorted(days))
        output_lines.append(f"{day_text} {start_utc}-{end_utc},")

    return "\n".join(output_lines)


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


@app.post("/api/nz-convert")
def nz_convert():
    payload = request.get_json(silent=True) or {}
    text = str(payload.get("text", "")).strip()
    if not text:
        return jsonify({"ok": False, "error": "Input is required"}), 400

    started = time.perf_counter()
    converted = convert_nz_notam_to_utc(text)
    elapsed_ms = round((time.perf_counter() - started) * 1000.0, 2)

    return jsonify(
        {
            "ok": True,
            "result": converted,
            "latencyMs": elapsed_ms,
            "mode": "nzdt-to-utc",
        }
    )


import re

def convert_aus_notam(text: str) -> str:
    lines = text.splitlines()

    # match lines like "MON-FRI 2145-0915" or "    SAT 2245-0550"
    schedule_pattern = re.compile(r"^\s*([A-Z\- ]+?)\s+(\d{4})-(\d{4})\s*$", re.IGNORECASE)
    schedule_items = []

    # import from web_app using exec so we don't duplicate
    from web_app import _parse_day_tokens, _compress_day_sequence
    
    for line in lines:
        match = schedule_pattern.match(line)
        if not match:
            continue
        day_text = match.group(1).strip().upper()
        start_local = match.group(2)
        end_local = match.group(3)
        day_indices = _parse_day_tokens(day_text)
        if not day_indices:
            continue
        
        # determine if overnight
        start_mins = int(start_local[:2]) * 60 + int(start_local[2:])
        end_mins = int(end_local[:2]) * 60 + int(end_local[2:])
        is_overnight = start_mins > end_mins

        shift = -1 if is_overnight else 0
        new_days = {(idx + shift) % 7 for idx in day_indices}
        
        schedule_items.append((start_local, end_local, new_days))
        
    if not schedule_items:
        return text
        
    grouped = {}
    for start_time, end_time, days in schedule_items:
        key = (start_time, end_time)
        grouped.setdefault(key, set()).update(days)
        
    output_lines = []
    for (start_time, end_time), days in sorted(grouped.items(), key=lambda item: min(item[1])):
        day_text = _compress_day_sequence(sorted(days))
        output_lines.append(f"{day_text} {start_time}-{end_time},")
        
    return "\n".join(output_lines)


@app.post("/api/aus-convert")
def aus_convert():
    payload = request.get_json(silent=True) or {}
    text = str(payload.get("text", "")).strip()
    if not text:
        return jsonify({"ok": False, "error": "Input is required"}), 400

    started = time.perf_counter()
    converted = convert_aus_notam(text)
    elapsed_ms = round((time.perf_counter() - started) * 1000.0, 2)

    return jsonify(
        {
            "ok": True,
            "result": converted,
            "latencyMs": elapsed_ms,
            "mode": "aus-day-logic",
        }
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)
