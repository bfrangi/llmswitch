#!/bin/bash

# AI Hub Stop Script

echo "Stopping AI Hub..."

# Find and kill the proxy process
PID=$(pgrep -f "hub.proxy")

if [ -z "$PID" ]; then
    echo "No running AI Hub proxy found."
else
    kill $PID
    echo "Stopped process $PID"
fi

# Also kill the tunnel if it was started via this script (optional)
# This is tricky to do perfectly without more state management,
# but we can try to kill background SSH processes started from here.
# For now, we'll just leave it to the user or a more robust script.
