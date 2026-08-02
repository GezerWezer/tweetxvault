from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB_DIR = ROOT / "tweetxvault" / "web"
NODE_TEST = ROOT / "tests" / "js" / "test_web_assets.cjs"


def test_browser_javascript_unit_suite():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the deterministic browser-asset unit harness")

    result = subprocess.run(
        [node, str(NODE_TEST)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "browser asset tests passed" in result.stdout


def test_index_loads_local_assets_in_dependency_order():
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    themes = html.index('<script src="/static/js/themes.js"></script>')
    autocomplete = html.index('<script src="/static/js/autocomplete.js"></script>')
    app = html.index('<script src="/static/js/app.js"></script>')

    assert themes < autocomplete < app
    assert '<link rel="stylesheet" href="/static/css/styles.css">' in html
    assert 'x-data="tweetApp()"' in html
    assert 'x-data="searchAutocomplete()"' in html
    assert 'aria-label="Scroll to top"' in html
    assert 'x-show="archiveEnrichmentIncomplete > 0"' in html
    assert "tweetxvault import enrich" in html
    assert ">Archive status<" in html
    assert 'x-text="statsHealth.enrichment.done.toLocaleString()"' in html
    assert 'x-text="statsHealth.enrichment.resurrected.toLocaleString()"' in html
    assert 'x-text="statsHealth.enrichment.incomplete.toLocaleString()"' in html
    assert 'x-model="showEmptyUnavailableReasons"' in html
    assert 'x-text="statsHealth.enrichment.available.toLocaleString()"' not in html

    app_js = (WEB_DIR / "static" / "js" / "app.js").read_text(encoding="utf-8")
    assert "fetchArchiveEnrichmentStatus" in app_js
    assert "d.enrichment?.incomplete" in app_js


@pytest.mark.parametrize(
    "relative_path",
    [
        "static/js/themes.js",
        "static/js/autocomplete.js",
        "static/js/app.js",
        "static/css/styles.css",
    ],
)
def test_index_referenced_assets_are_packaged(relative_path: str):
    asset = WEB_DIR / relative_path
    assert asset.is_file()
    assert asset.stat().st_size > 0
