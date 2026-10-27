"""Guard dialog positioning against the global CSS reset.

These stylesheet contracts complement browser geometry checks at desktop and
mobile viewports; they do not claim to replace a browser layout engine.
"""

import re
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "novatrack"


def test_desktop_modal_restores_auto_margin_after_reset():
    css = (ROOT / "static/style.css").read_text()
    rule = re.search(r"(?m)^\.modal\s*\{([^}]+)\}", css)
    assert rule is not None
    assert re.search(r"\bmargin\s*:\s*auto\s*;", rule[1]), (
        "The global margin: 0 reset must not pin the desktop dialog to the origin"
    )


def test_mobile_modal_remains_bottom_anchored():
    css = (ROOT / "static/style.css").read_text()
    mobile = css.split("@media (max-width: 640px)", 1)[1]
    rule = re.search(r"\.modal\s*\{([^}]+)\}", mobile)
    assert rule is not None
    for prop, value in {"position": "fixed", "top": "auto", "bottom": "0", "margin": "0"}.items():
        assert re.search(rf"\b{prop}\s*:\s*{value}\s*;", rule[1]), (
            f"Mobile dialog needs {prop}: {value}; to override the browser's top inset"
        )


def test_alert_warn_defined_in_stylesheet():
    """alert-warn class must be defined so the registration-paused banner is amber/yellow.

    The template uses alert alert-warn for the paused registration banner.
    Without this CSS rule the banner renders with no color — invisible as a warning.
    """
    css = (ROOT / "static/style.css").read_text()
    assert ".alert-warn" in css, (
        "alert-warn CSS class must be defined; the paused-registration banner "
        "uses it for the amber/yellow color. Without it the banner is invisible."
    )
    # Confirm it references --warning so it turns amber/yellow
    warn_block = re.search(r"\.alert-warn\s*\{([^}]+)\}", css)
    assert warn_block and "warning" in warn_block[1], (
        "alert-warn must reference the --warning colour variable"
    )


def test_alert_info_defined_in_stylesheet():
    """alert-info class must be defined; used for flash messages."""
    css = (ROOT / "static/style.css").read_text()
    assert ".alert-info" in css, (
        "alert-info CSS class must be defined; flash messages use it."
    )


def test_all_template_dialogs_use_shared_modal_styles():
    class DialogParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.dialogs = []

        def handle_starttag(self, tag, attrs):
            if tag == "dialog":
                self.dialogs.append(dict(attrs))

    dialogs = []
    for template in (ROOT / "templates").rglob("*.html"):
        parser = DialogParser()
        parser.feed(template.read_text())
        for attrs in parser.dialogs:
            assert "modal" in (attrs.get("class") or "").split(), template
            dialogs.append(attrs)
    assert dialogs, "Expected the shipment creation dialog"
