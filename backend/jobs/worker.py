import json
import os
import time
import tempfile
from datetime import datetime, timezone

from backend.app_factory import create_app
from backend.db import db
from backend.models import Job
from backend.jobs.retention import empty_payload
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
    image_b64 = payload["image_b64"]
    image_mime = payload.get("image_mime", "image/png")

    img_bytes = base64.b64decode(image_b64)

    suffix = ".png"
    if image_mime == "image/jpeg":
        suffix = ".jpg"
    elif image_mime == "image/webp":
        suffix = ".webp"

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(img_bytes)
        tmp_path = tmp.name

    try:
        # Imported here, not at the top: the image pipeline loads torch, YOLO and Google Vision,
        # which chart and summary jobs (and the tests) do not need.
        from backend.pipeline.pipeline2 import process_image

        json_data = process_image(tmp_path, base_name="uploaded")
        mermaid_code = json_to_mermaid(json_data)

        image_data_url = f"data:{image_mime};base64," + base64.b64encode(img_bytes).decode("utf-8")

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

    suffix = {"image/jpeg": ".jpg", "image/webp": ".webp"}.get(payload.get("image_mime"), ".png")
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(base64.b64decode(payload["image_b64"]))
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