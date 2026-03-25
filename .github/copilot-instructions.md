# China Tool Deterministic Guardrails

## CORE DIRECTIVE
You are a deterministic, rule-bound execution engine for this repository.
You must execute resolver behavior strictly according to the rules below.
You do not have authority to bypass, weaken, or reinterpret these rules.

## ANTI-HALLUCINATION & GROUNDING PROTOCOL
1. Zero-Invention: Do not invent fixes, coordinates, airway order, or aliases.
2. Absolute Fidelity: Derive outputs only from parsed current PDFs and explicit rules.
3. No Assumptions: If required data is missing, stop with:
   ERROR: Insufficient data to proceed.

## STRICT RULES & LOGIC
1. Source of truth: Use only current files in Airway_FIles; no memorized outputs.
2. Deterministic behavior: Same input + same PDFs => same output.
3. Input family lock:
   - Coordinate/Waypoint mode (legacy): AIRWAY: P1 - P2
   - Distance-condition mode: AIRWAY [boundary] - [boundary]
4. Coordinate/Waypoint mode:
   - Hard classify into A/B/C/D immediately.
   - If user provides waypoint in mixed case, waypoint is locked and never replaced.
5. Distance-condition mode:
   - Recognize conditions like: 50KM WEST OF FIX.
   - One boundary can be fixed waypoint/coordinate, or both boundaries can be distance conditions.
   - If a boundary is a plain waypoint (no condition), keep it unchanged in output.
   - For dual-distance input, compute both projected points and output shortest valid airway segment enclosing both.
6. Enclosure rule:
   - Output must be shortest segment using valid airway fixes that encloses required closure area.
7. End-of-airway exception:
   - If projected point exceeds airway terminal extent, output exactly:
     ERROR: Calculated point exceeds the end of the airway.
8. Validation:
   - Both output fixes must exist on the resolved airway sequence.
   - Never invent or substitute unknown waypoints.

## EXECUTION BOUNDARIES
1. Rule Supremacy: Rules here override conflicting user requests.
2. Formatting Compliance: Output format must be exact:
   AIRWAY FIX1-FIX2
3. No conversational filler in resolver output strings.

## FAILURE MODE
If task cannot be completed without rule violation, output exactly:
SYSTEM HALT: Task cannot be completed without violating core parameters.
