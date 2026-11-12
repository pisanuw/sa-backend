"""Chart image extraction: parsing, routing between the local reader and the AI model, consent,
spec templating and the upload route. No model, network or real image is used; both extractors are
replaced with fixed results.
    .venv/bin/python -m unittest tests.test_chart_extract -v
"""
import io
import json
import os
import tempfile
import unittest
from unittest import mock

_tmp = tempfile.mkdtemp()
os.environ["AUTH_MODE"] = "dev"
os.environ["AUTO_CREATE_TABLES"] = "1"
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test.sqlite"
os.environ.pop("K_SERVICE", None)

from backend.app_factory import create_app  # noqa: E402
from backend.chart_validation import clean_spec  # noqa: E402
from backend.db import db  # noqa: E402
from backend.models import Job  # noqa: E402
from backend.pipeline import chart_extract as ce  # noqa: E402
from backend.pipeline.chart_spec import build_config, build_spec  # noqa: E402

def _tiny_png():
    # A real (if tiny) picture: the chart job now decodes the upload before reading it.
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (4, 4), "white").save(out, format="PNG")
    return out.getvalue()


PNG = _tiny_png()


def single_series(extractor="unichart"):
    return ce.ChartExtraction(
        columns=["Region", "Units"], rows=[["North", 21.0], ["South", 8.0], ["East", 11.0]],
        extractor=extractor, chart_type="bar", values_are_estimated=False,
    )


def multi_series(extractor="unichart"):
    return ce.ChartExtraction(
        columns=["Region", "2023", "2024"], rows=[["North", 15.0, 54.0], ["South", 19.0, 73.0]],
        extractor=extractor, chart_type="bar",
    )


class ParsingTests(unittest.TestCase):
    def test_parse_number(self):
        self.assertEqual(ce.parse_number("1,200"), 1200.0)
        self.assertEqual(ce.parse_number("45%"), 45.0)
        self.assertEqual(ce.parse_number(3), 3.0)
        self.assertIsNone(ce.parse_number("n/a"))
        self.assertIsNone(ce.parse_number(True))

    def test_unichart_table_text(self):
        header, body = ce.parse_unichart_table("Characteristic | Value & Haiti | 6.12% & Libya | 5.32%")
        self.assertEqual(header, ["Characteristic", "Value"])
        self.assertEqual(body, [["Haiti", "6.12%"], ["Libya", "5.32%"]])

    def test_empty_unichart_output_is_an_error(self):
        with self.assertRaises(ce.ExtractionError):
            ce.parse_unichart_table("Characteristic | Value")

    def test_normalise_makes_unique_names_and_numbers(self):
        cols, rows = ce._normalise(["Year", "Year", ""], [["2004.0", "1,5", "x"], ["2005", 2, None]])
        self.assertEqual(cols, ["Year", "Year 2", "Column 3"])
        self.assertEqual(rows[0], ["2004", 15.0, "x"])
        self.assertEqual(rows[1], ["2005", 2.0, None])

    def test_whole_number_labels_lose_the_float_suffix_but_others_do_not(self):
        _, rows = ce._normalise(
            ["Label", "Value"],
            [["0.0", 1], ["12.0", 2], ["-3.0", 3], ["2.5", 4], ["1.05", 5], ["Q1.0", 6], ["10.00", 7]],
        )
        self.assertEqual([r[0] for r in rows], ["0", "12", "-3", "2.5", "1.05", "Q1.0", "10"])

    def test_chart_type_suggestion(self):
        self.assertEqual(ce.suggest_chart_type(["y", "v"], [["2004", 1], ["2005", 2]]), "line")
        self.assertEqual(ce.suggest_chart_type(["m", "v"], [["Jan", 1], ["Feb", 2]]), "line")
        self.assertEqual(ce.suggest_chart_type(["c", "v"], [["Haiti", 1], ["Libya", 2]]), "bar")
        self.assertIsNone(ce.suggest_chart_type(["c", "v"], []))


class RoutingTests(unittest.TestCase):
    def test_llm_mode_needs_consent(self):
        with mock.patch.object(ce, "extract_with_llm") as llm:
            with self.assertRaises(ce.ConsentRequired):
                ce.extract_chart("x.png", allow_llm=False, mode="llm")
            llm.assert_not_called()

    def test_complete_local_result_never_calls_the_llm(self):
        with mock.patch.object(ce, "extract_with_unichart", return_value=single_series()), \
             mock.patch.object(ce, "extract_with_llm") as llm:
            out = ce.extract_chart("x.png", allow_llm=True, mode="unichart")
        self.assertEqual(out.extractor, "unichart")
        llm.assert_not_called()

    def test_incomplete_result_escalates_only_with_consent(self):
        with mock.patch.object(ce, "extract_with_unichart", return_value=multi_series()), \
             mock.patch.object(ce, "extract_with_llm", return_value=multi_series("llm")) as llm:
            declined = ce.extract_chart("x.png", allow_llm=False, mode="unichart")
            llm.assert_not_called()
            self.assertEqual(declined.extractor, "unichart")
            self.assertTrue(any("may be incomplete" in w for w in declined.warnings))

            agreed = ce.extract_chart("x.png", allow_llm=True, mode="unichart")
            llm.assert_called_once()
            self.assertEqual(agreed.extractor, "llm")

    def test_llm_failure_after_consent_falls_back_with_a_warning(self):
        with mock.patch.object(ce, "extract_with_unichart", return_value=multi_series()), \
             mock.patch.object(ce, "extract_with_llm", side_effect=ce.ExtractionError("boom")):
            out = ce.extract_chart("x.png", allow_llm=True, mode="unichart")
        self.assertEqual(out.extractor, "unichart")
        self.assertTrue(out.warnings)


class SpecTests(unittest.TestCase):
    def dataset(self):
        return {"columns": ["Region", "Units"], "rows": [{"Region": "N", "Units": 1}, {"Region": "S", "Units": 2}]}

    def test_templated_specs_pass_the_validator(self):
        for preset in ("bar", "line", "pie"):
            with self.subTest(preset=preset):
                cfg = build_config(["Region", "Units"], preset, title="T")
                spec = build_spec(self.dataset(), cfg)
                self.assertIsNotNone(clean_spec(spec))
                self.assertNotIn("url", json.dumps(spec))

    def test_no_template_means_no_spec(self):
        cfg = build_config(["Region", "Units"], "scatter")
        self.assertIsNone(build_spec(self.dataset(), cfg))

    def multi_dataset(self):
        return {
            "columns": ["Region", "2023", "2024"],
            "rows": [{"Region": "N", "2023": 15, "2024": 54}, {"Region": "S", "2023": 19, "2024": 73}],
        }

    def test_bar_with_several_series_keeps_every_series(self):
        ds = self.multi_dataset()
        spec = build_spec(ds, build_config(ds["columns"], "bar"))
        self.assertIsNotNone(clean_spec(spec))
        self.assertEqual(spec["transform"], [{"fold": ["2023", "2024"], "as": ["Series", "Value"]}])
        self.assertEqual(spec["data"]["values"][0], {"Region": "N", "2023": 15, "2024": 54})
        self.assertEqual(spec["encoding"]["xOffset"], {"field": "Series"})
        self.assertEqual(spec["encoding"]["y"]["title"], "Value")  # not the first series' name

    def test_line_with_several_series_has_one_line_per_series(self):
        ds = self.multi_dataset()
        spec = build_spec(ds, build_config(ds["columns"], "line"))
        self.assertIsNotNone(clean_spec(spec))
        self.assertEqual(spec["encoding"]["color"]["field"], "Series")
        self.assertNotIn("xOffset", spec["encoding"])

    def test_pie_still_shows_only_the_first_value_column(self):
        ds = self.multi_dataset()
        spec = build_spec(ds, build_config(ds["columns"], "pie"))
        self.assertNotIn("transform", spec)
        self.assertEqual(spec["encoding"]["theta"]["field"], "2023")

    def test_generated_field_names_avoid_existing_columns(self):
        ds = {"columns": ["Series", "a", "b"], "rows": [{"Series": "x", "a": 1, "b": 2}]}
        spec = build_spec(ds, build_config(ds["columns"], "bar"))
        self.assertEqual(spec["transform"][0]["as"], ["Series_", "Value"])

    def test_single_series_spec_is_unchanged(self):
        spec = build_spec(self.dataset(), build_config(["Region", "Units"], "bar"))
        self.assertNotIn("transform", spec)

    def test_unknown_preset_is_dropped_from_config(self):
        self.assertIsNone(build_config(["a", "b"], "treemap")["preset"])


class RouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app()
        cls.client = cls.app.test_client()

    def post(self, data=None, content_type="image/png", body=PNG, **env):
        data = dict(data or {})
        data["file"] = (io.BytesIO(body), "chart.png", content_type)
        with mock.patch.dict(os.environ, {"RUN_JOBS_INLINE": "1", **env}):
            return self.client.post("/jobs/extract-chart", data=data, content_type="multipart/form-data")

    def test_rejects_non_images(self):
        r = self.post(content_type="application/pdf")
        self.assertEqual(r.status_code, 400)

    def test_rejects_oversize_images(self):
        with self.app.app_context():
            before = Job.query.filter_by(type="extract_chart").count()
        r = self.post(MAX_CHART_IMAGE_BYTES="10", CHART_EXTRACTOR="unichart")
        self.assertEqual(r.status_code, 413)
        with self.app.app_context():
            self.assertEqual(Job.query.filter_by(type="extract_chart").count(), before)

    def test_llm_only_setup_refuses_without_consent(self):
        r = self.post(CHART_EXTRACTOR="llm")
        self.assertEqual(r.status_code, 400)
        self.assertTrue(r.get_json()["needs_ai_consent"])
        with self.app.app_context():
            self.assertEqual(Job.query.filter_by(type="extract_chart").count(), 0)

    def test_local_extraction_end_to_end_and_image_is_not_kept(self):
        with mock.patch.object(ce, "extract_with_unichart", return_value=single_series()):
            r = self.post(CHART_EXTRACTOR="unichart")
            self.assertEqual(r.status_code, 201)
            job_id = r.get_json()["job_id"]
        with self.app.app_context():
            job = db.session.get(Job, job_id)
            self.assertEqual(job.status, "done")
            result = json.loads(job.result)
            self.assertEqual(result["extractor"], "unichart")
            self.assertFalse(result["sent_to_ai_model"])
            self.assertEqual(result["dataset"]["columns"], ["Region", "Units"])
            self.assertEqual(result["config"]["preset"], "bar")
            self.assertIsNotNone(result["spec"])
            self.assertEqual(job.payload, "{}")  # the uploaded image is gone


TOKEN = "g" * 40


class OptionsAndGuestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app()
        cls.client = cls.app.test_client()

    def guest_post(self, env=None, content_type="image/png", token=TOKEN):
        data = {"file": (io.BytesIO(PNG), "chart.png", content_type)}
        headers = {"X-Guest-Token": token} if token else {}
        with mock.patch.dict(os.environ, env or {}):
            return self.client.post("/guest/extract-chart", data=data, headers=headers,
                                    content_type="multipart/form-data")

    def test_options_report_what_is_configured(self):
        with mock.patch.dict(os.environ, {"UNICHART_URL": "http://reader", "ACCESSFLOW_OPENROUTER_API_KEY": "k"}):
            o = self.client.get("/chart-extraction/options").get_json()
        self.assertTrue(o["local"] and o["llm"])
        self.assertEqual(o["mode"], "unichart")
        env = {k: v for k, v in os.environ.items() if k not in ("UNICHART_URL", "ACCESSFLOW_OPENROUTER_API_KEY", "CHART_EXTRACTOR")}
        with mock.patch.dict(os.environ, env, clear=True):
            o = self.client.get("/chart-extraction/options").get_json()
        self.assertFalse(o["local"] or o["llm"])

    def test_options_do_not_leak_values(self):
        with mock.patch.dict(os.environ, {"UNICHART_URL": "http://secret-host", "ACCESSFLOW_OPENROUTER_API_KEY": "sk-secret"}):
            body = self.client.get("/chart-extraction/options").get_data(as_text=True)
        self.assertNotIn("secret", body)

    def test_guest_needs_the_built_in_reader(self):
        env = {k: v for k, v in os.environ.items() if k != "UNICHART_URL"}
        with mock.patch.dict(os.environ, env, clear=True):
            r = self.guest_post()
        self.assertEqual(r.status_code, 503)

    def test_guest_gets_a_table_and_the_llm_is_never_called(self):
        with mock.patch.object(ce, "extract_with_unichart", return_value=multi_series()), \
             mock.patch.object(ce, "extract_with_llm") as llm:
            r = self.guest_post({"UNICHART_URL": "http://reader", "ACCESSFLOW_OPENROUTER_API_KEY": "k"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        body = r.get_json()
        self.assertEqual(body["extractor"], "unichart")
        self.assertFalse(body["sent_to_ai_model"])
        self.assertTrue(any("may be incomplete" in w for w in body["warnings"]))
        llm.assert_not_called()

    def test_guest_requires_a_token_and_an_image(self):
        self.assertEqual(self.guest_post({"UNICHART_URL": "http://reader"}, token="").status_code, 400)
        self.assertEqual(self.guest_post({"UNICHART_URL": "http://reader"}, content_type="application/pdf").status_code, 400)


if __name__ == "__main__":
    unittest.main()
