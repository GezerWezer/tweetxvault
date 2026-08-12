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
    assert 'aria-label="Command activity"' in html
    assert "activity-spinner" in html
    assert "Scheduled Syncs" in html
    assert "Activity Logs" in html
    assert "Connect your account" in html
    assert "Paste session cookies from a signed-in browser." in html
    assert 'x-model="setupAuth.browser"' not in html
    assert 'x-model="setupAuth.browser_profile"' not in html
    assert 'x-model="setupAuth.browser_profile_path"' not in html
    assert "Import archive &amp; enrich" in html or "Import archive & enrich" in html
    assert "activity-step-viewport" in html
    assert "activity-completed-stack" in html
    assert '@scroll="setActivityScrollPosition($event)"' in html
    assert "activityShowCompleted" not in html
    assert "activityShowPending" not in html
    assert "activityPendingSteps" not in html
    assert "Show ' +" not in html
    assert '<span class="truncate font-bold text-[14px]">Archive Sync</span>' in html
    assert "`Last sync completed ${formatRelativeDate" in html
    assert "margin-bottom: 1rem" not in html
    assert "activity-sync-schedule-primary" in html
    assert 'x-show="!(activity || activityStartPending)" class="activity-sync-schedule"' in html
    assert "activity-sync-action is-primary" in html
    assert "activity-sync-action is-danger" in html
    assert 'x-text="activitySchedule.relative"' in html
    assert 'x-text="activitySchedule.date"' in html
    assert 'x-text="statsHealth.enrichment.available.toLocaleString()"' not in html

    app_js = (WEB_DIR / "static" / "js" / "app.js").read_text(encoding="utf-8")
    styles = (WEB_DIR / "static" / "css" / "styles.css").read_text(encoding="utf-8")
    assert "fetchArchiveEnrichmentStatus" in app_js
    assert "/api/stats/enrichment-incomplete" in app_js
    assert "d.incomplete" in app_js
    assert "/api/activity/status" in app_js
    assert "`/api/activity/${kind}`" in app_js
    assert "/api/activity/stop" in app_js
    assert "/api/setup/archive/import" in app_js
    assert "/api/setup/auth/test" not in app_js
    assert "setActivityScrollPosition" in app_js
    assert "activityShowPending" not in app_js
    assert "activityPendingSteps" not in app_js
    assert "formatActivityStatusTitle" not in app_js
    assert "var(--bg-primary) 97%" in styles
    assert ".activity-sync-action:focus-visible" in styles
    assert "min-height: 40px;" in styles
    assert "height: calc(100% + var(--activity-active-offset));" in styles
    assert "transform: translateY(var(--activity-fade-shift));" in styles
    assert "activity-drawer-controls-has-stack" in html
    assert "activity-drawer-controls-divider" in html
    assert '@wheel.stop="reverseActivityScroll($event)"' in html
    assert "activity-issues-panel" in html
    assert "activity-issues-count" in html
    assert "No issues reported" in html
    assert "--activity-active-offset: 0px;" in styles
    assert "--activity-active-offset: 64px;" in styles
    assert "--activity-completed-card-height: 64px;" in styles
    assert "transparent calc(100% - 24px)" in styles
    assert "transition: margin-top 0.35s" in styles
    assert "scrollbar-width: none;" in styles
    assert ".activity-step-viewport::-webkit-scrollbar" in styles
    assert "scrollbar-gutter: auto;" in styles
    assert "padding: 0 0 20px;" in styles
    assert "padding: 10px 16px;" in styles
    assert "activity-active-card-status" in html
    assert "activity-active-card-title" in html
    assert "height: 3px;" in styles
    assert "display: flow-root;" in styles
    assert "margin-inline: 4px;" not in styles
    assert "border-inline: 1px solid var(--border-color);" not in styles
    assert "activity-step-track-has-stack" in html
    assert "activity-step-scene" in html
    scene_styles = styles.split(".activity-step-track-has-stack .activity-step-scene {", 1)[
        1
    ].split("}", 1)[0]
    stack_styles = styles.split(".activity-completed-stack {", 1)[1].split("}", 1)[0]
    assert "top: var(--activity-stack-height);" in scene_styles
    assert "-1 * var(--activity-stack-height)" in scene_styles
    assert "var(--activity-scroll-shift)" in scene_styles
    assert "transform:" not in stack_styles
    assert ".activity-issues-scroll" in styles
    assert "max-height: 104px;" in styles
    assert "activity-step-scroll-range" not in html
    assert (
        "height: calc(100% + var(--activity-stack-height) - var(--activity-active-offset));"
        in styles
    )
    assert "overflow: clip;" in styles
    assert "const fadeTravel = 64;" in app_js
    assert "const sceneShift = scrollTop * 2;" in app_js
    assert "if (!this.activityHasActiveStep()) return;" in app_js
    assert "--activity-fade-opacity" in app_js
    assert "transition: none;" in styles
    assert styles.count("overscroll-behavior: none;") >= 2


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
