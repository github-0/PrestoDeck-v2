#!/bin/bash

set -e

if ! command -v python3 >/dev/null 2>&1; then
    echo "Python3 is not installed. Please install it first."
    exit 1
fi

echo "Creating virtual environment..."
python3 -m venv venv

source venv/bin/activate

echo "Upgrading pip..."
pip install --upgrade pip

echo "Installing required Python packages..."
pip install spotipy

python adhoc/generate_token.py