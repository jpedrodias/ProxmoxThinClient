#!/bin/bash
cd "$(dirname "$0")"

source venv/bin/activate

python vdiclient_gui.py

deactivate