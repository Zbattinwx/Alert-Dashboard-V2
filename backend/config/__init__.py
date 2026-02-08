"""Configuration management for Alert Dashboard V2."""

from .settings import Settings, get_settings
from .branding import BrandConfig, get_brand_config

__all__ = ["Settings", "get_settings", "BrandConfig", "get_brand_config"]
