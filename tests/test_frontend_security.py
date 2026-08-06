from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_frontend_does_not_persist_secrets_or_render_untrusted_html():
    javascript = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
    assert "innerHTML" not in javascript
    assert 'localStorage.setItem("ot_api_key"' not in javascript
    assert 'localStorage.setItem("ot_accounts"' not in javascript
    assert "passwordInput" not in javascript


def test_frontend_has_accessible_controls_and_ai_warning():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert 'href="#workspace"' in html
    assert 'aria-live="polite"' in html
    assert "AI can make mistakes" in html
    assert '<script src="/static/app.js" defer></script>' in html
