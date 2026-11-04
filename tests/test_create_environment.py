"""Offline unit tests for create_environment helpers. Run: python -m unittest discover tests"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from create_environment import CreateEnvironment, split_from  # noqa: E402


class SplitFromTests(unittest.TestCase):
    def test_basic_and_options(self):
        self.assertEqual(split_from("FROM ubuntu:22.04\nRUN x\n"), ("ubuntu:22.04", "RUN x\n"))
        image, rest = split_from("from --platform=linux/amd64 quay.io/a/b:1 AS base\nUSER root\n")
        self.assertEqual((image, rest), ("quay.io/a/b:1", "USER root\n"))

    def test_no_from(self):
        self.assertEqual(split_from("RUN true\n"), (None, "RUN true\n"))

    def test_arg_before_from_is_kept(self):
        self.assertEqual(split_from("ARG X=1\nFROM img\nRUN y"), ("img", "ARG X=1\nRUN y\n"))

    def test_from_inside_continued_run_is_content(self):
        text = 'RUN python - <<PY\nPY\nRUN python -c "x" && \\\n    echo a \\\nfrom pathlib import Path\n'
        image, rest = split_from("FROM img\n" + text)
        self.assertEqual(image, "img")
        self.assertIn("from pathlib import Path", rest)

    def test_continuation_with_comment_line(self):
        text = "FROM img\nRUN echo a \\\n# comment\nFROM b\n"
        image, rest = split_from(text)
        self.assertEqual(image, "img")
        self.assertIn("FROM b", rest)

    def test_from_inside_heredoc_is_content(self):
        text = "FROM img\nRUN <<EOT\nSELECT 1\nFROM source_table\nEOT\nRUN true\n"
        image, rest = split_from(text)
        self.assertEqual(image, "img")
        self.assertIn("FROM source_table", rest)
        self.assertIn("RUN true", rest)

    def test_quoted_and_dash_heredoc_delimiters(self):
        for opener, close in (("<<'SQL'", "SQL"), ('<<"SQL"', "SQL"), ("<<-SQL", "SQL")):
            text = f"RUN cat {opener}\nFROM t\n{close}\nFROM img\n"
            image, rest = split_from(text)
            self.assertEqual(image, "img", opener)
            self.assertIn("FROM t", rest)

    def test_instruction_after_heredoc_still_parsed(self):
        image, _ = split_from("RUN <<EOT\nx\nEOT\nFROM late\n")
        self.assertEqual(image, "late")

    def test_errors(self):
        for text in ("FROM a\nFROM b\n", "FROM scratch\n", "FROM ${BASE}\n", "FROM\n"):
            with self.assertRaises(ValueError, msg=text):
                split_from(text)


class FakeResponse:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


class FakeClient:
    """Serves the latest-revision API plus a paged revision listing."""

    def __init__(self, latest_id, ids, per_page=50):
        self.latest_id, self.ids, self.per_page, self.paged_calls = latest_id, ids, per_page, 0

    def get(self, path, **kw):
        if path.startswith("/api/environments/v1/environments/"):
            return FakeResponse({"environment": {"latestRevision": {"id": self.latest_id, "status": "Building"}}})
        self.paged_calls += 1
        page = int(path.split("/")[-2])
        chunk = self.ids[page * self.per_page:(page + 1) * self.per_page]
        pages = max(1, -(-len(self.ids) // self.per_page))
        return FakeResponse({"paginatedResults": {
            "listing": [{"id": i, "buildStatus": "Succeeded"} for i in chunk],
            "details": {"totalPages": pages}}})


class RevisionStatusTests(unittest.TestCase):
    def test_latest_uses_public_api(self):
        client = FakeClient("r1", ["r1"])
        self.assertEqual(CreateEnvironment()._revision_status(client, "e", "r1"), "building")
        self.assertEqual(client.paged_calls, 0)

    def test_superseded_revision_beyond_first_page(self):
        ids = [f"r{n}" for n in range(120, 0, -1)]          # newest first, target is the oldest
        client = FakeClient("r120", ids)
        self.assertEqual(CreateEnvironment()._revision_status(client, "e", "r1"), "succeeded")
        self.assertEqual(client.paged_calls, 3)

    def test_unknown_revision_returns_empty(self):
        client = FakeClient("r2", ["r2", "r1"])
        self.assertEqual(CreateEnvironment()._revision_status(client, "e", "missing"), "")


if __name__ == "__main__":
    unittest.main()
