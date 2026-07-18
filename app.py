import os
import sys

# Make sure this file's own directory is on sys.path, so `core` is importable
# no matter what directory the shell was in when `python app.py` was run
# (e.g. running it from a parent folder, or from OneDrive syncing a path
# with spaces, can otherwise cause "ModuleNotFoundError: No module named 'core'").
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import create_app

app = create_app()

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
