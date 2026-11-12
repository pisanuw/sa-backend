import json
import os
import time
import tempfile
from datetime import datetime, timezone

from backend.app_factory import create_app
from backend.db import db
from backend.models import Job
from backend.jobs.retention import empty_payload
from backend.image_prep import UnreadableImage, prepare as prepare_image
from backend.descriptions.summary_service import generate_summary_bundle
import base64
from backend.routes.api import json_to_mermaid

POLL_SECONDS = int(os.getenv("WORKER_POLL_SECONDS", "2"))


def utc_now_naive():
    """
    Return a UTC datetime without tzinfo so it matches common SQLAlchemy DateTime usage.
    """
    return datetime.utcnow()


def claim_one_job():
    """
    Claim the oldest queued job.

    Note:
    This is still a simple single-worker-friendly implementation.
    If you later run multiple workers, you should upgrade this to
    an atomic row-locking claim pattern.
    """
    job = Job.query.filter_by(status="queued").order_by(Job.created_at.asc()).first()
    if not job:
        return None

    job.status = "running"
    job.started_at = utc_now_naive()
    job.error = None
    db.session.commit()
    return job


def complete_job(job: Job, result_dict: dict):
    job.result = json.dumps(result_dict)
    job.status = "done"
    job.finished_at = utc_now_naive()
    empty_payload(job)  # the uploaded image is no longer needed
    db.session.commit()


def fail_job(job: Job, error_message: str):
    job.status = "failed"
    job.error = error_message
    job.finished_at = utc_now_naive()
    empty_payload(job)
    db.session.commit()


def process_test_job(job: Job, payload: dict):
    result = {
        "ok": True,
        "echo": payload,
    }
    complete_job(job, result)


def process_generate_summary_job(job: Job, payload: dict):
    diagram = payload["diagram"]
    model = None  # client-supplied model names are ignored; use llm.MODEL_SUMMARY

    bundle = generate_summary_bundle(
        diagram_json=diagram,
        model=model,
    )

    result = {
        "short_alt": bundle["short_alt"],
        "summary": bundle["long_markdown"],  # keep for backward compatibility
        "long_description_sections": bundle["long_description_sections"],
    }

    complete_job(job, result)


def process_upload_job(job: Job, payload: dict):
    img_bytes = base64.b64decode(payload["image_b64"])

    # The readers get an upright RGB PNG, white where the original was transparent.
    try:
        prepared = prepare_image(img_bytes)
    except UnreadableImage as e:
        fail_job(job, str(e))
        return

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp.write(prepared.png)
        tmp_path = tmp.name

    try:
        # Imported here, not at the top: the image pipeline loads torch, YOLO and Google Vision,
        # which chart and summary jobs (and the tests) do not need.
        from backend.pipeline.pipeline2 import process_image

        json_data = process_image(tmp_path, base_name="uploaded")
        mermaid_code = json_to_mermaid(json_data)

        # Keep the original when it can be stored as it is (PNG, JPEG, WebP, GIF); a BMP or TIFF is
        # kept as the prepared PNG instead. Either way it is the same size, so coordinates match.
        if prepared.keepable_mime:
            image_mime, kept = prepared.keepable_mime, img_bytes
        else:
            image_mime, kept = "image/png", prepared.png
        image_data_url = f"data:{image_mime};base64," + base64.b64encode(kept).decode("utf-8")

        result = {
            "mermaid_json": json_data,
            "mermaid_code": mermaid_code,
            "image_data_url": image_data_url,
            "image_mime": image_mime,
            "message": "Successfully processed image.",
        }

        complete_job(job, result)

    finally:
        try:
            os.remove(tmp_path)
        except Exception:
            pass

def process_extract_chart_job(job: Job, payload: dict):
    """Chart image -> table, settings and (for bar, line, pie) a Vega-Lite spec.

    The image is never returned and the job's payload (which holds it) is emptied afterwards, so a
    chart image is not kept in the jobs table once it has been read.
    """
    from backend.chart_validation import ChartValidationError
    from backend.pipeline.chart_extract import ConsentRequired, ExtractionError, extract_chart
    from backend.pipeline.chart_result import build_chart_result

    # Same preparation as STEM image uploads: a transparent chart PNG would otherwise read as black.
    try:
        prepared = prepare_image(base64.b64decode(payload["image_b64"]))
    except UnreadableImage as e:
        fail_job(job, str(e))  # also empties the payload, so the image is not kept
        return
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp.write(prepared.png)
        tmp_path = tmp.name

    try:
        try:
            x = extract_chart(tmp_path, allow_llm=bool(payload.get("allow_llm")))
        except ConsentRequired as e:
            fail_job(job, str(e))
            return
        except ExtractionError as e:
            fail_job(job, str(e))
            return

        try:
            result = build_chart_result(x)
        except ChartValidationError as e:
            fail_job(job, f"The chart could not be read as a table: {e}")
            return
        complete_job(job, result)
    finally:
        job.payload = "{}"
        db.session.commit()
        try:
            os.remove(tmp_path)
        except Exception:
            pass


def process_job(job: Job):
    payload = json.loads(job.payload or "{}")

    if job.type == "extract_chart":
        process_extract_chart_job(job, payload)
        return

    if job.type == "test":
        process_test_job(job, payload)
        return

    if job.type == "generate_summary":
        process_generate_summary_job(job, payload)
        return
    
    if job.type == "process_upload":
        process_upload_job(job, payload)
        return

    fail_job(job, f"Unknown job type: {job.type}")


def run_job_inline(job: Job):
    """
    Process a just-created job inside the current request.

    Used on scale-to-zero hosts (Cloud Run) where there is no always-on
    worker: CPU is only available while a request is in flight.
    """
    job.status = "running"
    job.started_at = utc_now_naive()
    job.error = None
    db.session.commit()

    try:
        process_job(job)
    except Exception as e:
        db.session.rollback()
        fail_job(job, str(e))
        print(f"❌ Job {job.id} failed: {e}")


def run_worker_loop(app):
    """Poll the jobs table forever. Runs standalone or as a thread in the web process."""
    with app.app_context():
        print("✅ Worker started and DB connected")

        while True:
            try:
                job = claim_one_job()
            except Exception as e:
                db.session.rollback()
                print(f"⚠️ Could not poll jobs: {e}")
                time.sleep(POLL_SECONDS)
                continue

            if not job:
                time.sleep(POLL_SECONDS)
                continue

            print(f"🧰 Processing job {job.id} ({job.type})")

            try:
                process_job(job)
                print(f"✅ Job {job.id} done")

            except Exception as e:
                db.session.rollback()
                fail_job(job, str(e))
                print(f"❌ Job {job.id} failed: {e}")


def main():
    run_worker_loop(create_app())


if __name__ == "__main__":
    main()