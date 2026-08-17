from __future__ import annotations

import ride_overlay
import ride_overlay_dashboard
import ride_overlay_data


def test_data_implementation_lives_in_data_module() -> None:
    assert ride_overlay.build_activity is ride_overlay_data.build_activity
    assert ride_overlay.read_fit is ride_overlay_data.read_fit
    assert ride_overlay.TimeSeries is ride_overlay_data.TimeSeries


def test_dashboard_implementation_lives_in_dashboard_module() -> None:
    assert ride_overlay.build_dashboard_runtimes is ride_overlay_dashboard.build_dashboard_runtimes
    assert ride_overlay.FrameRenderer is ride_overlay_dashboard.FrameRenderer
    assert ride_overlay.DashboardConfig is ride_overlay_dashboard.DashboardConfig
