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


@dataclass(frozen=True)
class ClosureAreaQuery:
    """Represents a closure area query with distance/direction offset."""
    airway: str
    distance_km: float
    direction: str
    reference_waypoint: str
    fixed_boundary_raw: Optional[str]
    output_order: str  # condition-first | fixed-first | fixed-second
    legacy_compact_mode: bool
    legacy_compact_suffix: Optional[str]
    secondary_distance_km: Optional[float]
    secondary_direction: Optional[str]
    secondary_reference_waypoint: Optional[str]


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
        user_text = self._normalize_input_text(user_text)

        # First, try to detect if this is a closure area query
        closure_query = self._try_parse_closure_area_query(user_text)
        if closure_query:
            return self._resolve_closure_area(closure_query)
        
        # Otherwise, use standard resolver logic
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

    def _normalize_input_text(self, text: str) -> str:
        """Normalize user input while preserving semantic separators.

        Goals:
        - tolerate extra spaces/tabs/newlines,
        - tolerate unicode dash variants,
        - tolerate loose airway colon spacing.
        """
        if text is None:
            return ""

        t = text.strip()
        # Normalize common unicode dash variants to ASCII hyphen.
        t = re.sub(r"[\u2010\u2011\u2012\u2013\u2014\u2212]", "-", t)
        # Collapse all whitespace runs.
        t = re.sub(r"\s+", " ", t)

        # Normalize airway prefix spacing and optional colon:
        # "B215 : ..." -> "B215 ..."
        m = re.match(r"^\s*([A-Za-z]\d{1,3})\s*:?\s*(.*?)\s*$", t)
        if m and m.group(2):
            t = f"{m.group(1).upper()} {m.group(2).strip()}"

        # Trim spaces around boundary separators while keeping waypoint tokens intact.
        t = re.sub(r"\s*-\s*", "-", t)

        return t

    def _try_parse_closure_area_query(self, text: str) -> Optional[ClosureAreaQuery]:
        """Detect closure-area style queries.

        Supported deterministic forms:
        - AIRWAY FIX - 50KM EAST OF REF
        - AIRWAY 50KM EAST OF REF - FIX
        - AIRWAY 50KM EAST OF REF
        - AIRWAY 50KM EAST OF REF-FIX   (legacy compact suffix)
        """
        m_airway = re.match(r"^\s*([A-Za-z]\d{1,3})\s*:?[\s]*(.+?)\s*$", text)
        if not m_airway:
            return None

        airway = m_airway.group(1).upper()
        body = m_airway.group(2).strip().upper()

        # If the user visibly provides a boundary separator around '-', parse split mode first.
        # This prevents a fixed waypoint (e.g. '- HAM') from being swallowed as compact suffix.
        has_visible_split = (
            re.search(r"\s-\s|\s-|-\s", body) is not None
            or re.search(r"-\s*\d", body) is not None
        )
        if has_visible_split:
            split_match = re.match(r"^\s*(.*?)\s*-\s*(.*?)\s*$", body)
            if split_match:
                left = split_match.group(1).strip()
                right = split_match.group(2).strip()
                if left and right:
                    left_cond = self._parse_distance_condition(left)
                    right_cond = self._parse_distance_condition(right)

                    if left_cond and right_cond:
                        l_distance_km, l_direction, l_reference_wp, _l_legacy = left_cond
                        r_distance_km, r_direction, r_reference_wp, _r_legacy = right_cond
                        return ClosureAreaQuery(
                            airway=airway,
                            distance_km=l_distance_km,
                            direction=l_direction,
                            reference_waypoint=l_reference_wp,
                            fixed_boundary_raw=None,
                            output_order="condition-first",
                            legacy_compact_mode=False,
                            legacy_compact_suffix=None,
                            secondary_distance_km=r_distance_km,
                            secondary_direction=r_direction,
                            secondary_reference_waypoint=r_reference_wp,
                        )

                    if right_cond:
                        distance_km, direction, reference_wp, _legacy_suffix = right_cond
                        return ClosureAreaQuery(
                            airway=airway,
                            distance_km=distance_km,
                            direction=direction,
                            reference_waypoint=reference_wp,
                            fixed_boundary_raw=left,
                            output_order="fixed-first",
                            legacy_compact_mode=False,
                            legacy_compact_suffix=None,
                            secondary_distance_km=None,
                            secondary_direction=None,
                            secondary_reference_waypoint=None,
                        )

                    if left_cond:
                        distance_km, direction, reference_wp, _legacy_suffix = left_cond
                        return ClosureAreaQuery(
                            airway=airway,
                            distance_km=distance_km,
                            direction=direction,
                            reference_waypoint=reference_wp,
                            fixed_boundary_raw=right,
                            output_order="fixed-second",
                            legacy_compact_mode=False,
                            legacy_compact_suffix=None,
                            secondary_distance_km=None,
                            secondary_direction=None,
                            secondary_reference_waypoint=None,
                        )

        # Fallback: treat the full body as a compact distance condition.
        # Example: A343 80KM WEST OF NLT-POSOT
        whole_cond = self._parse_distance_condition(body)
        if whole_cond is not None:
            distance_km, direction, reference_wp, legacy_suffix = whole_cond
            if legacy_suffix is not None:
                # Compact form with suffix, e.g. "50KM WEST OF TEPUT-ADMUX".
                # Suffix is an explicit fixed boundary and must be preserved.
                return ClosureAreaQuery(
                    airway=airway,
                    distance_km=distance_km,
                    direction=direction,
                    reference_waypoint=reference_wp,
                    fixed_boundary_raw=legacy_suffix,
                    output_order="fixed-second",
                    legacy_compact_mode=False,
                    legacy_compact_suffix=None,
                    secondary_distance_km=None,
                    secondary_direction=None,
                    secondary_reference_waypoint=None,
                )

            return ClosureAreaQuery(
                airway=airway,
                distance_km=distance_km,
                direction=direction,
                reference_waypoint=reference_wp,
                fixed_boundary_raw=None,
                output_order="condition-first",
                legacy_compact_mode=legacy_suffix is not None,
                legacy_compact_suffix=legacy_suffix,
                secondary_distance_km=None,
                secondary_direction=None,
                secondary_reference_waypoint=None,
            )

        return None
    
    def _resolve_closure_area(self, closure_query: ClosureAreaQuery) -> str:
        """Resolve closure-area query with deterministic airway-side selection."""
        route_fixes = self._build_routes_from_pdfs().get(closure_query.airway)
        if not route_fixes:
            raise ResolverError(f"Airway not found: {closure_query.airway}")
        if len(route_fixes) < 2:
            raise ResolverError(f"Airway has insufficient fixes: {closure_query.airway}")

        waypoint_index: Dict[str, int] = {fix.ident: idx for idx, fix in enumerate(route_fixes)}

        ref_wp = self._normalize_waypoint_token(closure_query.reference_waypoint)
        if ref_wp not in waypoint_index:
            raise ResolverError(f"Reference waypoint not on airway {closure_query.airway}: {ref_wp}")

        ref_idx = waypoint_index[ref_wp]
        ref_fix = route_fixes[ref_idx]

        target_lat, target_lon = self._calculate_offset_coordinate(
            ref_fix.lat,
            ref_fix.lon,
            closure_query.distance_km,
            closure_query.direction,
        )

        condition_fix_ident, _condition_fix_idx, condition_pos = self._find_enclosing_fix_for_condition(
            fixes=route_fixes,
            reference_idx=ref_idx,
            target=(target_lat, target_lon),
            direction=closure_query.direction,
        )

        if closure_query.secondary_distance_km is not None:
            sec_ref_wp = self._normalize_waypoint_token(closure_query.secondary_reference_waypoint or "")
            if sec_ref_wp not in waypoint_index:
                raise ResolverError(f"Reference waypoint not on airway {closure_query.airway}: {sec_ref_wp}")

            sec_ref_idx = waypoint_index[sec_ref_wp]
            sec_ref_fix = route_fixes[sec_ref_idx]
            sec_target_lat, sec_target_lon = self._calculate_offset_coordinate(
                sec_ref_fix.lat,
                sec_ref_fix.lon,
                closure_query.secondary_distance_km,
                closure_query.secondary_direction or "",
            )
            sec_fix_ident, sec_fix_idx, sec_pos = self._find_enclosing_fix_for_condition(
                fixes=route_fixes,
                reference_idx=sec_ref_idx,
                target=(sec_target_lat, sec_target_lon),
                direction=closure_query.secondary_direction or "",
            )

            # Dual-distance mode: build the shortest waypoint segment enclosing both
            # projected condition coordinates on the airway.
            low_pos = min(condition_pos, sec_pos)
            high_pos = max(condition_pos, sec_pos)

            left_idx = int(math.floor(low_pos))
            right_idx = int(math.ceil(high_pos))

            left_idx = max(0, min(left_idx, len(route_fixes) - 1))
            right_idx = max(0, min(right_idx, len(route_fixes) - 1))

            if left_idx == right_idx:
                if right_idx + 1 < len(route_fixes):
                    right_idx += 1
                elif left_idx - 1 >= 0:
                    left_idx -= 1
                else:
                    raise ResolverError("ERROR: Unable to build a valid enclosing segment")

            left_fix = route_fixes[left_idx].ident
            right_fix = route_fixes[right_idx].ident

            # Preserve input-condition order in output while keeping same physical segment.
            if condition_pos >= sec_pos:
                return f"{closure_query.airway} {right_fix}-{left_fix}"
            return f"{closure_query.airway} {left_fix}-{right_fix}"

        # Legacy compact mode (e.g., 80KM ... OF NLT-POSOT) is treated conservatively:
        # step one fix further outward from the reference side to avoid under-enclosure.
        if (
            closure_query.legacy_compact_mode
            and closure_query.legacy_compact_suffix is not None
            and closure_query.legacy_compact_suffix != ref_wp
        ):
            if _condition_fix_idx > ref_idx and _condition_fix_idx + 1 < len(route_fixes):
                condition_fix_ident = route_fixes[_condition_fix_idx + 1].ident
                _condition_fix_idx = _condition_fix_idx + 1
            elif _condition_fix_idx < ref_idx and _condition_fix_idx - 1 >= 0:
                condition_fix_ident = route_fixes[_condition_fix_idx - 1].ident
                _condition_fix_idx = _condition_fix_idx - 1

        # Choose the second boundary deterministically.
        if closure_query.fixed_boundary_raw is None:
            # Condition-only mode: segment between calculated boundary and reference fix.
            fix_a = condition_fix_ident
            fix_b = ref_wp
        else:
            fixed_raw = closure_query.fixed_boundary_raw
            fixed_coord = self._parse_coord_pair_from_any(fixed_raw)
            if fixed_coord is not None:
                prev_fix, next_fix, pos = self._neighbor_fixes_for_coordinate(
                    route_fixes,
                    fixed_coord,
                    with_position=True,
                )
                condition_pos = float(_condition_fix_idx)
                if pos <= condition_pos:
                    fixed_ident = prev_fix.ident
                else:
                    fixed_ident = next_fix.ident
            else:
                fixed_ident = self._normalize_waypoint_token(fixed_raw)
                if fixed_ident not in waypoint_index:
                    raise ResolverError(f"Waypoint not on airway {closure_query.airway}: {fixed_ident}")

            if closure_query.output_order == "fixed-first":
                fix_a, fix_b = fixed_ident, condition_fix_ident
            else:  # fixed-second
                fix_a, fix_b = condition_fix_ident, fixed_ident

        if fix_a == fix_b:
            # If user provided a fixed boundary, preserve it and expand on the
            # condition side to the adjacent waypoint that encloses projection.
            if closure_query.fixed_boundary_raw is not None:
                fixed_idx = waypoint_index[fix_a]
                if condition_pos < float(fixed_idx) and fixed_idx - 1 >= 0:
                    neighbor = route_fixes[fixed_idx - 1].ident
                elif condition_pos >= float(fixed_idx) and fixed_idx + 1 < len(route_fixes):
                    neighbor = route_fixes[fixed_idx + 1].ident
                else:
                    raise ResolverError("ERROR: Unable to build a valid enclosing segment")

                if closure_query.output_order == "fixed-first":
                    fix_a, fix_b = fix_a, neighbor
                else:  # fixed-second
                    fix_a, fix_b = neighbor, fix_b
            else:
                raise ResolverError("ERROR: Unable to build a valid enclosing segment")

        return f"{closure_query.airway} {fix_a}-{fix_b}"

    def _parse_distance_condition(self, text: str) -> Optional[Tuple[float, str, str, Optional[str]]]:
        m = re.match(
            r"^\s*(\d+(?:\.\d+)?)\s*KM\s+"
            r"(NORTH|SOUTH|EAST|WEST|NE|NW|SE|SW|N|S|E|W)\s+OF\s+"
            r"([A-Z0-9]{2,6})(?:\s*-\s*([A-Z0-9]{2,6}))?\s*$",
            text,
            re.IGNORECASE,
        )
        if not m:
            return None
        return float(m.group(1)), m.group(2).upper(), m.group(3).upper(), (m.group(4).upper() if m.group(4) else None)

    def _find_enclosing_fix_for_condition(
        self,
        fixes: Sequence[Fix],
        reference_idx: int,
        target: Tuple[float, float],
        direction: str,
    ) -> Tuple[str, int, float]:
        """Find enclosing fix by selecting the airway side from the reference and snapping to nearest segment."""
        lat, lon = target

        # Pick travel side from reference using immediate neighbors.
        left_idx = reference_idx - 1 if reference_idx - 1 >= 0 else None
        right_idx = reference_idx + 1 if reference_idx + 1 < len(fixes) else None

        if left_idx is None and right_idx is None:
            raise ResolverError("Airway has insufficient fixes")

        if left_idx is None:
            side = +1
        elif right_idx is None:
            side = -1
        else:
            requested_bearing = self._direction_to_bearing(direction)
            ref_fix = fixes[reference_idx]
            left_fix = fixes[left_idx]
            right_fix = fixes[right_idx]

            left_bearing = self._initial_bearing_degrees(ref_fix.lat, ref_fix.lon, left_fix.lat, left_fix.lon)
            right_bearing = self._initial_bearing_degrees(ref_fix.lat, ref_fix.lon, right_fix.lat, right_fix.lon)

            left_delta = self._bearing_delta_deg(requested_bearing, left_bearing)
            right_delta = self._bearing_delta_deg(requested_bearing, right_bearing)
            side = -1 if left_delta <= right_delta else +1

        best_i = -1
        best_dist = float("inf")
        best_t_raw = 0.0

        if side > 0:
            seg_start = reference_idx
            seg_end = len(fixes) - 2
        else:
            seg_start = 0
            seg_end = reference_idx - 1

        if seg_start > seg_end:
            raise ResolverError("ERROR: Calculated point exceeds the end of the airway")

        for i in range(seg_start, seg_end + 1):
            dist_km, _t_clamped, t_raw = self._point_to_segment_metrics(
                (lat, lon),
                (fixes[i].lat, fixes[i].lon),
                (fixes[i + 1].lat, fixes[i + 1].lon),
            )
            if dist_km < best_dist:
                best_dist = dist_km
                best_i = i
                best_t_raw = t_raw

        if best_i < 0:
            raise ResolverError("ERROR: Calculated point exceeds the end of the airway")

        # End-of-airway exception when projection moves beyond terminal segment.
        if side > 0 and best_i == len(fixes) - 2 and best_t_raw > 1.0001:
            raise ResolverError("ERROR: Calculated point exceeds the end of the airway")
        if side < 0 and best_i == 0 and best_t_raw < -0.0001:
            raise ResolverError("ERROR: Calculated point exceeds the end of the airway")

        boundary_idx = best_i + 1 if side > 0 else best_i
        projected_pos = float(best_i) + max(0.0, min(1.0, best_t_raw))
        return fixes[boundary_idx].ident, boundary_idx, projected_pos

    @staticmethod
    def _direction_to_bearing(direction: str) -> float:
        direction_map = {
            "N": 0.0,
            "NORTH": 0.0,
            "NE": 45.0,
            "NORTHEAST": 45.0,
            "E": 90.0,
            "EAST": 90.0,
            "SE": 135.0,
            "SOUTHEAST": 135.0,
            "S": 180.0,
            "SOUTH": 180.0,
            "SW": 225.0,
            "SOUTHWEST": 225.0,
            "W": 270.0,
            "WEST": 270.0,
            "NW": 315.0,
            "NORTHWEST": 315.0,
        }
        d = direction.strip().upper()
        if d not in direction_map:
            raise ResolverError(f"Invalid direction: {direction}")
        return direction_map[d]

    @staticmethod
    def _initial_bearing_degrees(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        phi1 = math.radians(lat1)
        phi2 = math.radians(lat2)
        dlambda = math.radians(lon2 - lon1)
        y = math.sin(dlambda) * math.cos(phi2)
        x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlambda)
        theta = math.degrees(math.atan2(y, x))
        return (theta + 360.0) % 360.0

    @staticmethod
    def _bearing_delta_deg(a: float, b: float) -> float:
        d = abs(a - b) % 360.0
        return d if d <= 180.0 else 360.0 - d
    
    def _calculate_offset_coordinate(
        self, ref_lat: float, ref_lon: float,
        distance_km: float, direction: str
    ) -> Tuple[float, float]:
        """Calculate a new coordinate at a given distance and direction from a reference point."""
        # Normalize direction to bearing (degrees)
        direction_map = {
            "N": 0, "NORTH": 0,
            "NE": 45, "NORTHEAST": 45,
            "E": 90, "EAST": 90,
            "SE": 135, "SOUTHEAST": 135,
            "S": 180, "SOUTH": 180,
            "SW": 225, "SOUTHWEST": 225,
            "W": 270, "WEST": 270,
            "NW": 315, "NORTHWEST": 315
        }
        
        direction_upper = direction.upper()
        if direction_upper not in direction_map:
            raise ResolverError(f"Invalid direction: {direction}")
        
        bearing = direction_map[direction_upper]
        
        # Use haversine formula to move the coordinate
        return self._point_at_bearing_distance(ref_lat, ref_lon, bearing, distance_km)
    
    @staticmethod
    def _point_at_bearing_distance(
        lat1: float, lon1: float, bearing: float, distance_km: float
    ) -> Tuple[float, float]:
        """Calculate destination point given start point, bearing, and distance."""
        R = 6371.0  # Earth radius in km
        bearing_rad = math.radians(bearing)
        lat1_rad = math.radians(lat1)
        lon1_rad = math.radians(lon1)
        
        angular_distance = distance_km / R
        
        lat2_rad = math.asin(
            math.sin(lat1_rad) * math.cos(angular_distance) +
            math.cos(lat1_rad) * math.sin(angular_distance) * math.cos(bearing_rad)
        )
        
        lon2_rad = lon1_rad + math.atan2(
            math.sin(bearing_rad) * math.sin(angular_distance) * math.cos(lat1_rad),
            math.cos(angular_distance) - math.sin(lat1_rad) * math.sin(lat2_rad)
        )
        
        lat2 = math.degrees(lat2_rad)
        lon2 = math.degrees(lon2_rad)
        
        return lat2, lon2

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
    def _point_to_segment_metrics(
        p: Tuple[float, float],
        a: Tuple[float, float],
        b: Tuple[float, float],
    ) -> Tuple[float, float, float]:
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
            d = math.hypot(px - ax, py - ay)
            return d, 0.0, 0.0

        t_raw = (apx * abx + apy * aby) / ab2
        t_clamped = max(0.0, min(1.0, t_raw))
        cx = ax + t_clamped * abx
        cy = ay + t_clamped * aby
        dist = math.hypot(px - cx, py - cy)
        return dist, t_clamped, t_raw

    @staticmethod
    def _point_to_segment_km(p: Tuple[float, float], a: Tuple[float, float], b: Tuple[float, float]) -> float:
        dist, _, _ = ChinaAirwayResolver._point_to_segment_metrics(p, a, b)
        return dist


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
