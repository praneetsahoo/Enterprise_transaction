"""Phase 8: the Streamlit dashboard renders without errors (Streamlit's own headless AppTest)."""
from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest
from sqlalchemy import create_engine

AppTest = pytest.importorskip("streamlit.testing.v1").AppTest
APP = str(Path(__file__).resolve().parents[1] / "app" / "dashboard.py")


@pytest.fixture(autouse=True)
def fresh_streamlit_cache(monkeypatch):
    """st.cache_data is process-wide; clear it so one test's results never leak into another.
    Also run without the login gate unless a test sets a password itself (the server's env has one)."""
    import streamlit as st

    from app.config import dashboard_password

    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    monkeypatch.delenv("DASHBOARD_PASSWORD_PARAM", raising=False)
    dashboard_password.cache_clear()
    st.cache_data.clear()
    st.cache_resource.clear()
    yield
    st.cache_data.clear()
    dashboard_password.cache_clear()


TABS = ["Overview", "Upload & run", "Settlement", "Fraud alerts", "Dead-letter queue", "Pipeline runs"]


def test_without_a_database_the_dashboard_shows_a_clear_error_not_a_crash():
    unreachable = create_engine("mysql+pymysql://nobody:nothing@127.0.0.1:1/none",
                                connect_args={"connect_timeout": 2})
    with mock.patch("app.database.connection.get_engine", return_value=unreachable):
        at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception
    assert "Cannot reach the database" in at.error[0].value
    assert "nothing" not in at.error[0].value                       # password never shown


@pytest.mark.live
def test_dashboard_renders_every_tab_against_the_database(engine):
    at = AppTest.from_file(APP, default_timeout=60).run()
    assert not at.exception, at.exception
    assert [t.label for t in at.tabs] == TABS
    assert not [e.value for e in at.error]


@pytest.mark.live
def test_fraud_what_if_sliders_rerun_the_sql(engine):
    at = AppTest.from_file(APP, default_timeout=60).run()
    at.slider[1].set_value(20).run()                                # slider max: nobody has > 20 failures
    assert not at.exception
    assert any("No user exceeds the threshold" in s.value for s in at.success)


def _unreachable():
    return create_engine("mysql+pymysql://nobody:nothing@127.0.0.1:1/none", connect_args={"connect_timeout": 2})


def test_login_gate_blocks_until_the_right_password(monkeypatch):
    from app.config import dashboard_password

    monkeypatch.setenv("DASHBOARD_PASSWORD", "correct-horse")
    dashboard_password.cache_clear()
    try:
        with mock.patch("app.database.connection.get_engine", return_value=_unreachable()):
            at = AppTest.from_file(APP, default_timeout=30).run()
            assert not at.tabs and len(at.text_input) == 1          # nothing visible before login
            at.text_input[0].input("wrong")
            at.button[0].click().run()
            assert at.error[0].value == "Wrong password." and not at.tabs
            at.text_input[0].input("correct-horse")
            at.button[0].click().run()
            assert not at.exception
            assert "Cannot reach the database" in at.error[0].value  # past the gate (no DB in this test)
    finally:
        dashboard_password.cache_clear()
