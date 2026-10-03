"""数据总览报告导出 — HTML → Word(docx) / PDF（精美排版）

复用 agent/html_report.build_html_report 产出的单文件 HTML，
用 HTMLParser 提取结构化内容块（标题/段落/风险/亮点），
分别渲染为 python-docx 文档与 reportlab PDF（中文字体 simhei.ttf）。

排版规范（两格式统一）：
- 品牌主色 #4f46e5；标题深灰；正文 #374151
- 一级标题带主色下划线；小节标题主色加粗 + 色块前缀
- 风险块：浅红底 + 红左边框 + 红字；亮点块：浅绿底 + 绿左边框 + 绿字
- 统计数字红色加粗高亮；页脚含报告名 + 页码
- 剔除 emoji（simhei 无 emoji 字形，PDF 会显示为方块）
"""

from __future__ import annotations

import io
import logging
import os
import re
from html.parser import HTMLParser

# ── 中文字体（PDF 用；Windows 黑体，跨环境时可通过 REPORT_CJK_FONT 覆盖）──
_CJK_FONT_PATH = os.getenv("REPORT_CJK_FONT", "C:/Windows/Fonts/simhei.ttf")
_CJK_FONT_NAME = "SimHei"

# ── 配色（商务靛蓝主题）────────────────────────────────────
_COLOR_BRAND = "#4f46e5"
_COLOR_TITLE = "#1f2937"
_COLOR_BODY = "#374151"
_COLOR_GRAY = "#9ca3af"
_COLOR_NUM = "#dc2626"          # 数字高亮
_COLOR_RISK = "#b91c1c"
_COLOR_RISK_BG = "#fef2f2"
_COLOR_RISK_BD = "#fecaca"
_COLOR_GOOD = "#047857"
_COLOR_GOOD_BG = "#ecfdf5"
_COLOR_GOOD_BD = "#a7f3d0"
_FONT_NAME = "微软雅黑"


# ── HTML → 内容块（(type, text)；type: title/meta/heading/subheading/para/risk/good）──

class _BlockParser(HTMLParser):
    """提取报告 HTML 的结构化文本块；跳过 script/style/button 内容。"""

    _SKIP_TAGS = {"script", "style", "button"}
    _TITLE_RE = re.compile(r"^\s*📊\s*")

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks: list[tuple[str, str]] = []
        self._buf: list[str] = []
        self._type: str | None = None
        self._skip = 0

    def _flush(self):
        if self._type and "".join(self._buf).strip():
            text = re.sub(r"\s+", " ", "".join(self._buf)).strip()
            if self._type == "title":
                text = self._TITLE_RE.sub("", text).strip() or "数据总览报告"
            self.blocks.append((self._type, text))
        self._buf = []
        self._type = None

    def handle_starttag(self, tag, attrs):
        cls = dict(attrs).get("class", "")
        if tag in self._SKIP_TAGS:
            self._skip += 1
            return
        if tag == "br":
            self._buf.append(" ")
            return
        if tag in ("h1", "h3", "p", "li"):
            self._flush()
            if tag == "h1":
                self._type = "title"
            elif tag == "h3":
                self._type = "heading"
            elif "sum-risk" in cls:
                self._type = "risk"
            elif "sum-good" in cls:
                self._type = "good"
            elif "sum-sec-h" in cls:
                self._type = "subheading"
            else:
                self._type = "para"
        elif tag == "div":
            if "sum-sec-h" in cls:
                self._flush()
                self._type = "subheading"
            elif "sum-risk" in cls:
                # 2026-10-03 修复（P1）：html_report 产出的是 <div class="sum-risk"> /
                # <div class="sum-good">，但本分支此前只认 sum-sec-h 与 footer →
                # _type 保持 None → handle_endtag 调 _flush() 时判定失败 → **整段丢弃**。
                # 后果：网页版总览报告的风险/亮点齐全，点「导出 Word/PDF」后
                # 「⚠️ 停机时长激增 3 倍」这类最该被看到的结论整段消失，
                # 而导出件看起来是完整的（比报错更糟）。
                # （80/82 行的同名判断在 p/li 分支里，对 div 不生效。）
                self._flush()
                self._type = "risk"
            elif "sum-good" in cls:
                self._flush()
                self._type = "good"
            elif "footer" in cls:
                self._flush()
                self._type = "footer"  # 页脚，最终过滤

    def handle_endtag(self, tag):
        if tag in self._SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
            return
        if tag in ("h1", "h3", "p", "li", "div"):
            self._flush()

    def handle_data(self, data):
        if self._skip:
            return
        if data.strip():
            self._buf.append(data)


def parse_report_blocks(html: str) -> list[tuple[str, str]]:
    p = _BlockParser()
    try:
        p.feed(html)
    except Exception as e:
        logging.getLogger("report_export").warning("HTML 报告解析失败: %s", e)
    return [b for b in p.blocks if b[0] != "footer"]


# ── 通用：emoji 剔除 + 统计数字切分（用于红色高亮）──────────

_EMOJI_RE = re.compile(
    r"[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D\u2122\u00A9\u00AE\u2728\u2B50\u2B55\u274C\u2705\u26A0\u2757\u3030]+"
)
# 统计数字：两位及以上整数 / 小数 / 百分比 / 千分位
_NUM_RE = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?%?|\d+\.\d+%?|\d{2,}%?")


def _strip_emoji(text: str) -> str:
    return re.sub(r"\s+", " ", _EMOJI_RE.sub("", text)).strip()


def _segments(text: str) -> list[tuple[str, bool]]:
    """把文本切分为 (片段, 是否数字) 序列，数字片段做红色加粗高亮。"""
    out: list[tuple[str, bool]] = []
    pos = 0
    for m in _NUM_RE.finditer(text):
        if m.start() > pos:
            out.append((text[pos:m.start()], False))
        out.append((m.group(), True))
        pos = m.end()
    if pos < len(text):
        out.append((text[pos:], False))
    return out or [(text, False)]


# ── Word（python-docx，精美排版）───────────────────────────

def blocks_to_docx(blocks: list[tuple[str, str]]) -> bytes:
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Pt, RGBColor, Cm

    doc = Document()
    # 全局默认字体与正文样式
    normal = doc.styles["Normal"]
    normal.font.name = _FONT_NAME
    normal.font.size = Pt(11)
    normal.font.color.rgb = RGBColor.from_string(_COLOR_BODY.lstrip("#"))
    try:
        normal.element.rPr.rFonts.set(qn("w:eastAsia"), _FONT_NAME)
    except Exception:
        pass
    # 页面边距
    for s in doc.sections:
        s.left_margin = Cm(2.2)
        s.right_margin = Cm(2.2)
        s.top_margin = Cm(2.0)
        s.bottom_margin = Cm(2.0)

    def _font(run, size, bold=False, color=None):
        run.font.name = _FONT_NAME
        run.font.size = Pt(size)
        run.font.bold = bold
        if color:
            run.font.color.rgb = RGBColor.from_string(color.lstrip("#"))
        try:
            run.element.rPr.rFonts.set(qn("w:eastAsia"), _FONT_NAME)
        except Exception:
            pass

    def _para(align=None, before=0, after=6, line=1.5, indent_cm=None):
        p = doc.add_paragraph()
        pf = p.paragraph_format
        pf.space_before = Pt(before)
        pf.space_after = Pt(after)
        pf.line_spacing = line
        if align is not None:
            p.alignment = align
        if indent_cm is not None:
            pf.first_line_indent = Cm(indent_cm)
        return p

    def _shade_left(p, fill_hex, border_hex):
        """段落浅底纹 + 左侧色条。"""
        pPr = p._p.get_or_add_pPr()
        shd = OxmlElement("w:shd")
        shd.set(qn("w:val"), "clear")
        shd.set(qn("w:fill"), fill_hex.lstrip("#"))
        pPr.append(shd)
        pBdr = OxmlElement("w:pBdr")
        left = OxmlElement("w:left")
        left.set(qn("w:val"), "single")
        left.set(qn("w:sz"), "16")
        left.set(qn("w:space"), "6")
        left.set(qn("w:color"), border_hex.lstrip("#"))
        pBdr.append(left)
        pPr.append(pBdr)

    def _bottom_border(p, color_hex):
        pPr = p._p.get_or_add_pPr()
        pBdr = OxmlElement("w:pBdr")
        bottom = OxmlElement("w:bottom")
        bottom.set(qn("w:val"), "single")
        bottom.set(qn("w:sz"), "8")
        bottom.set(qn("w:space"), "4")
        bottom.set(qn("w:color"), color_hex.lstrip("#"))
        pBdr.append(bottom)
        pPr.append(pBdr)

    def _add_runs(p, text, size, bold=False, color=None, highlight_nums=True):
        for seg, is_num in _segments(_strip_emoji(text)):
            run = p.add_run(seg)
            if is_num and highlight_nums:
                _font(run, size, bold=True, color=_COLOR_NUM)
            else:
                _font(run, size, bold=bold, color=color)

    # 标题区
    for typ, text in blocks:
        clean = _strip_emoji(text)
        if typ == "title":
            p = _para(align=WD_ALIGN_PARAGRAPH.CENTER, before=0, after=4, line=1.2)
            _add_runs(p, clean, 22, bold=True, color=_COLOR_BRAND, highlight_nums=False)
            _bottom_border(p, _COLOR_BRAND)
        elif typ == "meta":
            p = _para(align=WD_ALIGN_PARAGRAPH.CENTER, before=2, after=16, line=1.3)
            _add_runs(p, clean, 9, color=_COLOR_GRAY, highlight_nums=False)
        elif typ == "heading":
            p = _para(before=12, after=8, line=1.3)
            _add_runs(p, clean, 15, bold=True, color=_COLOR_BRAND, highlight_nums=False)
            _bottom_border(p, _COLOR_BRAND)
        elif typ == "subheading":
            p = _para(before=10, after=6, line=1.3)
            run = p.add_run("▎ ")
            _font(run, 13, bold=True, color=_COLOR_BRAND)
            _add_runs(p, clean, 13, bold=True, color=_COLOR_TITLE, highlight_nums=False)
        elif typ == "risk":
            p = _para(before=4, after=6, line=1.5, indent_cm=0.3)
            _shade_left(p, _COLOR_RISK_BG, _COLOR_RISK)
            _add_runs(p, clean, 10.5, color=_COLOR_RISK)
        elif typ == "good":
            p = _para(before=4, after=6, line=1.5, indent_cm=0.3)
            _shade_left(p, _COLOR_GOOD_BG, _COLOR_GOOD)
            _add_runs(p, clean, 10.5, color=_COLOR_GOOD)
        else:
            p = _para(before=2, after=6, line=1.6, indent_cm=0.74)  # 首行缩进 2 字符
            _add_runs(p, clean, 11)

    # 页脚：报告名 + 页码
    footer = doc.sections[0].footer
    fp = footer.paragraphs[0]
    fp.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = fp.add_run("数据总览报告 · 第 ")
    _font(r, 8.5, color=_COLOR_GRAY)
    r2 = fp.add_run()
    fld1 = OxmlElement("w:fldChar"); fld1.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText"); instr.set(qn("xml:space"), "preserve"); instr.text = "PAGE"
    fld2 = OxmlElement("w:fldChar"); fld2.set(qn("w:fldCharType"), "end")
    r2._r.append(fld1); r2._r.append(instr); r2._r.append(fld2)
    _font(r2, 8.5, color=_COLOR_GRAY)
    r3 = fp.add_run(" 页")
    _font(r3, 8.5, color=_COLOR_GRAY)

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


# ── PDF（reportlab platypus，精美排版）─────────────────────

def blocks_to_pdf(blocks: list[tuple[str, str]]) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Table, TableStyle

    try:
        pdfmetrics.registerFont(TTFont(_CJK_FONT_NAME, _CJK_FONT_PATH))
        font_ok = True
    except Exception:
        font_ok = False
    fn = _CJK_FONT_NAME if font_ok else "Helvetica"

    def _hex(c: str):
        return colors.HexColor(c)

    def _style(name, size, leading, bold=False, color=_COLOR_BODY, before=0, after=6,
               back=None, border=None, pad=None):
        kw = dict(fontName=fn, fontSize=size, leading=leading, textColor=_hex(color),
                  spaceBefore=before, spaceAfter=after, wordWrap="CJK")
        # 说明：SimHei 无独立粗体字形，reportlab ParagraphStyle 也无文字描边参数，
        # bold 仅作为语义标记保留（当前渲染不额外加粗），避免误导性 no-op 代码。
        _ = bold
        if back:
            kw["backColor"] = _hex(back)
        if border:
            kw["borderColor"] = _hex(border)
            kw["borderWidth"] = 0.6
            kw["borderPadding"] = pad or (6, 10, 6, 10)
        return ParagraphStyle(name, **kw)

    S = {
        "heading": _style("h", 15, 22, bold=True, color=_COLOR_BRAND, before=14, after=6),
        "subheading": _style("sh", 12.5, 18, bold=True, color=_COLOR_TITLE, before=10, after=5),
        "risk": _style("r", 10.5, 17, color=_COLOR_RISK, before=4, after=6,
                       back=_COLOR_RISK_BG, border=_COLOR_RISK_BD),
        "good": _style("g", 10.5, 17, color=_COLOR_GOOD, before=4, after=6,
                       back=_COLOR_GOOD_BG, border=_COLOR_GOOD_BD),
        "para": _style("p", 11, 18, color=_COLOR_BODY, before=2, after=6),
        "meta": _style("m", 9, 13, color=_COLOR_GRAY, before=4, after=16),
    }

    def _rich(text: str, highlight_nums=True) -> str:
        """转义 + emoji 剔除 + 数字红色加粗（reportlab 内联标记）。"""
        esc = _strip_emoji(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        if not highlight_nums:
            return esc
        out = []
        for seg, is_num in _segments(esc):
            if is_num:
                out.append(f'<font color="{_COLOR_NUM}"><b>{seg}</b></font>')
            else:
                out.append(seg)
        return "".join(out)

    PAGE_W = A4[0]
    content_w = PAGE_W - 40 * mm

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm,
                            topMargin=16 * mm, bottomMargin=18 * mm, title="数据总览报告")

    flow = []
    first = True
    for typ, text in blocks:
        if first and typ == "title":
            first = False
            title_style = _style("title", 21, 30, bold=True, color="#ffffff", before=0, after=0)
            t = Table([[Paragraph(_rich(text, highlight_nums=False), title_style)]],
                      colWidths=[content_w])
            t.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), _hex(_COLOR_BRAND)),
                ("LEFTPADDING", (0, 0), (-1, -1), 16),
                ("RIGHTPADDING", (0, 0), (-1, -1), 16),
                ("TOPPADDING", (0, 0), (-1, -1), 14),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 14),
            ]))
            flow.append(t)
            continue
        if typ == "title":
            continue
        if typ == "heading":
            flow.append(Paragraph(_rich(text, highlight_nums=False), S["heading"]))
            flow.append(HRFlowable(width="100%", thickness=1, color=_hex(_COLOR_BRAND), spaceBefore=0, spaceAfter=2))
            continue
        if typ == "subheading":
            flow.append(Paragraph(
                f'<font color="{_COLOR_BRAND}">▎</font> ' + _rich(text, highlight_nums=False),
                S["subheading"]))
            continue
        if typ in ("risk", "good", "para", "meta"):
            flow.append(Paragraph(_rich(text), S[typ]))
            continue
        flow.append(Paragraph(_rich(text), S["para"]))

    def _footer(canvas, doc_):
        canvas.saveState()
        canvas.setFont(fn, 8)
        canvas.setFillColor(_hex(_COLOR_GRAY))
        canvas.drawCentredString(PAGE_W / 2, 11 * mm, f"数据总览报告 · 第 {doc_.page} 页")
        canvas.restoreState()

    doc.build(flow, onFirstPage=_footer, onLaterPages=_footer)
    return buf.getvalue()
