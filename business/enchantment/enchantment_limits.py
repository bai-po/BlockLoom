"""Native control limits; original glint channels follow local.31's interface."""

from ...util.property_limits import update
from .enchantment import (
    DEFAULT_BRIGHTNESS,
    DEFAULT_COLOR,
    DEFAULT_PIXEL_BRIGHTNESS_MULTIPLIER,
)


def refresh(transaction, owner):
    limits = {
        "glint_color": {"subtype": "COLOR", "min": 0, "max": 1, "default": DEFAULT_COLOR},
        "glint_brightness": {"min": 0, "max": 10, "soft_min": 0, "soft_max": 10, "default": DEFAULT_BRIGHTNESS},
        "glint_gradient": {"min": 0, "max": 1},
        "glint_pixel_size": {"min": 1, "max": 16, "default": 1},
        "glint_pixel_speed": {"min": 0, "max": 10, "default": 1.0},
        "glint_pixel_brightness_multiplier": {"min": 0, "max": 100, "soft_min": 0, "soft_max": 10,
                                              "default": DEFAULT_PIXEL_BRIGHTNESS_MULTIPLIER},
        "glint_outline_color": {"subtype": "COLOR", "min": 0, "max": 1},
        "glint_outline_width": {"min": 0, "max": 0.25, "soft_min": 0, "soft_max": 0.1, "default": 0.006},
        "glint_outline_brightness": {"min": 0, "max": 10, "soft_min": 0, "soft_max": 4},
        "glint_geometry_outer": {"min": 0, "max": 1},
        "glint_geometry_inner": {"min": 0, "max": 1},
        "glint_geometry_random_factor": {"min": 0, "max": 1},
        "glint_geometry_height_ratio": {"min": 0, "max": 5},
        "glint_geometry_seed": {"min": 0, "max": 100000},
        "glint_geometry_center_falloff": {"min": 0, "max": 1},
        "glint_geometry_center_range": {"min": 0, "max": 5},
    }
    update(transaction, owner, limits)
