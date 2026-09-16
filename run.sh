#!/bin/bash
# Launches WaveStack using its own virtual environment, regardless of
# what directory this script is invoked from (important for .desktop
# launchers, which do not source your shell profile or activate venvs).
cd "$(dirname "$0")"
exec ./venv/bin/python3 wavestack.py "$@"
