"""Shared Playwright requirement for the browser suites (F24).

WHY THIS EXISTS
---------------
Every browser suite in this repo begins with::

    playwright_api = pytest.importorskip('playwright.sync_api', ...)

That is deliberate and correct for a developer running the suite locally
without browser extras — the module skips and the rest of the suite still runs.
It is wrong for CI, where "skipped" and "passed" must not be the same outcome:
a green build would then silently assert nothing about keyboard focus,
stored-XSS rendering, or the CSRF header the chat widget actually sends.

``ci-cd.yml`` previously guarded only ONE suite (``test_edit_profile_picker_browser``)
by grepping the log for a skip. The other six were reachable through
``pytest tests/`` with no enforcement at all.

THE MECHANISM
-------------
:func:`require_playwright` skips exactly as before when Playwright is absent,
UNLESS ``FRAMEIQ_REQUIRE_BROWSER=1``, in which case it raises. ``ci-cd.yml``
sets that variable on the test job, so:

* locally            -> unchanged behaviour (skip, developer keeps working)
* in CI              -> a missing Playwright/Chromium FAILS with an
  informative message naming the suite and the install command, instead of
  vanishing into a skip count.

Why this rather than parsing the pytest log for ``SKIPPED``: log-scraping is
exactly the fragile approach this replaces, and it cannot distinguish "the
dependency is missing" from "this test legitimately does not apply". Both of
those reasons are observable here. The failure is raised at the point of
import, so pytest reports it as an error on that file with a real traceback.

SCOPE
-----
This guards the *driver import* only. It deliberately does not convert
per-test ``pytest.skip`` calls into failures; none of the browser suites use
any, and if one legitimately needed to, that must not be swept up by a
dependency guard. Set ``FRAMEIQ_REQUIRE_BROWSER=1`` in CI only.
"""
import os

#: Set to "1" by CI. Local runs leave it unset and keep the skip behaviour.
ENV_REQUIRE = 'FRAMEIQ_REQUIRE_BROWSER'

_INSTALL_HINT = (
    'Install it with `pip install playwright` and then download the browser '
    'with `python -m playwright install --with-deps chromium`.'
)


def browser_required():
    """True when the environment demands that Playwright not be skipped."""
    return os.environ.get(ENV_REQUIRE, '').strip() == '1'


def require_playwright(suite_name):
    """Import and return ``playwright.sync_api``, or skip/fail as appropriate.

    ``suite_name`` is used only for the error message, so a failing CI run says
    which suite lost its coverage instead of reporting seven identical errors.
    """
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # noqa: BLE001 - any import failure is the same story
        message = (
            f'{suite_name}: Playwright is not importable ({exc}). '
            f'{_INSTALL_HINT}'
        )
        if browser_required():
            # Raise rather than exit: pytest attributes this to the importing
            # module and reports a genuine failure with a traceback.
            raise RuntimeError(
                f'{ENV_REQUIRE}=1 is set, so this browser suite is REQUIRED and '
                f'must not be skipped. {message}'
            ) from exc
        import pytest
        pytest.skip(message, allow_module_level=True)
    return sync_playwright
