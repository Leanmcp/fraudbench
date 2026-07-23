#!/usr/bin/env bash
# Start the Result Visualizer web UI.
#   ./run_visualizer_server.sh                 # serve default leaderboard dir on :8765
#   ./run_visualizer_server.sh --port 9000     # different port
#   ./run_visualizer_server.sh /path/to/dir    # serve a different results dir
# Any args are forwarded to server.py. Open http://localhost:8765 when it's up.
set -euo pipefail
cd "$(dirname "$0")"
exec python3 server.py "$@"
