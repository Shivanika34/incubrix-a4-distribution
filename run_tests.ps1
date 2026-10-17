# Runs the complete test suite from Windows PowerShell.
$ErrorActionPreference = "Stop"
python -m pytest -q
