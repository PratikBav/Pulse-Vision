"""Smoke test: the dashboard script runs and the Arduino controls work (no webcam needed)."""

import os
import time

from streamlit.testing.v1 import AppTest

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")


def test_dashboard_loads_and_connects_mock():
    at = AppTest.from_file(APP, default_timeout=60).run()
    assert not at.exception, at.exception
    assert at.title[0].value == "PulseVision"
    assert any(t.label == "Start measurement" for t in at.toggle)

    at.sidebar.selectbox[0].select("MOCK (simulated)").run()
    next(b for b in at.sidebar.button if b.label == "Connect").click().run()
    assert not at.exception, at.exception
    time.sleep(1.5)
    at.run()
    captions = " ".join(c.value for c in at.sidebar.caption)
    assert "connected" in captions and "MOCK" in captions
    next(b for b in at.sidebar.button if b.label == "Disconnect").click().run()
    assert "disconnected" in " ".join(c.value for c in at.sidebar.caption)
