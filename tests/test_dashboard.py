"""Phase 8: the Streamlit dashboard renders without errors (Streamlit's own headless AppTest)."""
from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest
from sqlalchemy import create_engine

AppTest = pytest.importorskip("streamlit.testing.v1").AppTest
APP = str(Path(__file__).resolve().parents[1] / "app" / "dashboard.py")
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
