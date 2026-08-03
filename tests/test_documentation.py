from __future__ import annotations

from pathlib import Path


def test_readme_covers_required_contributor_workflows() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    for heading in (
        "## Test a local source checkout",
        "## Test an existing wheel",
        "## Test coordinated changes from multiple repositories",
        "## Download once and run offline",
        "## Read a result",
    ):
        assert heading in readme


def test_handoff_documents_are_present_and_linked() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    paths = (
        "docs/architecture.md",
        "docs/local-and-o2-workflows.md",
        "docs/adding-a-package-or-suite.md",
        "docs/approving-output-changes.md",
        "docs/data-governance.md",
        "docs/release-procedure.md",
        "docs/pipeline-end-to-end.md",
        "MAINTAINERS.md",
    )
    for value in paths:
        assert Path(value).is_file()
        assert value in readme


def test_public_docs_use_informative_profile_names() -> None:
    public_docs = [Path("README.md"), Path("CONTRIBUTING.md"), *Path("docs").glob("*.md")]
    text = "\n".join(path.read_text(encoding="utf-8") for path in public_docs)
    for name in ("install-smoke", "historical-regression", "pipeline-smoke"):
        assert name in text
    assert "Gate A" in text
    assert "supported contributor interface" in text
