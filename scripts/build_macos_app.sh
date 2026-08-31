
#!/bin/zsh
set -euo pipefail

cd "$(dirname "$0")/.."

echo "CloudMover 0.19.0 macOS build"
echo "Cleaning previous build..."
rm -rf build dist

echo "Running tests..."
python3 -m pytest -q

echo "Building CloudMover.app..."
python3 -m PyInstaller --noconfirm --clean CloudMover.spec

echo
echo "Build complete:"
echo "  $(pwd)/dist/CloudMover.app"
echo
echo "Launch with:"
echo "  open dist/CloudMover.app"
