"""
Automated collections escalation ladder -- staff-facing task list.

The ladder itself (which stages exist, when a loan crosses into one) lives
in core/collections.py and runs once a day via core/scheduler.py, right
alongside the existing send_overdue_reminders job. This module is just the
UI staff use to see what the ladder has generated and act on it: view open
tasks, assign/reassign them, and mark them resolved with a note.
"""
from flask import Blueprint, request, jsonify, render_template

from core.database import get_db, execute, utcnow
from core.auth import login_required, role_required, permission_required
from core.collections import collections_ladder_public
from core.serializers import loan_public
from core.utils import log_audit

collections_bp = Blueprint('collections', __name__)


def _collection_task_public(row, loan_row=None):
    d = dict(row)
    out = {
        'id': d['id'],
        'loan_id': d['loan_id'],
        'stage': d['stage'],
        'days_overdue_at_creation': d['days_overdue_at_creation'],
        'outstanding_at_creation': d['outstanding_at_creation'],
        'status': d['status'],
        'assigned_to': d.get('assigned_to'),
        'assigned_to_name': d.get('assigned_to_name'),
        'notes': d.get('notes'),
        'resolved_by': d.get('resolved_by'),
        'resolved_by_name': d.get('resolved_by_name'),
        'resolved_at': d.get('resolved_at'),
        'created_at': d['created_at'],
        'updated_at': d['updated_at'],
    }
    if loan_row:
        out['loan'] = loan_public(loan_row)
    return out


_TASK_SELECT = """
    SELECT ct.*, u1.full_name AS assigned_to_name, u2.full_name AS resolved_by_name
    FROM collection_tasks ct
    LEFT JOIN users u1 ON u1.id = ct.assigned_to
    LEFT JOIN users u2 ON u2.id = ct.resolved_by
"""


@collections_bp.route('/')
@login_required
def index():
    from core.auth import get_current_user
    return render_template('collections/index.html', user=get_current_user(), ladder=collections_ladder_public())


@collections_bp.route('/api', methods=['GET'])
@login_required
def list_tasks():
    """?status=open|resolved|all (default open), ?stage=<stage key>,
    ?assigned_to=<user id>, ?mine=1 (tasks assigned to the current user)."""
    from core.auth import get_current_user
    user = get_current_user()

    status = request.args.get('status', 'open')
    stage = request.args.get('stage')
    assigned_to = request.args.get('assigned_to')
    mine = request.args.get('mine') == '1'

    sql = _TASK_SELECT + " WHERE 1=1"
    params = []
    if status != 'all':
        sql += " AND ct.status = %s"
        params.append(status)
    if stage:
        sql += " AND ct.stage = %s"
        params.append(stage)
    if mine:
        sql += " AND ct.assigned_to = %s"
        params.append(user['id'])
    elif assigned_to:
        sql += " AND ct.assigned_to = %s"
        params.append(int(assigned_to))
    sql += " ORDER BY ct.days_overdue_at_creation DESC, ct.created_at DESC"

    rows = get_db().execute(sql, tuple(params)).fetchall()

    loans_by_id = {}
    if rows:
        loan_ids = tuple({r['loan_id'] for r in rows})
        placeholders = ','.join(['%s'] * len(loan_ids))
        from core.routes.loans import _borrower_name_sql
        loan_rows = get_db().execute(
            _borrower_name_sql() + f" WHERE loans.id IN ({placeholders})", loan_ids
        ).fetchall()
        loans_by_id = {r['id']: r for r in loan_rows}

    return jsonify({
        'tasks': [_collection_task_public(r, loans_by_id.get(r['loan_id'])) for r in rows],
        'ladder': collections_ladder_public(),
    })


@collections_bp.route('/api/<int:task_id>', methods=['PATCH'])
@login_required
@role_required('admin', 'loan_officer')
@permission_required('collections.update')
def update_task(task_id):
    """Assign/reassign a task, or resolve it with a note. Body:
    {assigned_to?, notes?, status?: 'open'|'resolved'}"""
    from core.auth import get_current_user
    user = get_current_user()

    task = get_db().execute("SELECT * FROM collection_tasks WHERE id = %s", (task_id,)).fetchone()
    if not task:
        return jsonify({'error': 'Task not found'}), 404

    data = request.get_json() or {}
    fields, params = [], []

    if 'assigned_to' in data:
        fields.append("assigned_to = %s")
        params.append(data['assigned_to'])
    if 'notes' in data:
        fields.append("notes = %s")
        params.append((data.get('notes') or '').strip() or None)
    if data.get('status') == 'resolved' and task['status'] != 'resolved':
        fields += ["status = %s", "resolved_by = %s", "resolved_at = %s"]
        params += ['resolved', user['id'], utcnow()]
    elif data.get('status') == 'open' and task['status'] != 'open':
        fields += ["status = %s", "resolved_by = %s", "resolved_at = %s"]
        params += ['open', None, None]

    if not fields:
        return jsonify({'error': 'Nothing to update'}), 400

    fields.append("updated_at = %s")
    params.append(utcnow())
    params.append(task_id)

    execute(f"UPDATE collection_tasks SET {', '.join(fields)} WHERE id = %s", tuple(params))
    log_audit('COLLECTION_TASK_UPDATED', 'loan', task['loan_id'],
              old_values={'task_id': task_id, 'status': task['status']}, new_values=data)

    updated = get_db().execute(_TASK_SELECT + " WHERE ct.id = %s", (task_id,)).fetchone()
    return jsonify({'message': 'Task updated', 'task': _collection_task_public(updated)})


@collections_bp.route('/api/assignable-staff', methods=['GET'])
@login_required
def assignable_staff():
    """Active staff usable in the assignment dropdown -- deliberately not
    gated to admin (unlike /users/api) since loan officers need to assign
    tasks to each other too, and this only exposes id/name, nothing
    sensitive."""
    rows = get_db().execute(
        "SELECT id, full_name FROM users WHERE is_active = 1 ORDER BY full_name"
    ).fetchall()
    return jsonify({'staff': [{'id': r['id'], 'full_name': r['full_name']} for r in rows]})


@collections_bp.route('/api/run', methods=['POST'])
@login_required
@role_required('admin')
@permission_required('collections.run')
def run_now():
    """Manually trigger the escalation check (normally runs once daily via
    core/scheduler.py) -- e.g. right after making this change, so you don't
    have to wait until tomorrow's scheduled run to see tasks appear."""
    from core.collections import run_collections_escalation
    result = run_collections_escalation()
    return jsonify(result)
