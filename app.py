import os
import sys

# Make sure this file's own directory is on sys.path, so `core` is importable
# no matter what directory the shell was in when `python app.py` was run
# (e.g. running it from a parent folder, or from OneDrive syncing a path
# with spaces, can otherwise cause "ModuleNotFoundError: No module named 'core'").
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import create_app
from core.scheduler import start_scheduler

app = create_app()

# Start the daily overdue-reminder job once per running process.
#
# - Production (gunicorn, this module imported with app.debug False): start
#   immediately. gunicorn.conf.py sets preload_app=True, so this module
#   (and this line) only runs once in the master before forking workers --
#   not once per worker -- which is exactly what we want for a single
#   scheduled job.
# - Local dev (`python app.py`, debug=True): Werkzeug's reloader re-executes
#   this module in a child process and sets WERKZEUG_RUN_MAIN=true only in
#   that child, not in the parent launcher. Gating on that avoids starting
#   two competing schedulers (one in each process) when the reloader is on.
if not app.debug or os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
    start_scheduler(app)

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
