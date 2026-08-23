from __future__ import annotations

import pytest
from PIL import ImageFont
from pydantic import ValidationError

from ride_overlay_dashboard import DashboardConfig, FrameRenderer


def numeric_config(**overrides) -> DashboardConfig:
    raw = {
        "id": "speed",
        "source": "speed",
        "font_size": 32,
        "anchor": {"x": 0.5, "y": 0.5},
        "align": "center",
        "color": "#FFFFFFFF",
    }
    raw.update(overrides)
    return DashboardConfig.model_validate(raw)


def renderer() -> FrameRenderer:
    result = FrameRenderer.__new__(FrameRenderer)
    result.width = 200
    result.height = 100
    result.numeric_text_layers = {}
    result._font = lambda size: ImageFont.load_default(size=size)  # type: ignore[method-assign]
    return result


def test_numeric_stroke_fields_are_no_longer_accepted() -> None:
    with pytest.raises(ValidationError, match="stroke_width"):
        numeric_config(stroke_width=2)
    with pytest.raises(ValidationError, match="stroke_color"):
        numeric_config(stroke_color="#000000FF")


def test_shadow_configuration_is_validated() -> None:
    dashboard = numeric_config(
        shadow={
            "color": "#12345680",
            "offset_x": -3.5,
            "offset_y": 7,
            "blur_radius": 8.5,
            "opacity": 0.4,
        }
    )

    assert dashboard.shadow is not None
    assert dashboard.shadow.color == "#12345680"
    assert dashboard.shadow.offset_x == pytest.approx(-3.5)
    assert dashboard.shadow.blur_radius == pytest.approx(8.5)

    with pytest.raises(ValidationError):
        numeric_config(shadow={"opacity": 1.01})
    with pytest.raises(ValidationError):
        numeric_config(shadow={"blur_radius": -1})


def test_shadow_expands_visual_bounds_and_multiplies_alpha() -> None:
    text_renderer = renderer()
    plain_image, plain_origin = text_renderer._numeric_text_layer(
        numeric_config(),
        "88",
    )
    shadow_image, shadow_origin = text_renderer._numeric_text_layer(
        numeric_config(
            id="shadowed",
            shadow={
                "color": "#FF000080",
                "offset_x": 40,
                "offset_y": 0,
                "blur_radius": 0,
                "opacity": 0.5,
            },
        ),
        "88",
    )

    assert shadow_origin[0] == plain_origin[0]
    assert shadow_image.width > plain_image.width
    pixels = shadow_image.tobytes()
    red_shadow_alpha = [
        pixels[index + 3]
        for index in range(0, len(pixels), 4)
        if pixels[index] > 0 and pixels[index + 1] == 0 and pixels[index + 2] == 0
    ]
    assert red_shadow_alpha
    assert max(red_shadow_alpha) == 64


def test_blurred_text_layer_is_reused_until_its_content_changes() -> None:
    text_renderer = renderer()
    dashboard = numeric_config(
        shadow={
            "color": "#000000FF",
            "offset_x": 4,
            "offset_y": 4,
            "blur_radius": 8,
            "opacity": 0.75,
        }
    )

    first_image, first_origin = text_renderer._numeric_text_layer(dashboard, "42")
    second_image, second_origin = text_renderer._numeric_text_layer(dashboard, "42")
    changed_image, _changed_origin = text_renderer._numeric_text_layer(dashboard, "43")

    assert second_image is first_image
    assert second_origin == first_origin
    assert changed_image is not first_image
