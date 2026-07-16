"""
routes/v3.py
------------
Serves the built React app (frontend/dist, built with `npm run build` from
/frontend) at /v3/*. The React router handles client-side routes, so every
sub-path falls back to index.html except real static asset files.
"""
import os
from flask import Blueprint, send_from_directory, abort

v3_bp = Blueprint('v3', __name__)

_DIST_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    'frontend', 'dist'
)


@v3_bp.route('/')
@v3_bp.route('/<path:subpath>')
def serve(subpath=''):
    if not os.path.isdir(_DIST_DIR):
        abort(404, description="Frontend not built yet -- run `npm run build` in /frontend.")

    candidate = os.path.join(_DIST_DIR, subpath)
    if subpath and os.path.isfile(candidate):
        return send_from_directory(_DIST_DIR, subpath)

    # Client-side route (e.g. /v3/loans/apply) -- serve the SPA shell.
    return send_from_directory(_DIST_DIR, 'index.html')
