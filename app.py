"""
api/app.py
Flask web application — REST API + file upload + chat endpoint.
Run with: python -m api.app
"""
import os
import json
import uuid
import logging
from datetime import datetime
from pathlib import Path

from flask import Flask, request, jsonify, Response, stream_with_context
from werkzeug.utils import secure_filename
from flask_cors import CORS

# Load .env if present
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Add parent to path so core imports work
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.models import ProjectContext, IntakeState
from core.chat_agent import process_message

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)
app.config['MAX_CONTENT_LENGTH'] = 512 * 1024 * 1024   # 512 MB

# ── Upload folder — files saved here persist for M query file-path connections
UPLOAD_ROOT = Path(__file__).parent.parent / "uploads"
UPLOAD_ROOT.mkdir(exist_ok=True)

# In-memory session store (replace with Redis in production)
SESSIONS: dict[str, ProjectContext] = {}

ALLOWED_EXTENSIONS = {
    'csv', 'xlsx', 'xls', 'txt', 'md', 'pdf', 'docx', 'json'
}

def allowed_file(filename: str) -> bool:
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def get_session_upload_dir(session_id: str) -> Path:
    """Return (and create) the upload folder for this session."""
    d = UPLOAD_ROOT / session_id
    d.mkdir(exist_ok=True)
    return d


# ── Session management ─────────────────────────────────────────────────────

@app.route('/api/session', methods=['POST'])
def create_session():
    """Create a new project session. Returns session_id."""
    session_id = str(uuid.uuid4())
    SESSIONS[session_id] = ProjectContext()
    logger.info("Session created: %s", session_id)
    return jsonify({
        'session_id': session_id,
        'state': IntakeState.PROJECT_INIT.value,
        'message': 'Session created. Send your first message to begin.'
    })


@app.route('/api/session/<session_id>', methods=['GET'])
def get_session(session_id: str):
    """Get current session state summary."""
    ctx = SESSIONS.get(session_id)
    if not ctx:
        return jsonify({'error': 'Session not found'}), 404
    return jsonify(_ctx_summary(ctx))


@app.route('/api/session/<session_id>', methods=['DELETE'])
def delete_session(session_id: str):
    """Delete a session."""
    SESSIONS.pop(session_id, None)
    return jsonify({'deleted': session_id})


def _ctx_summary(ctx: ProjectContext) -> dict:
    return {
        'project_id':    ctx.project_id,
        'project_name':  ctx.project_name,
        'state':         ctx.intake_state.value,
        'raw_files':     [f.file_name for f in ctx.raw_files],
        'mapping_files': [f.file_name for f in ctx.mapping_files],
        'protocol':      ctx.protocol_file_name,
        'measures_count': len(ctx.measures),
        'mappings_count': len(ctx.mapping_decisions),
        'errors_count':   len(ctx.errors),
        'errors_pending': len(ctx.pending_errors()),
        'model_built':    ctx.semantic_model is not None,
        'last_updated':   str(ctx.last_updated),
    }


# ── Chat endpoint ──────────────────────────────────────────────────────────

@app.route('/api/chat/<session_id>', methods=['POST'])
def chat(session_id: str):
    """
    Main chat endpoint.
    Accepts JSON {message: str} or multipart/form-data with optional file.
    Returns JSON {reply: str, state: str, context: {...}}.
    """
    ctx = SESSIONS.get(session_id)
    if not ctx:
        return jsonify({'error': 'Session not found'}), 404

    # Parse message
    attached_file = None

    if request.content_type and 'multipart' in request.content_type:
        message = request.form.get('message', '')
        if 'file' in request.files:
            f = request.files['file']
            if f and f.filename and allowed_file(f.filename):
                fname   = secure_filename(f.filename)
                content = f.read()

                # Save to disk so M queries can reference the file by path
                upload_dir  = get_session_upload_dir(session_id)
                saved_path  = upload_dir / fname
                saved_path.write_bytes(content)
                logger.info("File saved: %s (%d bytes) → %s",
                            fname, len(content), saved_path)

                # Pass both content (for profiling) and saved path (for BIM)
                attached_file = (fname, content, str(saved_path))
            elif f and f.filename:
                return jsonify({
                    'error': f"File type not allowed: {f.filename}. "
                             f"Accepted: {', '.join(ALLOWED_EXTENSIONS)}"
                }), 400
    else:
        data = request.get_json(silent=True) or {}
        message = data.get('message', '')

    if not message and not attached_file:
        return jsonify({'error': 'No message or file provided'}), 400

    try:
        reply, updated_ctx = process_message(ctx, message, attached_file)
        SESSIONS[session_id] = updated_ctx

        response = {
            'reply':   reply,
            'state':   updated_ctx.intake_state.value,
            'context': _ctx_summary(updated_ctx),
        }

        # If exports are ready, include them
        if updated_ctx.excel_bytes:
            response['exports_available'] = ['excel']
        if updated_ctx.tmdl_json:
            response['exports_available'] = response.get('exports_available', []) + ['tmdl']
        if updated_ctx.pbi_zip:
            response['exports_available'] = response.get('exports_available', []) + ['powerbi']

        return jsonify(response)

    except Exception as exc:
        logger.exception("Error processing message in session %s", session_id)
        return jsonify({
            'error': f'Processing error: {exc}',
            'state': ctx.intake_state.value,
        }), 500


# ── File download endpoints ────────────────────────────────────────────────

@app.route('/api/export/<session_id>/excel', methods=['GET'])
def download_excel(session_id: str):
    """Download the generated Excel workbook."""
    ctx = SESSIONS.get(session_id)
    if not ctx:
        return jsonify({'error': 'Session not found'}), 404
    xlsx = ctx.excel_bytes
    if not xlsx:
        return jsonify({'error': 'Excel export not yet generated'}), 404

    name = f"{ctx.project_name.replace(' ', '_')}_Model.xlsx"
    return Response(
        xlsx,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        headers={'Content-Disposition': f'attachment; filename="{name}"'}
    )


@app.route('/api/export/<session_id>/tmdl', methods=['GET'])
def download_tmdl(session_id: str):
    """Download the TMDL JSON."""
    ctx = SESSIONS.get(session_id)
    if not ctx:
        return jsonify({'error': 'Session not found'}), 404
    tmdl = ctx.tmdl_json
    if not tmdl:
        return jsonify({'error': 'TMDL export not yet generated'}), 404

    name = f"{ctx.project_name.replace(' ', '_')}_Model.tmdl.json"
    return Response(
        tmdl,
        mimetype='application/json',
        headers={'Content-Disposition': f'attachment; filename="{name}"'}
    )


@app.route('/api/export/<session_id>/audit', methods=['GET'])
def download_audit(session_id: str):
    """Download the error resolution audit log."""
    ctx = SESSIONS.get(session_id)
    if not ctx:
        return jsonify({'error': 'Session not found'}), 404
    audit = ctx.audit_json
    if not audit:
        return jsonify({'error': 'Audit log not yet generated'}), 404

    name = f"{ctx.project_name.replace(' ', '_')}_AuditLog.json"
    return Response(
        audit,
        mimetype='application/json',
        headers={'Content-Disposition': f'attachment; filename="{name}"'}
    )


@app.route('/api/export/<session_id>/report', methods=['GET'])
def download_report(session_id: str):
    """Generate and download the PDF completion report."""
    ctx = SESSIONS.get(session_id)
    if not ctx:
        return jsonify({'error': 'Session not found'}), 404
    if not ctx.semantic_model:
        return jsonify({'error': 'Model not yet built'}), 404

    try:
        from core.report_generator import generate_completion_report
        pdf_bytes = generate_completion_report(ctx)
        name = f"{ctx.project_name.replace(' ', '_')}_CompletionReport.pdf"
        return Response(
            pdf_bytes,
            mimetype='application/pdf',
            headers={'Content-Disposition': f'attachment; filename="{name}"'}
        )
    except Exception as exc:
        logger.exception("Report generation failed")
        return jsonify({'error': f'Report generation failed: {exc}'}), 500


@app.route('/api/export/<session_id>/powerbi', methods=['GET'])
def download_powerbi(session_id: str):
    """
    Download the Power BI export package (ZIP).
    Contains:
      model.bim        — BIM semantic model for Power BI Desktop / Tabular Editor
      report.json      — Report layout with auto-generated visuals
      README_IMPORT.txt — Step-by-step import instructions
    """
    ctx = SESSIONS.get(session_id)
    if not ctx:
        return jsonify({'error': 'Session not found'}), 404

    pbi_zip = ctx.pbi_zip

    # Auto-generate on demand if model is built but export not triggered yet
    if not pbi_zip and ctx.semantic_model:
        try:
            from core.pbi_exporter import export_powerbi_package
            from core.chat_agent import _get_all_dfs
            file_paths = getattr(ctx, 'file_paths', {})
            pbi_zip = export_powerbi_package(ctx.semantic_model, _get_all_dfs(ctx), file_paths)
            ctx.pbi_zip = pbi_zip
            SESSIONS[session_id] = ctx
        except Exception as exc:
            logger.exception("Power BI package generation failed")
            return jsonify({'error': f'Power BI export failed: {exc}'}), 500

    if not pbi_zip:
        return jsonify({'error': 'Power BI export not yet generated. Build the model first.'}), 404

    name = f"{ctx.project_name.replace(' ', '_')}_PowerBI_Export.zip"
    return Response(
        pbi_zip,
        mimetype='application/zip',
        headers={'Content-Disposition': f'attachment; filename="{name}"'}
    )


# ── Health check ───────────────────────────────────────────────────────────

@app.route('/api/health', methods=['GET'])
def health():
    api_key_set = bool(os.getenv('ANTHROPIC_API_KEY'))
    return jsonify({
        'status': 'ok',
        'api_key_configured': api_key_set,
        'active_sessions': len(SESSIONS),
        'timestamp': datetime.now().isoformat(),
    })


@app.route('/api/sessions', methods=['GET'])
def list_sessions():
    return jsonify({
        'sessions': [
            {'session_id': sid, **_ctx_summary(ctx)}
            for sid, ctx in SESSIONS.items()
        ]
    })


if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    debug = os.getenv('FLASK_DEBUG', 'false').lower() == 'true'
    logger.info("Starting AI Reporting API on port %d", port)
    app.run(host='0.0.0.0', port=port, debug=debug)