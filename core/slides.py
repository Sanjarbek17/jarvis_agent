"""Picture-heavy slide decks as tiny OpenDocument presentations (.odp) (ticket 917f6c5a).

The Claude Google Drive connector can't put content into a Slides deck, but it can upload a file that Google
turns into a real Google Slides deck. The upload goes through the model as base64 text, which it must copy
exactly: a 18 KB .pptx (24,000 characters) failed twice that way, so decks are written as .odp instead,
plain XML with no theme or layouts, a few KB. The "pictures" are big emoji in coloured circles, with arrows.

A deck spec: {"title": "...", "subtitle": "...", "slides": [{"icon": "📱", "title": "...", "short": "...",
"lines": ["..."]}], "flow_title": "..."}: a title slide shows every step's icon, then one slide per step,
then a recap with the icons joined by arrows (when there are 2 or more steps).
"""
import base64
import io
import zipfile
from xml.sax.saxutils import escape

MIME = "application/vnd.oasis.opendocument.presentation"
W, H = 33.867, 19.05                              # cm, 16:9: the layout below is drawn on this
K = 27.94 / 33.867                                # ...and scaled to the 27.94 cm (11 in) page Google gives an
                                                  # imported deck, whatever page size the file says (measured)
COLORS = ["#DBEAFE", "#DCFCE7", "#FEF3C7", "#FCE7F3", "#E0E7FF", "#CCFBF1"]
INK, MUTED, ACCENT = "#1F2937", "#6B7280", "#2563EB"

NS = ('xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
      'xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0" '
      'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
      'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" '
      'xmlns:fo="urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0" '
      'xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0" '
      'xmlns:presentation="urn:oasis:names:tc:opendocument:xmlns:presentation:1.0" office:version="1.2"')

PARAS = (("pT", 44, INK, True, "center"), ("pS", 20, MUTED, False, "center"), ("pH", 36, INK, True, "start"),
         ("pL", 22, MUTED, False, "start"), ("pN", 16, ACCENT, True, "start"), ("pC", 16, INK, True, "center"),
         ("pR", 36, INK, True, "center"), ("pI", 73, INK, False, "center"), ("pB", 110, INK, False, "center"),
         ("pi", 54, INK, False, "center"))        # emoji under ~44 pt after scaling went missing in Google's copy


def _graphic(name: str, fill: str = "", middle: bool = False) -> str:
    props = f'draw:fill="solid" draw:fill-color="{fill}"' if fill else 'draw:fill="none"'
    mid = ' draw:textarea-vertical-align="middle"' if middle else ""
    return (f'<style:style style:name="{name}" style:family="graphic"><style:graphic-properties {props} '
            f'draw:stroke="none"{mid}/></style:style>')


def _styles_auto() -> str:
    out = [_graphic("gn"), _graphic("ga", ACCENT), _graphic("gw", "#FFFFFF", True)]
    out += [_graphic(f"g{i}", c, True) for i, c in enumerate(COLORS)]
    for name, size, color, bold, align in PARAS:
        weight = ' fo:font-weight="bold"' if bold else ""
        out.append(f'<style:style style:name="{name}" style:family="paragraph"><style:paragraph-properties '
                   f'fo:text-align="{align}"/><style:text-properties fo:font-size="{round(size * K)}pt" fo:color="{color}"'
                   f'{weight}/></style:style>')
    return "".join(out)


def _at(x, y, w, h) -> str:
    return f'svg:x="{x * K:.2f}cm" svg:y="{y * K:.2f}cm" svg:width="{w * K:.2f}cm" svg:height="{h * K:.2f}cm"'


def _box(x, y, w, h, text, para) -> str:
    paras = "".join(f'<text:p text:style-name="{para}">{escape(line)}</text:p>' for line in text.split("\n"))
    return f'<draw:frame draw:style-name="gn" {_at(x, y, w, h)}><draw:text-box>{paras}</draw:text-box></draw:frame>'


def _circle(x, y, d, style, icon, para) -> str:
    return (f'<draw:ellipse draw:style-name="{style}" {_at(x, y, d, d)}>'
            f'<text:p text:style-name="{para}">{escape(icon)}</text:p></draw:ellipse>')


def _shape(x, y, w, h, style, kind, path) -> str:
    return (f'<draw:custom-shape draw:style-name="{style}" {_at(x, y, w, h)}><draw:enhanced-geometry '
            f'svg:viewBox="0 0 21600 21600" draw:type="{kind}" draw:enhanced-path="{path}"/></draw:custom-shape>')


def _rect(x, y, w, h, style) -> str:
    return _shape(x, y, w, h, style, "rectangle", "M 0 0 L 21600 0 21600 21600 0 21600 Z N")


def _arrow(x, y, w, h) -> str:
    return _shape(x, y, w, h, "ga", "right-arrow",
                  "M 0 5400 L 14400 5400 14400 0 21600 10800 14400 21600 14400 16200 0 16200 Z N")


def _row(steps, top, d, icon_para, labels=False) -> str:
    """Every step's icon in a circle, left to right, with arrows between."""
    n, gap = max(1, len(steps)), 1.4
    x = (W - (n * d + (n - 1) * gap)) / 2
    out = []
    for i, s in enumerate(steps):
        out.append(_circle(x, top, d, f"g{i % len(COLORS)}", s.get("icon", "•"), icon_para))
        if labels:
            out.append(_box(x - 0.5, top + d + 0.4, d + 1.0, 1.4, s.get("short") or s.get("title", ""), "pC"))
        if i < n - 1:
            out.append(_arrow(x + d + 0.2, top + d / 2 - 0.45, gap - 0.4, 0.9))
        x += d + gap
    return "".join(out)


def build(spec: dict) -> bytes:
    steps = spec.get("slides", [])
    pages = [_box(2, 2.4, W - 4, 3, spec.get("title", ""), "pT")
             + (_box(2, 5.6, W - 4, 1.6, spec["subtitle"], "pS") if spec.get("subtitle") else "")
             + _row(steps, 9.0, 3.6, "pi")]
    for i, s in enumerate(steps):
        lines = "\n".join(f"• {line}" for line in (s.get("lines") or [])[:4])
        pages.append(_rect(0, 0, 12.8, H, f"g{i % len(COLORS)}")
                     + _circle(2.0, 5.3, 8.4, "gw", s.get("icon", "•"), "pB")
                     + _box(14.2, 2.4, 6, 1.4, f"{i + 1} / {len(steps)}", "pN")
                     + _box(14.2, 3.8, 18, 3.8, s.get("title", ""), "pH")
                     + (_box(14.2, 8.4, 18, 8, lines, "pL") if lines else ""))
    if len(steps) > 1:
        pages.append(_box(2, 2.2, W - 4, 2.4, spec.get("flow_title") or "All together", "pR")
                     + _row(steps, 6.6, 4.6, "pI", labels=True))
    content = (f'<?xml version="1.0" encoding="UTF-8"?><office:document-content {NS}><office:automatic-styles>'
               f'{_styles_auto()}</office:automatic-styles><office:body><office:presentation>'
               + "".join(f'<draw:page draw:name="p{i + 1}" draw:master-page-name="Default">{p}</draw:page>'
                         for i, p in enumerate(pages))
               + '</office:presentation></office:body></office:document-content>')
    styles = (f'<?xml version="1.0" encoding="UTF-8"?><office:document-styles {NS}><office:automatic-styles>'
              f'<style:page-layout style:name="PM1"><style:page-layout-properties fo:page-width="{W * K:.2f}cm" '
              f'fo:page-height="{H * K:.2f}cm" style:print-orientation="landscape"/></style:page-layout></office:automatic-styles>'
              '<office:master-styles><style:master-page style:name="Default" style:page-layout-name="PM1"/>'
              '</office:master-styles></office:document-styles>')
    manifest = ('<?xml version="1.0" encoding="UTF-8"?><manifest:manifest '
                'xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0" manifest:version="1.2">'
                f'<manifest:file-entry manifest:full-path="/" manifest:media-type="{MIME}"/>'
                '<manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/>'
                '<manifest:file-entry manifest:full-path="styles.xml" manifest:media-type="text/xml"/>'
                '</manifest:manifest>')
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), MIME, compress_type=zipfile.ZIP_STORED)   # first, uncompressed
        for name, data in (("content.xml", content), ("styles.xml", styles), ("META-INF/manifest.xml", manifest)):
            z.writestr(name, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    return out.getvalue()


def build_base64(spec: dict) -> str:
    return base64.b64encode(build(spec)).decode()
