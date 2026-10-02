#!/bin/bash

# AI Hub Start Script

# Check if pyproject.toml exists
if [ ! -f "pyproject.toml" ]; then
    echo "Error: pyproject.toml not found. Are you in the ai-hub directory?"
    exit 1
fi

echo "Starting AI Hub Gateway..."

# Run the proxy using the installed package
# (In development, you might want to use 'python -m hub.proxy')
python3 -m hub.proxy
