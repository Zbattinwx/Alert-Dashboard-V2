"""
Branding configuration for white-label support.
Allows switching between different brand configurations (ONW, Battin Front, etc.)
"""

import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field


class ColorScheme(BaseModel):
    """Color scheme for a brand."""
    primary: str = Field(default="#1a5fb4", description="Primary brand color")
    secondary: str = Field(default="#26a269", description="Secondary brand color")
    accent: str = Field(default="#e66100", description="Accent color")
    background: str = Field(default="#1e1e1e", description="Background color")
    surface: str = Field(default="#2d2d2d", description="Surface/card background")
    text: str = Field(default="#ffffff", description="Primary text color")
    text_secondary: str = Field(default="#b0b0b0", description="Secondary text color")
    success: str = Field(default="#26a269", description="Success color")
    warning: str = Field(default="#e5a50a", description="Warning color")
    error: str = Field(default="#c01c28", description="Error color")

    # Alert-specific colors (can override defaults)
    tornado_warning: str = Field(default="#FF0000", description="Tornado warning color")
    severe_thunderstorm: str = Field(default="#FFA500", description="Severe T-Storm color")
    flash_flood: str = Field(default="#8B0000", description="Flash flood color")
    winter_storm: str = Field(default="#FF69B4", description="Winter storm color")


class FontConfig(BaseModel):
    """Font configuration for a brand."""
    heading: str = Field(default="Roboto Condensed", description="Heading font family")
    body: str = Field(default="Open Sans", description="Body text font family")
    monospace: str = Field(default="JetBrains Mono", description="Monospace font family")
    heading_weight: str = Field(default="700", description="Heading font weight")
    body_weight: str = Field(default="400", description="Body font weight")


class TickerConfig(BaseModel):
    """Ticker widget configuration for a brand."""
    show_logo: bool = Field(default=True, description="Show logo in ticker")
    logo_position: str = Field(default="left", description="Logo position: left, right")
    sponsor_logo: Optional[str] = Field(default=None, description="Sponsor logo filename")
    scroll_speed_ms: int = Field(default=10000, description="Ticker scroll duration in ms")
    background_color: Optional[str] = Field(default=None, description="Override background")
    text_color: Optional[str] = Field(default=None, description="Override text color")


class BrandConfig(BaseModel):
    """Complete brand configuration."""
    name: str = Field(description="Full brand name")
    short_name: str = Field(description="Short/abbreviated name")
    tagline: Optional[str] = Field(default=None, description="Brand tagline")

    # Assets
    logo: str = Field(default="logo.png", description="Main logo filename")
    logo_dark: Optional[str] = Field(default=None, description="Dark mode logo")
    favicon: str = Field(default="favicon.ico", description="Favicon filename")
    og_image: Optional[str] = Field(default=None, description="OpenGraph image")

    # Styling
    colors: ColorScheme = Field(default_factory=ColorScheme)
    fonts: FontConfig = Field(default_factory=FontConfig)

    # Widget configurations
    ticker: TickerConfig = Field(default_factory=TickerConfig)

    # URLs
    website_url: Optional[str] = Field(default=None, description="Brand website URL")
    social_twitter: Optional[str] = Field(default=None, description="Twitter/X handle")
    social_facebook: Optional[str] = Field(default=None, description="Facebook page")
    social_youtube: Optional[str] = Field(default=None, description="YouTube channel")

    # Footer/Attribution
    copyright_text: Optional[str] = Field(default=None, description="Copyright text")
    powered_by_text: str = Field(
        default="Powered by Alert Dashboard V2",
        description="Powered by attribution"
    )

    def get_asset_path(self, asset_name: str, brands_dir: Path) -> Path:
        """Get full path to a brand asset, with fallback to default."""
        brand_asset = brands_dir / self.short_name.lower() / asset_name
        if brand_asset.exists():
            return brand_asset
        # Fallback to default brand assets
        default_asset = brands_dir / "default" / asset_name
        if default_asset.exists():
            return default_asset
        return brand_asset  # Return expected path even if missing

    def to_css_variables(self) -> dict[str, str]:
        """Convert brand config to CSS custom properties."""
        css_vars = {}

        # Colors
        for color_name, color_value in self.colors.model_dump().items():
            css_name = f"--brand-{color_name.replace('_', '-')}"
            css_vars[css_name] = color_value

        # Fonts
        css_vars["--font-heading"] = self.fonts.heading
        css_vars["--font-body"] = self.fonts.body
        css_vars["--font-mono"] = self.fonts.monospace
        css_vars["--font-heading-weight"] = self.fonts.heading_weight
        css_vars["--font-body-weight"] = self.fonts.body_weight

        return css_vars

    def to_css_string(self) -> str:
        """Generate CSS :root block with brand variables."""
        css_vars = self.to_css_variables()
        lines = [":root {"]
        for name, value in css_vars.items():
            lines.append(f"  {name}: {value};")
        lines.append("}")
        return "\n".join(lines)


# Default brand configurations
DEFAULT_BRANDS = {
    "default": BrandConfig(
        name="Alert Dashboard",
        short_name="Dashboard",
        tagline="Real-time Weather Alerts",
        colors=ColorScheme(),
        fonts=FontConfig(),
    ),
    "onw": BrandConfig(
        name="Ohio News & Weather",
        short_name="ONW",
        tagline="Your Local Severe Weather Source",
        logo="onw_logo.png",
        colors=ColorScheme(
            primary="#1565c0",
            secondary="#00897b",
            accent="#ff6f00",
        ),
        fonts=FontConfig(
            heading="Roboto Condensed",
            body="Roboto",
        ),
        ticker=TickerConfig(
            show_logo=True,
            sponsor_logo="canopy_sponsor.png",
        ),
        website_url="https://ohionewsandweather.com",
        social_twitter="@ohionewswx",
    ),
    "battinfront": BrandConfig(
        name="The Battin Front",
        short_name="TBF",
        tagline="Weather Coverage You Can Trust",
        logo="tbf_logo.png",
        colors=ColorScheme(
            primary="#2563eb",
            secondary="#059669",
            accent="#dc2626",
            background="#0f172a",
            surface="#1e293b",
        ),
        fonts=FontConfig(
            heading="Inter",
            body="Inter",
        ),
        website_url="https://thebattinfront.com",
    ),
}


def load_brand_from_file(brand_file: Path) -> BrandConfig:
    """Load brand configuration from JSON file."""
    with open(brand_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    return BrandConfig(**data)


def save_brand_to_file(brand: BrandConfig, brand_file: Path) -> None:
    """Save brand configuration to JSON file."""
    brand_file.parent.mkdir(parents=True, exist_ok=True)
    with open(brand_file, "w", encoding="utf-8") as f:
        json.dump(brand.model_dump(), f, indent=2)


@lru_cache
def get_brand_config(brand_name: str = "default", config_dir: Optional[Path] = None) -> BrandConfig:
    """
    Get brand configuration by name.

    Looks for brand config in:
    1. config/brands/{brand_name}.json
    2. Built-in DEFAULT_BRANDS
    3. Falls back to 'default' brand
    """
    # Try loading from file first
    if config_dir is None:
        config_dir = Path("config/brands")

    brand_file = config_dir / f"{brand_name}.json"
    if brand_file.exists():
        try:
            return load_brand_from_file(brand_file)
        except Exception as e:
            print(f"Warning: Failed to load brand '{brand_name}' from file: {e}")

    # Try built-in brands
    if brand_name in DEFAULT_BRANDS:
        return DEFAULT_BRANDS[brand_name]

    # Fallback to default
    print(f"Warning: Brand '{brand_name}' not found, using default")
    return DEFAULT_BRANDS["default"]


def reload_brand_config(brand_name: str = "default") -> BrandConfig:
    """Reload brand configuration (clears cache)."""
    get_brand_config.cache_clear()
    return get_brand_config(brand_name)


def list_available_brands(config_dir: Optional[Path] = None) -> list[str]:
    """List all available brand names."""
    brands = set(DEFAULT_BRANDS.keys())

    if config_dir is None:
        config_dir = Path("config/brands")

    if config_dir.exists():
        for brand_file in config_dir.glob("*.json"):
            brands.add(brand_file.stem)

    return sorted(brands)
