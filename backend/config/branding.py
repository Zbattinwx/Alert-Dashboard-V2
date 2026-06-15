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

    # CSS variable overrides applied to :root at runtime
    css_overrides: dict[str, str] = Field(default_factory=dict, description="CSS custom property overrides")

    # Assets
    logo: str = Field(default="logo.png", description="Main logo filename")
    logo_dark: Optional[str] = Field(default=None, description="Dark mode logo")
    logo_is_wordmark: bool = Field(default=False, description="Logo already includes the brand name (clients show the logo alone, no separate name text)")
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
            primary="#7aa2f7",
            secondary="#bb9af7",
            accent="#ff9e64",
            background="#1a1d2e",
            surface="#2a2f41",
            text="#c0c5d5",
            text_secondary="#8892aa",
        ),
        fonts=FontConfig(
            heading="Roboto",
            body="Roboto",
        ),
        ticker=TickerConfig(
            show_logo=True,
            sponsor_logo="canopy_sponsor.png",
        ),
        website_url="https://ohionewsandweather.com",
        social_twitter="@ohionewswx",
        css_overrides={
            "--bg-primary": "#1a1d2e",
            "--bg-secondary": "#24283b",
            "--bg-tertiary": "#1e2235",
            "--bg-card": "#2a2f41",
            "--text-primary": "#c0c5d5",
            "--text-secondary": "#8892aa",
            "--text-muted": "#5c6a8a",
            "--border-color": "rgba(65, 72, 104, 0.6)",
            "--border-light": "rgba(65, 72, 104, 0.2)",
            "--accent-blue": "#7aa2f7",
            "--accent-cyan": "#7dcfff",
            "--accent-indigo": "#7aa2f7",
            "--accent-purple": "#bb9af7",
            "--accent-green": "#26a269",
            "--accent-red": "#c01c28",
            "--accent-yellow": "#e5a50a",
            "--accent-orange": "#ff9e64",
            "--primary-color": "#7aa2f7",
            "--primary-light": "#9db8f7",
            "--primary-dark": "#5a82d7",
            "--secondary-color": "#ff9e64",
            "--tbf-gradient": "linear-gradient(135deg, #7aa2f7 0%, #bb9af7 100%)",
            "--tbf-gradient-subtle": "linear-gradient(135deg, rgba(122, 162, 247, 0.15) 0%, rgba(187, 154, 247, 0.15) 100%)",
            "--storm-gradient": "linear-gradient(135deg, #7aa2f7 0%, #ff9e64 100%)",
        },
    ),
    "battinfront": BrandConfig(
        name="The Battin Front",
        short_name="TBF",
        tagline="Weather Coverage You Can Trust",
        logo="tbf_logo.png",
        colors=ColorScheme(
            primary="#00CED1",
            secondary="#9333EA",
            accent="#f97316",
            background="#06080f",
            surface="#0e1322",
        ),
        fonts=FontConfig(
            heading="Inter",
            body="Inter",
        ),
        website_url="https://thebattinfront.com",
        css_overrides={
            "--bg-primary": "#06080f",
            "--bg-secondary": "#0c1019",
            "--bg-tertiary": "#161c2e",
            "--bg-card": "#0e1322",
            "--text-primary": "#e2e8f0",
            "--text-secondary": "#94a3b8",
            "--text-muted": "#64748b",
            "--border-color": "rgba(255, 255, 255, 0.07)",
            "--border-light": "rgba(255, 255, 255, 0.03)",
            "--accent-blue": "#00CED1",
            "--accent-cyan": "#00CED1",
            "--accent-indigo": "#6366F1",
            "--accent-purple": "#9333EA",
            "--accent-green": "#10b981",
            "--accent-red": "#ef4444",
            "--accent-yellow": "#f59e0b",
            "--accent-orange": "#f97316",
            "--primary-color": "#00CED1",
            "--primary-light": "#22d3ee",
            "--primary-dark": "#0891b2",
            "--secondary-color": "#9333EA",
            "--tbf-gradient": "linear-gradient(135deg, #00CED1 0%, #6366F1 50%, #9333EA 100%)",
            "--tbf-gradient-subtle": "linear-gradient(135deg, rgba(0, 206, 209, 0.15) 0%, rgba(99, 102, 241, 0.1) 50%, rgba(147, 51, 234, 0.15) 100%)",
            "--storm-gradient": "linear-gradient(135deg, #00CED1 0%, #6366F1 50%, #9333EA 100%)",
        },
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
