"""The worker must not load the image pipeline (torch, YOLO, Google Vision) just by being imported,
and `process_image` must stay available by name for job types that pass it along.
    .venv/bin/python -m unittest tests.test_worker_imports -v
"""
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

_tmp = tempfile.mkdtemp()
os.environ["AUTH_MODE"] = "dev"
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test.sqlite"
os.environ.pop("K_SERVICE", None)


class WorkerImportTests(unittest.TestCase):
    def test_importing_the_worker_does_not_load_the_pipeline(self):
        sys.modules.pop("backend.pipeline.pipeline2", None)
        import backend.jobs.worker as worker  # noqa: F401

        self.assertNotIn("backend.pipeline.pipeline2", sys.modules)
        self.assertNotIn("torch", sys.modules)

    def test_process_image_loads_the_pipeline_when_called(self):
        import backend.jobs.worker as worker

        calls = []
        fake = types.ModuleType("backend.pipeline.pipeline2")
        fake.process_image = lambda path, base_name=None: calls.append((path, base_name)) or {"nodes": [], "edges": []}
        with mock.patch.dict(sys.modules, {"backend.pipeline.pipeline2": fake}):
            self.assertEqual(worker.process_image("/tmp/x.png", base_name="uploaded"), {"nodes": [], "edges": []})
        self.assertEqual(calls, [("/tmp/x.png", "uploaded")])


if __name__ == "__main__":
    unittest.main()
