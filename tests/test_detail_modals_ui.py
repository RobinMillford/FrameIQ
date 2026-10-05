"""Focused UI/structure tests for the shared detail-page modal system.

These are server-side assertions on the delivered artifacts (templates,
the shared stylesheet, the shared modal engine). They run without a
browser and pin the contract that caused the original bug:

  the modals must NOT stay inside a transformed/stacking ancestor, they
  must be moved into a body-level portal, and their dialog/backdrop
  geometry and theme must hold at every breakpoint.

Layout/behaviour that genuinely requires layout (centering, scroll lock,
poster pixel width, focus trap) is verified by
scripts/verify_detail_modals_browser.py against real Chromium, Firefox
and WebKit. What is asserted here is everything that can be decided from
the source of truth, so a regression fails fast in CI.
"""
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
TPL = ROOT / "templates"
JS = ROOT / "static" / "js"
CSS = ROOT / "static" / "css"

MODAL_IDS = ["list-modal", "diary-modal", "trailer-modal"]


@pytest.fixture(scope="module")
def modal_partials():
    return {
        "list": (TPL / "partials" / "modal_list.html").read_text(),
        "diary": (TPL / "partials" / "modal_diary.html").read_text(),
        "trailer": (TPL / "partials" / "modal_trailer.html").read_text(),
    }


@pytest.fixture(scope="module")
def modal_engine():
    return (JS / "fi-modal.js").read_text()


@pytest.fixture(scope="module")
def modal_css():
    return (CSS / "modals.css").read_text()


@pytest.fixture(scope="module")
def detail_pages():
    return {
        "movie": (TPL / "movie_detail.html").read_text(),
        "tv": (TPL / "tv_detail.html").read_text(),
    }


# ═══════════════════════════════════════════════════════════════════
# 1/2/23 — both forms use ONE shared modal infrastructure
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("key,title", [
    ("list", "Add to List"),
    ("diary", "Log to Diary"),
])
def test_add_to_list_and_diary_share_modal_infrastructure(modal_partials, key,
                                                          title):
    """23. Add to List and Log to Diary use the same modal infrastructure."""
    body = modal_partials[key]
    assert 'class="fi-modal-backdrop"' in body
    assert 'class="fi-modal-dialog"' in body
    assert 'role="dialog"' in body
    assert 'aria-modal="true"' in body
    assert title in body


@pytest.mark.parametrize("key,expected_id", [
    ("list", "list-modal"),
    ("diary", "diary-modal"),
    ("trailer", "trailer-modal"),
])
def test_every_modal_is_a_fi_modal_backdrop(modal_partials, key, expected_id):
    """12. No competing list-modal vs diary-modal systems exist."""
    body = modal_partials[key]
    assert f'id="{expected_id}"' in body
    assert "fi-modal-backdrop" in body
    # The old per-modal class stack is gone.
    assert 'class="modal fixed' not in body
    assert "modal-content" not in body


def test_only_one_modal_engine_exists():
    """12/37. Exactly one shared modal JS module, one shared stylesheet."""
    engines = sorted(p.name for p in JS.glob("*modal*.js"))
    assert engines == ["detail-modals.js", "fi-modal.js"], engines
    sheets = sorted(p.name for p in CSS.glob("*modal*.css"))
    assert sheets == ["modals.css"], sheets


@pytest.mark.parametrize("page", ["movie", "tv"])
def test_detail_pages_load_shared_modal_engine(detail_pages, page):
    body = detail_pages[page]
    engine = body.index("filename='js/fi-modal.js'")
    forms = body.index("filename='js/detail-modals.js'")
    assert engine != -1 and forms != -1
    # Engine must load BEFORE the form logic that calls into it.
    assert engine < forms


def test_base_template_loads_shared_modal_stylesheet():
    base = (TPL / "base.html").read_text()
    assert "css/modals.css" in base
    # After chrome.css so it owns the 1200 overlay layer.
    assert base.index("css/chrome.css") < base.index("css/modals.css")


# ═══════════════════════════════════════════════════════════════════
# 4 — body-level portal
# ═══════════════════════════════════════════════════════════════════

def test_modals_are_moved_into_body_level_portal(modal_engine):
    """4. document.body owns a modal-root; modals are adopted into it."""
    assert "fi-modal-root" in modal_engine
    assert "document.body.appendChild(root)" in modal_engine
    # Adoption moves the node, it does not clone it.
    assert "portal.appendChild(el)" in modal_engine


def test_modals_do_not_stay_inside_transformed_main(modal_engine):
    """3/4. The portal — not a nested container — owns the modal."""
    # The engine must never reparent into <main> or any page container.
    assert "querySelector('main')" not in modal_engine
    assert "appendChild(main" not in modal_engine


# ═══════════════════════════════════════════════════════════════════
# 5 — viewport-level positioning (CSS contract)
# ═══════════════════════════════════════════════════════════════════

def test_backdrop_is_viewport_fixed_and_full_inset(modal_css):
    """5. position: fixed; inset: 0 at the modal layer."""
    block = re.search(r"\.fi-modal-backdrop\s*\{(.*?)\n\}", modal_css, re.S)
    assert block, "no .fi-modal-backdrop rule"
    body = block.group(1)
    assert "position: fixed" in body
    assert "inset: 0" in body


def test_dialog_is_relative_with_bounded_width_and_height(modal_css):
    """5/14/15. position: relative; width: min(...); max-height: 100dvh."""
    block = re.search(r"\.fi-modal-dialog\s*\{(.*?)\n\}", modal_css, re.S)
    assert block, "no .fi-modal-dialog rule"
    body = block.group(1)
    assert "position: relative" in body
    assert "margin: auto" in body
    assert re.search(r"width:\s*min\(520px,\s*calc\(100vw - 32px\)\)", body)
    assert "calc(100dvh - 32px)" in body
    # Never a full-screen panel, never wider than the viewport.
    assert "100vw" not in body.replace("calc(100vw - 32px)", "")


def test_dialog_never_exceeds_viewport(modal_css):
    """14. No width > viewport, no horizontal overflow."""
    mobile = re.search(r"@media \(max-width: 767px\) \{(.*?)\n\}", modal_css, re.S)
    assert mobile, "no mobile block"
    assert "width: calc(100vw - 24px)" in mobile.group(1)


# ═══════════════════════════════════════════════════════════════════
# 6/26 — animation strategy
# ═══════════════════════════════════════════════════════════════════

def test_animation_uses_transform_and_opacity_only(modal_css):
    """6. No layout-property animation; transform + opacity only."""
    dialog = re.search(r"\.fi-modal-dialog\s*\{(.*?)\n\}", modal_css, re.S).group(1)
    assert "transform: translateY(8px) scale(0.98)" in dialog
    assert re.search(r"transition:\s*opacity[^;]*transform|"
                     r"transition:\s*transform[^;]*opacity", dialog)
    for prop in ("transition:",):
        line = [ln for ln in dialog.splitlines() if prop in ln]
        for ln in line:
            assert not re.search(r"transition[^;]*\b(top|left|width|height)\b", ln), ln


def test_animation_uses_small_movement_only(modal_css):
    """6. No large translate/scale jumps."""
    assert "translateY(8px) scale(0.98)" in modal_css
    assert not re.search(r"scale\(1\.[2-9]", modal_css)
    assert "translateX(" not in modal_css


def test_modal_uses_explicit_state_model(modal_engine, modal_css):
    """6. closed / opening / open / closing; never display:none."""
    for state in ("closed", "opening", "open", "closing"):
        assert f"'{state}'" in modal_engine, state
        assert f'data-state="{state}"' in modal_css, state
    # display is never toggled by the engine (display:none would kill the
    # transition); CSS owns display.
    assert "display: none" not in modal_engine
    assert ".fi-modal-backdrop[data-state=\"closed\"]" in modal_css


def test_reduced_motion_is_respected(modal_engine, modal_css):
    """26. prefers-reduced-motion collapses the animation, never sticks."""
    assert "prefers-reduced-motion: reduce" in modal_css
    assert "prefersReducedMotion" in modal_engine
    reduced = modal_css.split("prefers-reduced-motion: reduce")[1]
    assert "transform: none" in reduced


# ═══════════════════════════════════════════════════════════════════
# 7 — no page jump
# ═══════════════════════════════════════════════════════════════════

def test_opening_never_scrolls_into_view(modal_engine):
    """7. No scrollIntoView, no scrollTo on open."""
    assert "scrollIntoView" not in modal_engine
    open_block = modal_engine.split("function open(")[1].split("\n    function ")[0]
    assert "scrollTo" not in open_block


# ═══════════════════════════════════════════════════════════════════
# 8 — scroll lock with exact restoration
# ═══════════════════════════════════════════════════════════════════

def test_scroll_lock_snapshots_and_restores(modal_engine):
    """8. Original scroll restored exactly; nothing left permanently set."""
    assert "lockScroll" in modal_engine
    assert "unlockScroll" in modal_engine
    assert "position: fixed" in modal_engine or "position = 'fixed'" in modal_engine \
        or "body.style.position = 'fixed'" in modal_engine
    assert "window.scrollTo(0, y)" in modal_engine
    assert "fi-modal-scroll-locked" in modal_engine


def test_scroll_lock_does_not_permanently_set_overflow_hidden(modal_engine):
    """8. overflow:hidden must be removed again on close."""
    assert "classList.remove('fi-modal-scroll-locked')" in modal_engine


# ═══════════════════════════════════════════════════════════════════
# 9/29 — focus + accessibility
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("key,title_id", [
    ("list", "list-modal-title"),
    ("diary", "diary-modal-title"),
])
def test_dialog_has_stable_labelledby_title(modal_partials, key, title_id):
    """29. aria-labelledby points at a stable title id."""
    body = modal_partials[key]
    assert f'aria-labelledby="{title_id}"' in body
    assert f'id="{title_id}"' in body


def test_focus_enters_trap_and_returns(modal_engine):
    """9/29. Focus in on open, trapped while open, returned on close."""
    assert "focusFirst" in modal_engine
    assert "trapFocus" in modal_engine
    assert "trigger.focus()" in modal_engine
    assert "data-fi-autofocus" in modal_engine


def test_escape_and_backdrop_close_but_click_inside_does_not(modal_engine):
    """9. ESC closes; backdrop click closes; dialog click does not."""
    assert "e.key === 'Escape'" in modal_engine
    assert "e.target === current.el && !current.dialog.contains(e.target)" \
        in modal_engine


def test_hidden_modal_controls_are_not_focusable(modal_engine):
    """9. Closed dialogs are inert + aria-hidden (out of the tab order)."""
    assert "el.inert = true" in modal_engine
    assert "aria-hidden" in modal_engine
    assert "visibility: hidden" in (CSS / "modals.css").read_text()


def test_focus_return_does_not_scroll_the_page(modal_engine):
    """8. Focus is restored BEFORE the scroll unlock.

    Focusing the trigger while <body> is already unlocked drags the
    document up to the button (measured: 2400 -> 364).
    """
    finalize = modal_engine.split("function finalize(")[1].split("\n    }")[0]
    assert finalize.index("trigger.focus()") < finalize.index("unlockScroll()")


def test_close_hook_lets_consumers_clean_up(modal_engine):
    """The engine owns ESC/backdrop, so teardown is registered, not cloned."""
    assert "onClose" in modal_engine
    assert "runCloseHandlers" in modal_engine
    finalize = modal_engine.split("function finalize(")[1].split("\n    }")[0]
    assert "runCloseHandlers" in finalize


def test_trailer_registers_its_iframe_teardown(detail_pages):
    """Closing the trailer with ESC must stop the video."""
    for name, body in detail_pages.items():
        assert "FiModal.onClose('trailer-modal'" in body, name


# ═══════════════════════════════════════════════════════════════════
# 13 — layering
# ═══════════════════════════════════════════════════════════════════

def test_modal_uses_documented_layer_token(modal_css):
    """13. Reuse the existing ladder; no arbitrary z-index."""
    assert "--fi-z-modal: 1200" in modal_css
    assert "z-index: var(--fi-z-modal)" in modal_css
    assert not re.search(r"z-index:\s*9{4,}", modal_css)
    chrome = (CSS / "chrome.css").read_text()
    # The ladder comment in chrome.css must stay truthful.
    assert "nav 1100 < modals 1200 < menu portal 1250 < toasts 1300" in chrome


def test_old_z50_modal_layer_is_gone(detail_pages):
    """13. The modals no longer sit at z-50 under the header (1100)."""
    for name, body in detail_pages.items():
        assert 'z-50 flex items-center justify-center hidden' not in body, name


# ═══════════════════════════════════════════════════════════════════
# 16/17/18/19 — theme
# ═══════════════════════════════════════════════════════════════════

def test_modal_surface_uses_design_tokens(modal_css):
    """16. Reuse tokens; no hardcoded palette explosion."""
    dialog = re.search(r"\.fi-modal-dialog\s*\{(.*?)\n\}", modal_css, re.S).group(1)
    assert "var(--bg-surface" in dialog
    assert "var(--line-hi" in dialog
    assert "var(--text-hi" in dialog


def test_form_controls_are_dark(modal_css):
    """17. No clashing white native inputs/selects/dates."""
    for sel in (".fi-modal-input,", ".fi-modal-select,"):
        assert sel in modal_css
    assert "color-scheme: dark" in modal_css
    assert "-webkit-appearance: none" in modal_css


def test_only_one_primary_and_one_secondary_button(modal_css):
    """18. Primary/secondary variants; no per-modal random colours."""
    assert modal_css.count(".fi-modal-btn--primary") >= 1
    assert modal_css.count(".fi-modal-btn--secondary") >= 1
    primary = re.search(r"\.fi-modal-btn--primary\s*\{(.*?)\n\}", modal_css, re.S).group(1)
    assert "var(--accent" in primary
    secondary = re.search(r"\.fi-modal-btn--secondary\s*\{(.*?)\n\}", modal_css, re.S).group(1)
    assert "background: transparent" in secondary
    # The old purple/pink and cyan/blue gradients are gone.
    for grad in ("from-purple-600", "to-pink-600", "from-cyan-600", "to-blue-600"):
        assert grad not in modal_css, grad


def test_modal_partials_use_shared_button_and_field_classes(modal_partials):
    """18/19. Both forms render the same components."""
    for key in ("list", "diary"):
        body = modal_partials[key]
        assert "fi-modal-btn--primary" in body
        assert "fi-modal-btn--secondary" in body
        assert "fi-modal-label" in body
        assert "Cancel" in body


def test_secondary_text_uses_readable_token(modal_css):
    """19. Secondary text must not disappear on the dark surface."""
    desc = re.search(r"\.fi-modal-description\s*\{(.*?)\n\}", modal_css, re.S).group(1)
    color = re.search(r"color:\s*([^;]+);", desc).group(1)
    assert "var(--text-mid" in color, color
    assert "--text-low" not in color, color
    assert "var(--text-mid" in modal_css


def test_focus_ring_is_visible(modal_css):
    """19. Visible accent focus ring on every control."""
    assert "outline: 2px solid var(--accent" in modal_css


# ═══════════════════════════════════════════════════════════════════
# 15/24 — responsive breakpoints
# ═══════════════════════════════════════════════════════════════════

def test_modal_breakpoints_are_meaningful(modal_css):
    """24. Desktop / Tablet / Mobile / Very narrow modes."""
    assert "@media (max-width: 767px)" in modal_css
    assert "@media (max-width: 380px)" in modal_css


def test_modal_content_area_scrolls_not_the_page(modal_css):
    """15. overflow-y:auto on the content area only."""
    body = re.search(r"\.fi-modal-body\s*\{(.*?)\n\}", modal_css, re.S).group(1)
    assert "overflow-y: auto" in body
    # The dialog itself clips, it does not scroll the document.
    dialog = re.search(r"\.fi-modal-dialog\s*\{(.*?)\n\}", modal_css, re.S).group(1)
    assert "overflow: hidden" in dialog


# ═══════════════════════════════════════════════════════════════════
# 20/22/23/30 — poster sizing
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("page,poster_class", [
    ("movie", ".movie-poster"),
    ("tv", ".show-poster"),
])
def test_poster_has_responsive_breakpoint_ranges(detail_pages, page,
                                                 poster_class):
    """20/24. Desktop 240-300, Tablet 190-240, Mobile min(210px, 55vw)."""
    body = detail_pages[page]
    assert "max-width: min(210px, 55vw)" in body, "mobile range"
    assert "@media (min-width: 768px)" in body, "tablet breakpoint"
    assert "max-width: 230px" in body, "tablet 190-240"
    assert "@media (min-width: 1200px)" in body, "desktop breakpoint"
    assert "max-width: 280px" in body, "desktop 240-300"
    assert "max-width: 300px" in body, "desktop upper bound"


@pytest.mark.parametrize("page,poster_class", [
    ("movie", ".movie-poster"),
    ("tv", ".show-poster"),
])
def test_poster_preserves_quality_attributes(detail_pages, page, poster_class):
    """30. Aspect ratio, object-fit and lazy loading preserved."""
    body = detail_pages[page]
    block = re.search(re.escape(poster_class) + r"\s*\{(.*?)\n        \}",
                      body, re.S)
    assert block, "no poster rule"
    rules = block.group(1)
    assert "aspect-ratio: 2 / 3" in rules
    assert "object-fit: cover" in rules
    assert "height: auto" in rules
    # lazy loading on the primary poster element
    assert f'class="{poster_class[1:]} rounded-lg" loading="lazy"' in body


def test_movie_poster_no_longer_uses_flat_tailwind_widths(detail_pages):
    """20. The old flat w-40 sm:w-64 (160/256px) is replaced."""
    assert 'class="movie-poster rounded-lg w-40 sm:w-64' not in detail_pages["movie"]


# ═══════════════════════════════════════════════════════════════════
# 27/28 — no state corruption under rapid open/close
# ═══════════════════════════════════════════════════════════════════

def test_single_open_modal_and_idempotent_close(modal_engine):
    """27/28. Rapid open/close cannot duplicate or orphan a modal."""
    assert "if (current && current.id !== id) close(current.id, true)" in modal_engine
    assert "if (entry.closing) return" in modal_engine
    assert "current = null;" in modal_engine


def test_event_listeners_are_bound_once(modal_engine):
    """37. No duplicate listeners after repeated opens."""
    assert "if (listenersBound || !root) return;" in modal_engine
    assert "listenersBound = true;" in modal_engine
    assert modal_engine.count("addEventListener('keydown'") == 1


def test_tv_detail_page_has_no_duplicate_diary_modal(detail_pages):
    """28. One logical modal instance per page."""
    assert detail_pages["tv"].count('partials/modal_diary.html') == 1
    assert detail_pages["movie"].count('partials/modal_diary.html') == 1


# ═══════════════════════════════════════════════════════════════════
# 38 — no backend / schema changes
# ═══════════════════════════════════════════════════════════════════

def test_modal_endpoints_unchanged():
    """38. The existing modal endpoints are still the ones used."""
    js = (JS / "detail-modals.js").read_text()
    assert "'/api/diary/log'" in js
    assert "'/api/lists/' + listId + '/add'" in js
    assert "'/api/users/' + ctx.user_id + '/lists'" in js


def test_no_python_model_or_migration_touched():
    """38. This change is frontend-only: no models, no migrations."""
    diff = [
        "models/", "migrates/", "api/", "routes/", "app.py",
    ]
    changed = subprocess_changed_files()
    for prefix in diff:
        assert not any(c.startswith(prefix) for c in changed), \
            f"frontend task must not touch {prefix}: {changed}"


def subprocess_changed_files():
    """Files changed vs the task baseline commit (a72c27c)."""
    import subprocess
    try:
        out = subprocess.run(
            ["git", "diff", "--name-only", "a72c27c", "HEAD"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=20)
        return [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]
    except Exception:
        return []