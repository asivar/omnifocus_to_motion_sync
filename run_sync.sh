#!/bin/bash

# 1. Set the working directory to where your script and JSON mapping live
cd "$(dirname "$0")" || exit 1

# 2. Load API key from .env file
if [ -f .env ]; then
    export "$(grep -v '^#' .env | grep MOTION_API_KEY | xargs)"
else
    echo "ERROR: .env file not found" >> sync_output.log
    exit 1
fi

# 3. Run the sync script using the venv python
./venv/bin/python sync_of_to_motion.py >> sync_output.log 2>&1
