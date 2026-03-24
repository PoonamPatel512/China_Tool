#!/usr/bin/env python3

from __future__ import annotations

import argparse
import math
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from pypdf import PdfReader


ROUTE_RE = re.compile(r"^[ABGVW]\d{1,3}$")
MARKERS = ("▲", "△", "▴", "▵")


@dataclass(frozen=True)
class Fix:
    ident: str
    lat: float
    lon: float
    route: str
    source_pdf: str
    page: int


@dataclass(frozen=True)
class ParsedInput:
    airway: str
    p1_raw: str
    p2_raw: str


class ResolverError(Exception):
    pass


class ChinaAirwayResolver:
    """
    Deterministic airway segment resolver.

    Design guarantees:
    - Parses the current PDFs for every request (no fixed answer memory).
    - Route sequence is built from official order as it appears in PDFs.
    - Input case path is locked after classification.
    """

    _cached_signatures: Optional[Tuple[Tuple[str, int, int], ...]] = None
    _cached_routes: Optional[Dict[str, List[Fix]]] = None

    def __init__(self, airway_dir: Path) -> None:
        self.airway_dir = airway_dir
        self.last_data_refresh = "unknown"

    def resolve(self, user_text: str) -> str:
        parsed = self._parse_user_input(user_text)
        route_fixes = self._build_routes_from_pdfs().get(parsed.airway)
        if not route_fixes:
            raise ResolverError(f"Airway not found: {parsed.airway}")
        if len(route_fixes) < 2:
            raise ResolverError(f"Airway has insufficient fixes: {parsed.airway}")

        p1_coord = self._parse_coord_pair_from_any(parsed.p1_raw)
        p2_coord = self._parse_coord_pair_from_any(parsed.p2_raw)

        p1_is_coord = p1_coord is not None
        p2_is_coord = p2_coord is not None

        # STEP 1 — hard input classification
        if p1_is_coord and p2_is_coord:
            case = "A"
        elif p1_is_coord and not p2_is_coord:
            case = "B"
        elif (not p1_is_coord) and p2_is_coord:
            case = "C"
        else:
            case = "D"

        waypoint_index: Dict[str, int] = {fix.ident: idx for idx, fix in enumerate(route_fixes)}

        fix1: str
        fix2: str
        provided_waypoints: List[str] = []

        if case == "A":
            p1_prev, _ = self._neighbor_fixes_for_coordinate(route_fixes, p1_coord)
            _, p2_next = self._neighbor_fixes_for_coordinate(route_fixes, p2_coord)
            fix1, fix2 = p1_prev.ident, p2_next.ident

        elif case == "B":
            provided_wp = self._normalize_waypoint_token(parsed.p2_raw)
            if provided_wp not in waypoint_index:
                raise ResolverError(f"Waypoint not on airway {parsed.airway}: {provided_wp}")
            provided_waypoints = [provided_wp]

            coord_prev, coord_next, coord_pos = self._neighbor_fixes_for_coordinate(route_fixes, p1_coord, with_position=True)
            wp_pos = float(waypoint_index[provided_wp])

            if coord_pos < wp_pos:
                fix1, fix2 = coord_prev.ident, provided_wp
            else:
                fix1, fix2 = provided_wp, coord_next.ident

        elif case == "C":
            provided_wp = self._normalize_waypoint_token(parsed.p1_raw)
            if provided_wp not in waypoint_index:
                raise ResolverError(f"Waypoint not on airway {parsed.airway}: {provided_wp}")
            provided_waypoints = [provided_wp]

            coord_prev, coord_next, coord_pos = self._neighbor_fixes_for_coordinate(route_fixes, p2_coord, with_position=True)
            wp_pos = float(waypoint_index[provided_wp])

            if coord_pos < wp_pos:
                fix1, fix2 = coord_prev.ident, provided_wp
            else:
                fix1, fix2 = provided_wp, coord_next.ident

        else:  # case D
            wp1 = self._normalize_waypoint_token(parsed.p1_raw)
            wp2 = self._normalize_waypoint_token(parsed.p2_raw)
            if wp1 not in waypoint_index:
                raise ResolverError(f"Waypoint not on airway {parsed.airway}: {wp1}")
            if wp2 not in waypoint_index:
                raise ResolverError(f"Waypoint not on airway {parsed.airway}: {wp2}")
            provided_waypoints = [wp1, wp2]
            fix1, fix2 = wp1, wp2

        # FINAL OUTPUT GUARD
        for wp in provided_waypoints:
            if wp not in (fix1, fix2):
                raise ResolverError("Provided waypoint changed unexpectedly; locked-case guard violated")

        return f"{parsed.airway} {fix1}-{fix2}"

    def _build_routes_from_pdfs(self) -> Dict[str, List[Fix]]:
        pdfs = sorted(self.airway_dir.glob("*.pdf"))
        if not pdfs:
            raise ResolverError(f"No PDF files found in {self.airway_dir}")

        signatures: Tuple[Tuple[str, int, int], ...] = tuple(
            (pdf.name, int(pdf.stat().st_mtime_ns), int(pdf.stat().st_size)) for pdf in pdfs
        )

        if (
            ChinaAirwayResolver._cached_signatures == signatures
            and ChinaAirwayResolver._cached_routes is not None
        ):
            self.last_data_refresh = "cache-hit"
            return ChinaAirwayResolver._cached_routes

        routes: Dict[str, List[Fix]] = {}
        current_route: Optional[str] = None

        for pdf in pdfs:
            reader = PdfReader(str(pdf))
            for page_num, page in enumerate(reader.pages, start=1):
                text = page.extract_text() or ""
                for raw_line in text.splitlines():
                    line = raw_line.strip()
                    if not line:
                        continue

                    if ROUTE_RE.fullmatch(line):
                        current_route = line
                        routes.setdefault(current_route, [])
                        continue

                    if current_route is None:
                        continue

                    fix = self._parse_fix_line(line, current_route, pdf.name, page_num)
                    if not fix:
                        continue

                    bucket = routes[current_route]
                    if bucket and bucket[-1].ident == fix.ident and self._haversine_km(
                        bucket[-1].lat,
                        bucket[-1].lon,
                        fix.lat,
                        fix.lon,
                    ) < 0.5:
                        continue
                    bucket.append(fix)

        # Keep only routes with usable fix chains.
        filtered = {k: v for k, v in routes.items() if len(v) >= 2}
        ChinaAirwayResolver._cached_signatures = signatures
        ChinaAirwayResolver._cached_routes = filtered
        self.last_data_refresh = "rebuilt-from-pdfs"
        return filtered

    def _parse_fix_line(self, line: str, route: str, source_pdf: str, page: int) -> Optional[Fix]:
        if not line.startswith(MARKERS):
            return None

        coord = self._parse_coord_pair_from_any(line)
        if coord is None:
            return None

        ident = self._extract_fix_ident(line)
        if not ident:
            return None

        lat, lon = coord
        return Fix(ident=ident, lat=lat, lon=lon, route=route, source_pdf=source_pdf, page=page)

    def _extract_fix_ident(self, line: str) -> Optional[str]:
        coord_match = self._find_coord_span(line)
        if not coord_match:
            return None
        prefix = line[: coord_match[0]]

        paren = re.findall(r"\(([A-Z0-9]{2,6})\)", prefix.upper())
        if paren:
            return paren[-1]

        # OCR can split identifiers like "A TNOK"; merge 1-letter + token.
        tokens = re.findall(r"[A-Z]{1,6}", prefix.upper())
        stopwords = {
            "VOR",
            "DME",
            "NDB",
            "VORTAC",
            "TACAN",
            "ACC",
            "APP",
            "FIR",
            "ATC",
            "RNA",
            "RNP",
            "ENR",
        }
        tokens = [t for t in tokens if t not in stopwords]
        if not tokens:
            return None

        if len(tokens) >= 2 and len(tokens[0]) == 1 and 2 <= len(tokens[1]) <= 5:
            merged = tokens[0] + tokens[1]
            if 3 <= len(merged) <= 6:
                return merged

        for token in tokens:
            if 2 <= len(token) <= 6:
                return token

        return tokens[0]

    def _parse_user_input(self, text: str) -> ParsedInput:
        m = re.match(r"^\s*([A-Za-z]\d{1,3})\s*:?\s*(.*?)\s*-\s*(.*?)\s*$", text)
        if not m:
            raise ResolverError("Input format must be AIRWAY: P1 - P2")
        airway = m.group(1).upper()
        p1 = m.group(2).strip().upper()
        p2 = m.group(3).strip().upper()
        if not p1 or not p2:
            raise ResolverError("Both P1 and P2 are required")
        return ParsedInput(airway=airway, p1_raw=p1, p2_raw=p2)

    def _normalize_waypoint_token(self, token: str) -> str:
        return re.sub(r"\s+", "", token.strip().upper())

    def _neighbor_fixes_for_coordinate(
        self,
        fixes: Sequence[Fix],
        coord: Optional[Tuple[float, float]],
        with_position: bool = False,
    ):
        if coord is None:
            raise ResolverError("Coordinate value is missing")

        lat, lon = coord

        # Exact match to a published fix coordinate.
        for idx, fix in enumerate(fixes):
            if self._haversine_km(lat, lon, fix.lat, fix.lon) <= 2.0:
                if idx - 1 < 0 or idx + 1 >= len(fixes):
                    raise ResolverError("Coordinate resolves to route boundary; neighbor fix is unavailable")
                prev_fix = fixes[idx - 1]
                next_fix = fixes[idx + 1]
                if with_position:
                    return prev_fix, next_fix, float(idx)
                return prev_fix, next_fix

        best_idx = -1
        best_dist = float("inf")

        for i in range(len(fixes) - 1):
            d = self._point_to_segment_km((lat, lon), (fixes[i].lat, fixes[i].lon), (fixes[i + 1].lat, fixes[i + 1].lon))
            if d < best_dist:
                best_dist = d
                best_idx = i

        if best_idx < 0 or best_dist > 80.0:
            raise ResolverError("Coordinate is not on the specified airway")

        prev_fix = fixes[best_idx]
        next_fix = fixes[best_idx + 1]
        pos = best_idx + 0.5

        if with_position:
            return prev_fix, next_fix, pos
        return prev_fix, next_fix

    def _parse_coord_pair_from_any(self, text: str) -> Optional[Tuple[float, float]]:
        text_u = text.upper()

        # Compact format, e.g. N373914E1011858
        m_compact = re.search(r"([NS]\d{6,7}(?:\.\d+)?)[\s,/-]*([EW]\d{7,8}(?:\.\d+)?)", text_u)
        if m_compact:
            lat = self._parse_compact_angle(m_compact.group(1), is_lat=True)
            lon = self._parse_compact_angle(m_compact.group(2), is_lat=False)
            return lat, lon

        lat_m = re.search(r"[NS]\s*\d{1,2}\s*°\s*\d{1,2}(?:\.\d+)?(?:\s*[′']\s*\d{1,2}(?:\.\d+)?)?\s*[″\"]?", text_u)
        lon_m = re.search(r"[EW]\s*\d{1,3}\s*°\s*\d{1,2}(?:\.\d+)?(?:\s*[′']\s*\d{1,2}(?:\.\d+)?)?\s*[″\"]?", text_u)
        if lat_m and lon_m and lat_m.start() < lon_m.start():
            lat = self._parse_symbol_angle(lat_m.group(0), is_lat=True)
            lon = self._parse_symbol_angle(lon_m.group(0), is_lat=False)
            return lat, lon

        return None

    def _find_coord_span(self, text: str) -> Optional[Tuple[int, int]]:
        text_u = text.upper()
        m_compact = re.search(r"([NS]\d{6,7}(?:\.\d+)?)[\s,/-]*([EW]\d{7,8}(?:\.\d+)?)", text_u)
        if m_compact:
            return m_compact.span()
        lat_m = re.search(r"[NS]\s*\d{1,2}\s*°\s*\d{1,2}(?:\.\d+)?(?:\s*[′']\s*\d{1,2}(?:\.\d+)?)?\s*[″\"]?", text_u)
        lon_m = re.search(r"[EW]\s*\d{1,3}\s*°\s*\d{1,2}(?:\.\d+)?(?:\s*[′']\s*\d{1,2}(?:\.\d+)?)?\s*[″\"]?", text_u)
        if lat_m and lon_m:
            return lat_m.start(), lon_m.end()
        return None

    def _parse_compact_angle(self, token: str, is_lat: bool) -> float:
        token = token.strip().upper()
        hemi = token[0]
        digits = token[1:]
        if is_lat:
            if len(digits) < 6:
                raise ResolverError(f"Invalid latitude coordinate: {token}")
            deg = int(digits[0:2])
            mins = int(digits[2:4])
            secs = float(digits[4:])
        else:
            if len(digits) < 7:
                raise ResolverError(f"Invalid longitude coordinate: {token}")
            deg = int(digits[0:3])
            mins = int(digits[3:5])
            secs = float(digits[5:])

        val = deg + (mins / 60.0) + (secs / 3600.0)
        if hemi in ("S", "W"):
            val = -val
        return val

    def _parse_symbol_angle(self, token: str, is_lat: bool) -> float:
        t = token.upper().replace(" ", "")
        m = re.match(r"^([NSEW])(\d{1,3})°(\d{1,2}(?:\.\d+)?)(?:[′'](\d{1,2}(?:\.\d+)?))?[″\"]?$", t)
        if not m:
            raise ResolverError(f"Invalid coordinate token: {token}")

        hemi = m.group(1)
        deg = int(m.group(2))
        mins = float(m.group(3))
        secs = float(m.group(4)) if m.group(4) else 0.0

        if is_lat and deg > 90:
            raise ResolverError(f"Invalid latitude degrees: {token}")
        if not is_lat and deg > 180:
            raise ResolverError(f"Invalid longitude degrees: {token}")

        val = deg + (mins / 60.0) + (secs / 3600.0)
        if hemi in ("S", "W"):
            val = -val
        return val

    @staticmethod
    def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        r = 6371.0
        p1, p2 = math.radians(lat1), math.radians(lat2)
        dphi = math.radians(lat2 - lat1)
        dlambda = math.radians(lon2 - lon1)
        a = math.sin(dphi / 2.0) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2.0) ** 2
        return 2.0 * r * math.asin(math.sqrt(a))

    @staticmethod
    def _point_to_segment_km(p: Tuple[float, float], a: Tuple[float, float], b: Tuple[float, float]) -> float:
        # Local projection is sufficient for segment selection across nearby fixes.
        lat0 = math.radians((a[0] + b[0] + p[0]) / 3.0)

        def to_xy(lat: float, lon: float) -> Tuple[float, float]:
            x = lon * 111.320 * math.cos(lat0)
            y = lat * 110.574
            return x, y

        px, py = to_xy(*p)
        ax, ay = to_xy(*a)
        bx, by = to_xy(*b)

        abx, aby = bx - ax, by - ay
        apx, apy = px - ax, py - ay
        ab2 = abx * abx + aby * aby
        if ab2 == 0.0:
            return math.hypot(px - ax, py - ay)

        t = (apx * abx + apy * aby) / ab2
        t_clamped = max(0.0, min(1.0, t))
        cx = ax + t_clamped * abx
        cy = ay + t_clamped * aby
        return math.hypot(px - cx, py - cy)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="China Airway Segment Resolver")
    parser.add_argument(
        "query",
        nargs="?",
        help="Input in format AIRWAY: P1 - P2, e.g. B215: N373914E1011858 - N381302E1000042",
    )
    parser.add_argument(
        "--airway-dir",
        default="Airway_FIles",
        help="Directory containing airway PDFs (default: Airway_FIles)",
    )
    args = parser.parse_args(argv)

    query = args.query
    if not query:
        query = input("Enter query (AIRWAY: P1 - P2): ").strip()

    resolver = ChinaAirwayResolver(Path(args.airway_dir))

    try:
        result = resolver.resolve(query)
    except ResolverError as exc:
        print(f"ERROR: {exc}")
        return 1

    print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
