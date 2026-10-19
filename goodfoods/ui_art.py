"""Original illustrated covers, one per GoodFoods concept (pure inline SVG, no image files).

The outlets are fictional, so we use illustrations rather than stock photos: no licences,
no downloads, nothing that implies a real restaurant, and a consistent brand look.
"""
from __future__ import annotations

# concept -> (background, motif colour, motif)
STYLES = {
    "Tandoor House": ("#F3E3D3", "#9A4A1F", "flames"),
    "Dosa Darbar": ("#F4EBD0", "#8A6A12", "dosa"),
    "Coastal Kitchen": ("#DCEBEF", "#1F5F73", "waves"),
    "Wok Street": ("#F2DCD5", "#9B2C2C", "wok"),
    "Trattoria": ("#EEE3D2", "#7A3E2B", "pizza"),
    "Smokehouse": ("#E6DED6", "#4A3B32", "grill"),
    "Biryani Co.": ("#F1E2C6", "#8A5A12", "handi"),
    "Green Bowl": ("#DDEBDD", "#2F6B3B", "leaves"),
    "Sakura": ("#F3E1E6", "#8C3B57", "sushi"),
    "Mezze Terrace": ("#F0E6D6", "#6A5A2B", "sun"),
    "Chettinad Mess": ("#EEDDD0", "#8B3A1E", "leaf"),
    "Skyline Grill": ("#DCE2EC", "#2B3F66", "skyline"),
    "Café Brew": ("#EADFD3", "#5A3B26", "cup"),
}


def _motif(kind: str, c: str) -> str:
    s = f'stroke="{c}" stroke-width="3" fill="none" stroke-linecap="round" stroke-linejoin="round"'
    f = f'fill="{c}"'
    if kind == "flames":
        return "".join(f'<path d="M{x} 92 C{x - 14} 70 {x + 2} 60 {x - 4} 36 C{x + 14} 52 {x + 18} 70 {x} 92Z" {f} '
                       f'opacity="{o}"/>' for x, o in ((140, .55), (160, .9), (180, .55))) + \
            f'<path d="M118 96 H202" {s}/>'
    if kind == "dosa":
        return (f'<path d="M84 96 H236" {s} opacity=".5"/>'
                f'<path d="M92 82 L222 54 Q238 70 224 88 Z" {f} opacity=".85"/>'
                f'<path d="M222 54 Q238 70 224 88" stroke="#F4EBD0" stroke-width="2" fill="none"/>'
                f'<path d="M108 100 a12 8 0 0 0 24 0Z" {f} opacity=".55"/>'
                f'<path d="M138 100 a12 8 0 0 0 24 0Z" {f} opacity=".35"/>')
    if kind == "waves":
        waves = "".join(f'<path d="M70 {y} q15 -10 30 0 t30 0 t30 0 t30 0 t30 0 t30 0" {s} opacity="{o}"/>'
                        for y, o in ((86, .9), (98, .5)))
        return waves + (f'<path d="M140 56 q22 -16 44 0 q-22 16 -44 0Z" {f}/>'
                        f'<path d="M184 56 l14 -10 v20Z" {f}/>')
    if kind == "wok":
        return (f'<path d="M110 66 h100 a50 30 0 0 1 -100 0Z" {f} opacity=".85"/>'
                f'<path d="M150 30 L176 62 M166 26 L186 60" {s}/>'
                f'<path d="M210 70 h22" {s}/>')
    if kind == "pizza":
        slices = "".join(f'<path d="M160 64 L{160 + 34 * x:.0f} {64 + 34 * y:.0f}" {s} opacity=".6"/>'
                         for x, y in ((1, 0), (-0.5, 0.87), (-0.5, -0.87)))
        dots = "".join(f'<circle cx="{x}" cy="{y}" r="4" {f}/>' for x, y in ((146, 54), (176, 58), (158, 80), (176, 78)))
        return f'<circle cx="160" cy="64" r="34" {s}/>' + slices + dots
    if kind == "grill":
        bars = "".join(f'<path d="M112 {y} H208" {s}/>' for y in (62, 74, 86))
        return bars + f'<path d="M160 26 C150 40 168 44 158 56" {s} opacity=".6"/>' + \
            f'<path d="M180 30 C172 42 186 46 178 56" {s} opacity=".6"/>'
    if kind == "handi":
        return (f'<path d="M122 58 h76 c0 30 -14 40 -38 40 s-38 -10 -38 -40Z" {f} opacity=".85"/>'
                f'<path d="M116 58 h88" {s}/><path d="M146 50 q14 -14 28 0" {s}/>'
                f'<path d="M150 38 q4 -8 0 -14 M166 36 q4 -8 0 -14" {s} opacity=".5"/>')
    if kind == "leaves":
        return (f'<path d="M112 70 h96 a48 26 0 0 1 -96 0Z" {s}/>'
                f'<path d="M140 64 c-4 -22 12 -34 24 -34 c2 16 -8 30 -24 34Z" {f} opacity=".85"/>'
                f'<path d="M164 64 c6 -18 22 -24 32 -20 c-4 14 -18 22 -32 20Z" {f} opacity=".55"/>')
    if kind == "sushi":
        rolls = "".join(f'<circle cx="{x}" cy="70" r="16" {f} opacity=".85"/><circle cx="{x}" cy="70" r="7" '
                        f'fill="#F3E1E6"/>' for x in (124, 160, 196))
        return rolls + f'<path d="M100 96 H220" {s}/>'
    if kind == "sun":
        rays = "".join(f'<path d="M{160 + 40 * x:.0f} {78 - 40 * y:.0f} L{160 + 52 * x:.0f} {78 - 52 * y:.0f}" {s} '
                       f'opacity=".6"/>' for x, y in ((1, 0), (.7, .7), (0, 1), (-.7, .7), (-1, 0)))
        return f'<path d="M126 78 a34 34 0 0 1 68 0Z" {f} opacity=".85"/>' + rays + f'<path d="M90 82 H230" {s}/>'
    if kind == "leaf":
        return (f'<path d="M96 76 C130 40 200 40 228 70 C200 96 130 100 96 76Z" {f} opacity=".8"/>'
                f'<path d="M100 76 C150 66 190 66 226 70" stroke="#EEDDD0" stroke-width="2" fill="none"/>'
                f'<path d="M150 34 q10 6 6 18" {s}/>')
    if kind == "skyline":
        blocks = "".join(f'<rect x="{x}" y="{y}" width="{w}" height="{100 - y}" {f} opacity="{o}"/>'
                         for x, y, w, o in ((104, 60, 18, .5), (124, 40, 22, .85), (148, 52, 16, .6),
                                            (166, 30, 20, .9), (188, 56, 18, .55), (208, 46, 14, .7)))
        return blocks + f'<circle cx="222" cy="24" r="8" {f} opacity=".5"/>'
    if kind == "cup":
        return (f'<path d="M132 52 h52 v26 a18 18 0 0 1 -18 18 h-16 a18 18 0 0 1 -18 -18Z" {f} opacity=".85"/>'
                f'<path d="M184 58 a10 10 0 0 1 0 20" {s}/><path d="M122 100 H196" {s}/>'
                f'<path d="M148 42 q6 -8 0 -16 M166 42 q6 -8 0 -16" {s} opacity=".5"/>')
    return f'<circle cx="160" cy="64" r="30" {s}/>'


def cover_svg(concept: str, height: int = 96, radius: int = 12, label: str | None = None) -> str:
    """An inline SVG cover for an outlet concept, filling its container's width."""
    bg, fg, kind = STYLES.get(concept, ("#E6F0EB", "#1E5A47", "plate"))
    dots = "".join(f'<circle cx="{x}" cy="{y}" r="1.4" fill="{fg}" opacity=".12"/>'
                   for x in range(10, 320, 22) for y in range(10, 128, 22))
    aria = f'role="img" aria-label="{label or concept} illustration"'
    return (f'<svg viewBox="0 0 320 128" preserveAspectRatio="xMidYMid slice" width="100%" height="{height}" '
            f'{aria} style="display:block;border-radius:{radius}px;background:{bg}">'
            f'{dots}<g transform="translate(0 4)">{_motif(kind, fg)}</g></svg>')
