import json
import os
import uuid
import base64
from datetime import datetime
from flask import Blueprint, request, jsonify, current_app, g
from werkzeug.utils import secure_filename
from backend.db import db
from backend.models import Job
from backend.auth import require_auth
from backend.jobs.retention import purge_quietly

jobs_bp = Blueprint("jobs", __name__)

# Jobs stuck in "running" longer than this are reported as failed (e.g. the
# instance was recycled mid-job on a scale-to-zero host).
JOB_TIMEOUT_SECONDS = int(os.environ.get("JOB_TIMEOUT_SECONDS", "900"))


def run_inline_if_enabled(job: Job):
    """With RUN_JOBS_INLINE=1, process the job now instead of leaving it for a worker."""
    if os.environ.get("RUN_JOBS_INLINE") != "1":
        return
    # Imported lazily: the worker module imports the app factory (circular otherwise).
    from backend.jobs.worker import run_job_inline

    run_job_inline(job)


def serialize_job(job: Job):
    return {
        "job_id": job.id,
        "type": job.type,
        "status": job.status,
        "result": json.loads(job.result) if job.result else None,
        "error": job.error,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


@jobs_bp.route("/jobs/test", methods=["POST"])
@require_auth
def enqueue_test_job():
    payload = request.get_json(silent=True) or {"message": "hello worker"}

    job = Job(
        type="test",
        payload=json.dumps(payload),
        status="queued",
        owner_id=g.user_id,
    )

    db.session.add(job)
    db.session.commit()
    run_inline_if_enabled(job)

    return jsonify({
        "job_id": job.id,
        "status": job.status,
    }), 201


@jobs_bp.route("/jobs/generate-summary", methods=["POST"])
@require_auth
def enqueue_summary_job():
    payload = request.get_json(silent=True) or {}

    diagram = payload.get("diagram")
    if not diagram:
        return jsonify({"error": "Missing required field: diagram"}), 400

    job = Job(
        type="generate_summary",
        payload=json.dumps(payload),
        status="queued",
        owner_id=g.user_id,
    )

    db.session.add(job)
    db.session.commit()
    run_inline_if_enabled(job)

    return jsonify({
        "job_id": job.id,
        "status": job.status,
    }), 201


from sqlalchemy.orm import load_only

@jobs_bp.route("/jobs/<int:job_id>", methods=["GET"])
@require_auth
def get_job(job_id):
    job = (
        Job.query
        .options(
            load_only(
                Job.id,
                Job.owner_id,
                Job.type,
                Job.status,
                Job.result,
                Job.error,
                Job.created_at,
                Job.started_at,
                Job.finished_at,
            )
        )
        .filter_by(id=job_id)
        .first_or_404()
    )

    # Jobs are private to the user who created them; don't reveal that others exist.
    if job.owner_id != g.user_id:
        return jsonify({"error": "Not found"}), 404

    if (
        job.status == "running"
        and job.started_at
        and (datetime.utcnow() - job.started_at).total_seconds() > JOB_TIMEOUT_SECONDS
    ):
        job.status = "failed"
        job.error = "Job timed out"
        job.finished_at = datetime.utcnow()
        db.session.commit()

    return jsonify({
        "job_id": job.id,
        "type": job.type,
        "status": job.status,
        "result": json.loads(job.result) if job.result else None,
        "error": job.error,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }), 200

CHART_IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp"}


def _max_chart_image_bytes():
    """Read per request so the limit can be changed without a restart and tested."""
    try:
        return int(os.environ.get("MAX_CHART_IMAGE_BYTES", str(8 * 1024 * 1024)))
    except ValueError:
        return 8 * 1024 * 1024


def _chart_extractor_uses_llm():
    """True when a request with allow_llm unset would still have to call the LLM."""
    mode = os.environ.get("CHART_EXTRACTOR") or ("unichart" if os.environ.get("UNICHART_URL") else "llm")
    return mode == "llm"


@jobs_bp.route("/chart-extraction/options", methods=["GET"])
def chart_extraction_options():
    """What reading a chart image can do on this deployment (public; reveals no configuration
    values). The page uses it to show only the choices that exist."""
    from backend.pipeline.chart_extract import extraction_options

    o = extraction_options()
    return jsonify({
        "local": o["local"],   # built-in reader: the image is not sent to a third party
        "llm": o["llm"],       # AI model (Gemini via OpenRouter): needs the person's agreement
        "mode": os.environ.get("CHART_EXTRACTOR") or ("unichart" if o["local"] else "llm"),
    }), 200


@jobs_bp.route("/jobs/extract-chart", methods=["POST"])
@require_auth
def enqueue_extract_chart_job():
    """Read a chart image into a table. Form fields: `file`, and `allow_llm` = "true" only when the
    person has chosen to send the image to the AI model (Gemini through OpenRouter). Without it the
    image is never sent to a third party."""
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"error": "No file provided"}), 400
    if file.mimetype not in CHART_IMAGE_TYPES:
        return jsonify({"error": "Upload a PNG, JPEG or WebP image"}), 400

    limit = _max_chart_image_bytes()
    img_bytes = file.read(limit + 1)
    if not img_bytes:
        return jsonify({"error": "Empty file"}), 400
    if len(img_bytes) > limit:
        return jsonify({"error": f"Image is larger than {max(limit // (1024 * 1024), 1)} MB"}), 413

    allow_llm = (request.form.get("allow_llm") or "").lower() == "true"
    if _chart_extractor_uses_llm() and not allow_llm:
        return jsonify({
            "error": "Reading this chart needs the AI model, which sends the image to a third party. "
                     "Agree to that, or enter the data as a CSV instead.",
            "needs_ai_consent": True,
        }), 400

    purge_quietly()  # keep old uploads out of the table even without a scheduled cleanup
    job = Job(
        type="extract_chart",
        payload=json.dumps({
            "image_b64": base64.b64encode(img_bytes).decode("utf-8"),
            "image_mime": file.mimetype,
            "allow_llm": allow_llm,
        }),
        status="queued",
        owner_id=g.user_id,
    )
    db.session.add(job)
    db.session.commit()
    run_inline_if_enabled(job)

    return jsonify({"job_id": job.id, "status": job.status}), 201


@jobs_bp.route("/jobs/process-upload", methods=["POST"])
@require_auth
def enqueue_upload_job():
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    file = request.files["file"]
    if not file or not file.filename:
        return jsonify({"error": "Invalid file"}), 400

    # Checked before a job exists, so a file that cannot be used never reaches the paid readers.
    from backend.image_prep import UnreadableImage, prepare
    from backend.image_store import MAX_IMAGE_BYTES

    img_bytes = file.read(MAX_IMAGE_BYTES + 1)
    if not img_bytes:
        return jsonify({"error": "The file is empty."}), 400
    if len(img_bytes) > MAX_IMAGE_BYTES:
        # The original is kept with the STEM image, and the store takes up to this size.
        return jsonify({"error": f"The picture is larger than {MAX_IMAGE_BYTES // (1024 * 1024)} MB. "
                                 "Upload a smaller one."}), 413
    try:
        prepared = prepare(img_bytes)
    except UnreadableImage as e:
        return jsonify({"error": str(e)}), 400

    mime_type = prepared.keepable_mime or file.mimetype or "image/png"

    payload = {
        "image_b64": base64.b64encode(img_bytes).decode("utf-8"),
        "original_filename": file.filename,
        "image_mime": mime_type,
    }

    purge_quietly()
    job = Job(
        type="process_upload",
        payload=json.dumps(payload),
        status="queued",
        owner_id=g.user_id,
    )

    db.session.add(job)
    db.session.commit()
    run_inline_if_enabled(job)

    return jsonify({
        "job_id": job.id,
        "status": job.status,
    }), 201
    