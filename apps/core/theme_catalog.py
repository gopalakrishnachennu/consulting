"""Shared platform theme catalog for settings UI and runtime CSS variables."""

from copy import deepcopy


def _hex_to_rgb(value: str) -> tuple[float, float, float]:
    value = value.lstrip("#")
    return tuple(int(value[i : i + 2], 16) / 255 for i in (0, 2, 4))


def _relative_luminance(value: str) -> float:
    channels = []
    for channel in _hex_to_rgb(value):
        if channel <= 0.03928:
            channels.append(channel / 12.92)
        else:
            channels.append(((channel + 0.055) / 1.055) ** 2.4)
    r, g, b = channels
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast_ratio(foreground: str, background: str) -> float:
    l1 = _relative_luminance(foreground)
    l2 = _relative_luminance(background)
    lighter = max(l1, l2)
    darker = min(l1, l2)
    return (lighter + 0.05) / (darker + 0.05)


def _contrast_grade(ratio: float) -> str:
    if ratio >= 7:
        return "AAA"
    if ratio >= 4.5:
        return "AA"
    if ratio >= 3:
        return "Large only"
    return "Review"


def _overall_contrast_label(ratios: list[float]) -> tuple[str, str]:
    minimum = min(ratios)
    if minimum >= 7:
        return "Excellent contrast", "emerald"
    if minimum >= 4.5:
        return "Strong contrast", "green"
    if minimum >= 3:
        return "Mixed contrast", "amber"
    return "Needs review", "rose"


_THEMES = [
    {
        "slug": "indigo",
        "label": "Indigo",
        "tagline": "Professional",
        "group": "core",
        "group_label": "Core palettes",
        "badge": None,
        "swatches": ["#4f46e5", "#3b82f6", "#06b6d4"],
        "brand_50": "#eef2ff",
        "brand_100": "#e0e7ff",
        "brand_200": "#c7d2fe",
        "brand_300": "#a5b4fc",
        "brand_400": "#818cf8",
        "brand_500": "#6366f1",
        "brand_600": "#4f46e5",
        "brand_700": "#4338ca",
        "brand_800": "#3730a3",
        "nav": "#4f46e5",
        "nav_dark": "#4338ca",
        "gradient": "linear-gradient(135deg,#4f46e5 0%,#3b82f6 50%,#06b6d4 100%)",
        "ring": "rgba(99,102,241,.25)",
        "text": "#4f46e5",
        "text_hover": "#4338ca",
    },
    {
        "slug": "violet",
        "label": "Violet",
        "tagline": "Bold & Modern",
        "group": "core",
        "group_label": "Core palettes",
        "badge": None,
        "swatches": ["#7c3aed", "#8b5cf6", "#a78bfa"],
        "brand_50": "#f5f3ff",
        "brand_100": "#ede9fe",
        "brand_200": "#ddd6fe",
        "brand_300": "#c4b5fd",
        "brand_400": "#a78bfa",
        "brand_500": "#8b5cf6",
        "brand_600": "#7c3aed",
        "brand_700": "#6d28d9",
        "brand_800": "#5b21b6",
        "nav": "#7c3aed",
        "nav_dark": "#6d28d9",
        "gradient": "linear-gradient(135deg,#7c3aed 0%,#8b5cf6 50%,#a78bfa 100%)",
        "ring": "rgba(139,92,246,.25)",
        "text": "#7c3aed",
        "text_hover": "#6d28d9",
    },
    {
        "slug": "blue",
        "label": "Blue",
        "tagline": "Corporate",
        "group": "core",
        "group_label": "Core palettes",
        "badge": None,
        "swatches": ["#1d4ed8", "#2563eb", "#38bdf8"],
        "brand_50": "#eff6ff",
        "brand_100": "#dbeafe",
        "brand_200": "#bfdbfe",
        "brand_300": "#93c5fd",
        "brand_400": "#60a5fa",
        "brand_500": "#3b82f6",
        "brand_600": "#2563eb",
        "brand_700": "#1d4ed8",
        "brand_800": "#1e40af",
        "nav": "#2563eb",
        "nav_dark": "#1d4ed8",
        "gradient": "linear-gradient(135deg,#1d4ed8 0%,#2563eb 50%,#38bdf8 100%)",
        "ring": "rgba(59,130,246,.25)",
        "text": "#2563eb",
        "text_hover": "#1d4ed8",
    },
    {
        "slug": "emerald",
        "label": "Emerald",
        "tagline": "Growth",
        "group": "core",
        "group_label": "Core palettes",
        "badge": None,
        "swatches": ["#047857", "#059669", "#34d399"],
        "brand_50": "#ecfdf5",
        "brand_100": "#d1fae5",
        "brand_200": "#a7f3d0",
        "brand_300": "#6ee7b7",
        "brand_400": "#34d399",
        "brand_500": "#10b981",
        "brand_600": "#059669",
        "brand_700": "#047857",
        "brand_800": "#065f46",
        "nav": "#059669",
        "nav_dark": "#047857",
        "gradient": "linear-gradient(135deg,#047857 0%,#059669 50%,#34d399 100%)",
        "ring": "rgba(16,185,129,.25)",
        "text": "#059669",
        "text_hover": "#047857",
    },
    {
        "slug": "teal",
        "label": "Teal",
        "tagline": "Trustworthy",
        "group": "core",
        "group_label": "Core palettes",
        "badge": "CHENN default",
        "swatches": ["#0f766e", "#0d9488", "#2dd4bf"],
        "brand_50": "#f0fdfa",
        "brand_100": "#ccfbf1",
        "brand_200": "#99f6e4",
        "brand_300": "#5eead4",
        "brand_400": "#2dd4bf",
        "brand_500": "#14b8a6",
        "brand_600": "#0d9488",
        "brand_700": "#0f766e",
        "brand_800": "#115e59",
        "nav": "#0d9488",
        "nav_dark": "#0f766e",
        "gradient": "linear-gradient(135deg,#0f766e 0%,#0d9488 50%,#2dd4bf 100%)",
        "ring": "rgba(20,184,166,.25)",
        "text": "#0d9488",
        "text_hover": "#0f766e",
    },
    {
        "slug": "rose",
        "label": "Rose",
        "tagline": "Energetic",
        "group": "core",
        "group_label": "Core palettes",
        "badge": None,
        "swatches": ["#be123c", "#e11d48", "#fb7185"],
        "brand_50": "#fff1f2",
        "brand_100": "#ffe4e6",
        "brand_200": "#fecdd3",
        "brand_300": "#fda4af",
        "brand_400": "#fb7185",
        "brand_500": "#f43f5e",
        "brand_600": "#e11d48",
        "brand_700": "#be123c",
        "brand_800": "#9f1239",
        "nav": "#e11d48",
        "nav_dark": "#be123c",
        "gradient": "linear-gradient(135deg,#be123c 0%,#e11d48 50%,#fb7185 100%)",
        "ring": "rgba(244,63,94,.25)",
        "text": "#e11d48",
        "text_hover": "#be123c",
    },
    {
        "slug": "amber",
        "label": "Amber",
        "tagline": "Warm",
        "group": "core",
        "group_label": "Core palettes",
        "badge": None,
        "swatches": ["#92400e", "#d97706", "#fbbf24"],
        "brand_50": "#fffbeb",
        "brand_100": "#fef3c7",
        "brand_200": "#fde68a",
        "brand_300": "#fcd34d",
        "brand_400": "#fbbf24",
        "brand_500": "#f59e0b",
        "brand_600": "#d97706",
        "brand_700": "#b45309",
        "brand_800": "#92400e",
        "nav": "#b45309",
        "nav_dark": "#92400e",
        "gradient": "linear-gradient(135deg,#92400e 0%,#d97706 50%,#fbbf24 100%)",
        "ring": "rgba(245,158,11,.25)",
        "text": "#d97706",
        "text_hover": "#b45309",
    },
    {
        "slug": "slate",
        "label": "Slate",
        "tagline": "Dark & Minimal",
        "group": "core",
        "group_label": "Core palettes",
        "badge": None,
        "swatches": ["#0f172a", "#1e293b", "#475569"],
        "brand_50": "#f8fafc",
        "brand_100": "#f1f5f9",
        "brand_200": "#e2e8f0",
        "brand_300": "#cbd5e1",
        "brand_400": "#94a3b8",
        "brand_500": "#64748b",
        "brand_600": "#475569",
        "brand_700": "#334155",
        "brand_800": "#1e293b",
        "nav": "#1e293b",
        "nav_dark": "#0f172a",
        "gradient": "linear-gradient(135deg,#0f172a 0%,#1e293b 50%,#475569 100%)",
        "ring": "rgba(100,116,139,.25)",
        "text": "#475569",
        "text_hover": "#334155",
    },
    {
        "slug": "saffron",
        "label": "Saffron",
        "tagline": "Warm Indian primary",
        "group": "india",
        "group_label": "India-inspired palettes",
        "badge": "India pick",
        "swatches": ["#c2410c", "#f97316", "#fbbf24"],
        "brand_50": "#fff7ed",
        "brand_100": "#ffedd5",
        "brand_200": "#fed7aa",
        "brand_300": "#fdba74",
        "brand_400": "#fb923c",
        "brand_500": "#f97316",
        "brand_600": "#ea580c",
        "brand_700": "#c2410c",
        "brand_800": "#9a3412",
        "nav": "#c2410c",
        "nav_dark": "#9a3412",
        "gradient": "linear-gradient(135deg,#9a3412 0%,#f97316 42%,#fbbf24 100%)",
        "ring": "rgba(249,115,22,.25)",
        "text": "#c2410c",
        "text_hover": "#9a3412",
    },
    {
        "slug": "chakra",
        "label": "Chakra",
        "tagline": "Tricolor with navy anchor",
        "group": "india",
        "group_label": "India-inspired palettes",
        "badge": None,
        "swatches": ["#ff9933", "#1d3fa6", "#138808"],
        "brand_50": "#eef4ff",
        "brand_100": "#d9e7ff",
        "brand_200": "#b8d1ff",
        "brand_300": "#8cb3ff",
        "brand_400": "#5d8ef7",
        "brand_500": "#335fd1",
        "brand_600": "#1d3fa6",
        "brand_700": "#173584",
        "brand_800": "#142c69",
        "nav": "#1d3fa6",
        "nav_dark": "#142c69",
        "gradient": "linear-gradient(135deg,#ff9933 0%,#1d3fa6 48%,#138808 100%)",
        "ring": "rgba(29,63,166,.25)",
        "text": "#1d3fa6",
        "text_hover": "#142c69",
    },
    {
        "slug": "banyan",
        "label": "Banyan",
        "tagline": "Deep green with saffron edge",
        "group": "india",
        "group_label": "India-inspired palettes",
        "badge": None,
        "swatches": ["#f59e0b", "#157139", "#0f766e"],
        "brand_50": "#effaf1",
        "brand_100": "#dcf6df",
        "brand_200": "#b6ebbf",
        "brand_300": "#85d79a",
        "brand_400": "#4db96e",
        "brand_500": "#1f8f47",
        "brand_600": "#157139",
        "brand_700": "#12582e",
        "brand_800": "#104624",
        "nav": "#157139",
        "nav_dark": "#104624",
        "gradient": "linear-gradient(135deg,#f59e0b 0%,#157139 42%,#0f766e 100%)",
        "ring": "rgba(31,143,71,.25)",
        "text": "#157139",
        "text_hover": "#104624",
    },
    {
        "slug": "kesari",
        "label": "Kesari",
        "tagline": "Richer saffron for command surfaces",
        "group": "india",
        "group_label": "India-inspired palettes",
        "badge": "Executive",
        "swatches": ["#9f2d00", "#dd6b20", "#f6ad55"],
        "brand_50": "#fff8f1",
        "brand_100": "#feecdc",
        "brand_200": "#fdd4b0",
        "brand_300": "#f7b267",
        "brand_400": "#ed8936",
        "brand_500": "#dd6b20",
        "brand_600": "#c05621",
        "brand_700": "#9f2d00",
        "brand_800": "#7b2800",
        "nav": "#9f2d00",
        "nav_dark": "#7b2800",
        "gradient": "linear-gradient(135deg,#7b2800 0%,#dd6b20 46%,#f6ad55 100%)",
        "ring": "rgba(221,107,32,.25)",
        "text": "#9f2d00",
        "text_hover": "#7b2800",
    },
    {
        "slug": "ashoka",
        "label": "Ashoka",
        "tagline": "Formal navy with tricolor energy",
        "group": "india",
        "group_label": "India-inspired palettes",
        "badge": "Enterprise",
        "swatches": ["#162f65", "#2457c5", "#ff9933"],
        "brand_50": "#edf3ff",
        "brand_100": "#d9e5ff",
        "brand_200": "#bbcffd",
        "brand_300": "#95b2fb",
        "brand_400": "#5d86f2",
        "brand_500": "#2457c5",
        "brand_600": "#1b469f",
        "brand_700": "#162f65",
        "brand_800": "#11244d",
        "nav": "#162f65",
        "nav_dark": "#11244d",
        "gradient": "linear-gradient(135deg,#11244d 0%,#2457c5 56%,#ff9933 100%)",
        "ring": "rgba(36,87,197,.25)",
        "text": "#1b469f",
        "text_hover": "#162f65",
    },
    {
        "slug": "ivory_saffron",
        "label": "Ivory Saffron",
        "tagline": "Soft ivory with premium saffron accents",
        "group": "india",
        "group_label": "India-inspired palettes",
        "badge": "Premium",
        "swatches": ["#fff7e6", "#d97706", "#b45309"],
        "brand_50": "#fffbf2",
        "brand_100": "#fff4dd",
        "brand_200": "#fde7bb",
        "brand_300": "#fbd38d",
        "brand_400": "#f6ad55",
        "brand_500": "#ed8936",
        "brand_600": "#d97706",
        "brand_700": "#b45309",
        "brand_800": "#92400e",
        "nav": "#b45309",
        "nav_dark": "#92400e",
        "gradient": "linear-gradient(135deg,#fff7e6 0%,#f6ad55 34%,#b45309 100%)",
        "ring": "rgba(217,119,6,.23)",
        "text": "#b45309",
        "text_hover": "#92400e",
    },
]


def _enrich_theme(theme: dict) -> dict:
    data = deepcopy(theme)
    nav_ratio = _contrast_ratio("#ffffff", data["nav_dark"])
    button_ratio = _contrast_ratio("#ffffff", data["brand_600"])
    link_ratio = _contrast_ratio(data["text"], "#ffffff")
    overall_label, overall_tone = _overall_contrast_label([nav_ratio, button_ratio, link_ratio])
    data["contrast"] = {
        "nav_ratio": round(nav_ratio, 2),
        "nav_grade": _contrast_grade(nav_ratio),
        "button_ratio": round(button_ratio, 2),
        "button_grade": _contrast_grade(button_ratio),
        "link_ratio": round(link_ratio, 2),
        "link_grade": _contrast_grade(link_ratio),
        "overall_label": overall_label,
        "overall_tone": overall_tone,
    }
    data["contrast_overview"] = (
        f"Nav {data['contrast']['nav_grade']} • "
        f"Buttons {data['contrast']['button_grade']} • "
        f"Links {data['contrast']['link_grade']}"
    )
    return data


_CATALOG = [_enrich_theme(theme) for theme in _THEMES]
_CATALOG_BY_SLUG = {theme["slug"]: theme for theme in _CATALOG}


def get_theme_definition(slug: str | None) -> dict:
    return deepcopy(_CATALOG_BY_SLUG.get(slug or "", _CATALOG_BY_SLUG["indigo"]))


def get_theme_catalog() -> dict[str, dict]:
    return {slug: deepcopy(theme) for slug, theme in _CATALOG_BY_SLUG.items()}


def get_theme_groups() -> list[dict]:
    groups = []
    for group_key, label, description in (
        ("core", "Core palettes", "Neutral business themes for broad compatibility."),
        ("india", "India-inspired palettes", "Saffron, navy, ivory, and deep green palettes tuned for CHENN."),
    ):
        groups.append(
            {
                "key": group_key,
                "label": label,
                "description": description,
                "themes": [deepcopy(theme) for theme in _CATALOG if theme["group"] == group_key],
            }
        )
    return groups
