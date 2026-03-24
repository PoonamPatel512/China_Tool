# China_Tool

Deterministic China airway segment resolver that reads official airway PDFs and returns:

AIRWAY FIX1-FIX2

## Key Guarantees

- Fresh calculation every request from current PDFs in Airway_FIles.
- No memorized outputs and no fixed answer table.
- Strict case-locked resolution logic:
	- Coordinate-Coordinate
	- Coordinate-Waypoint
	- Waypoint-Coordinate
	- Waypoint-Waypoint
- Provided waypoint is never replaced in mixed cases.
- Route continuation across PDF pages is parsed as a single ordered airway chain.

## Install

python -m pip install -r requirements.txt

## Run

python china_airway_resolver.py "B215: N373914E1011858 - N381302E1000042"

or interactive mode:

python china_airway_resolver.py

## Web UI

Start server:

python web_app.py

Open in browser:

http://localhost:8000

UI supports:

- Manual typing
- Clipboard paste button
- Ctrl+Enter or Cmd+Enter to resolve
- One-click copy of output
- Mobile-friendly responsive layout

The backend computes every query freshly from current PDFs in Airway_FIles.
For smooth UI performance, parsed airway chains are cached in memory only while PDF file signatures remain unchanged. Any monthly PDF update invalidates cache automatically and rebuilds from PDFs.

## Input Format

AIRWAY: P1 - P2

Examples:

- B215: N373914E1011858 - N381302E1000042
- W191: N381039E0992753 - NODID

## Output Format

AIRWAY FIX1-FIX2

No colon, uppercase, no extra spaces.