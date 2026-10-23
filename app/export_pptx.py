"""
export_pptx.py — 试卷 / 题目导出为 PowerPoint (.pptx)

定位：**一题一页的讲题课件**，不是讲义。
- 每页：题号 + 题型/难度角标 → 题干（公式贴图）→ 配图 → 选项 → 答案 → 解析
- 讲题节奏：答案和解析默认放「下一页」，方便先让学生想、再翻页讲
- 公式：LaTeX -> PNG（复用 formula.py 的 matplotlib 渲染 + 磁盘缓存），
  贴进 PPT 后按原始尺寸等比缩放，避免变形

排版核心是 `flow_mixed()`：把「中文 + $LaTeX$」的混合文本按**行内流式**排布
（而不是每段公式换一行），先量后排，超页自动收尾。
"""
import os
import re
import sys
import math

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db
import formula

from pptx import Presentation
from pptx.util import Inches, Pt, Emu
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
from PIL import Image

# ---------------------------------------------------------------- 版面参数

import db
from ppt_theme import Theme, load_theme

SLIDE_W = Inches(13.333)          # 16:9
SLIDE_H = Inches(7.5)
EMU_IN = 914400

# 版式常量现在是「默认主题」的兜底值；实际取用一律走 _T() 当前主题。
# 保留这些名字仅为向后兼容旧调用（如 formula 缓存目录等）。
MARGIN = Inches(0.6)
CONTENT_W = SLIDE_W - 2 * MARGIN
BAR_H = Inches(0.72)
BODY_TOP = Inches(1.0)
BODY_BOTTOM = Inches(7.1)
MAX_BODY_H = BODY_BOTTOM - BODY_TOP

FONT = '微软雅黑'

# ---- 当前生效的主题（模块级；_build 开始时设定）
_THEME = None


def _T():
    """取当前主题；未设定时惰性用默认主题。"""
    global _THEME
    if _THEME is None:
        _THEME = load_theme('默认蓝')
    return _THEME


def _cw():
    """当前主题下的正文宽度（EMU）。"""
    return int(SLIDE_W) - 2 * _T().emu('margin')


def C(name):
    """当前主题颜色 -> RGBColor。"""
    return _T().rgb(name)


TYPE_LABELS = {
    'choice_single': '单选题',
    'choice_multi': '多选题',
    'fill': '填空题',
    'judgment': '判断题',
    'experiment': '实验题',
    'calculation': '计算题',
}

_FORMULA_FAILS = []

_TEX_SPLIT = re.compile(r'\$(.+?)\$', re.S)


# ---------------------------------------------------------------- 文本度量

def vis_w(text, size):
    """
    估算文本宽度（EMU）。中文按 1.0 字宽，ASCII 按 0.52 字宽。
    微软雅黑经验值：字宽 ≈ 字号点数 * 12700 EMU/pt。
    """
    n = 0.0
    for ch in text:
        n += 1.0 if ord(ch) > 0x2E80 else 0.52
    # 1 pt = 12700 EMU；一个全角字宽 ≈ size pt
    return int(n * size * 12700)


def line_h(size, spacing=1.28):
    """一行占高（EMU）。"""
    return int(size * spacing * 12700)


def split_mixed(text):
    """把 '含 $LaTeX$ 的文本' 切成 [('text'|'tex', 内容), ...]"""
    text = text or ''
    if not text:
        return []
    segs, pos = [], 0
    for m in _TEX_SPLIT.finditer(text):
        head = text[pos:m.start()]
        if head:
            segs.append(('text', head))
        segs.append(('tex', m.group(1).strip()))
        pos = m.end()
    tail = text[pos:]
    if tail:
        segs.append(('text', tail))
    return segs


def has_tex(text):
    return bool(_TEX_SPLIT.search(text or ''))


# ---------------------------------------------------------------- 公式贴图

def _render_tex(tex, fontsize=22, dpi=200, color=None):
    """渲染公式为 PNG（透明底）。

    color 默认取当前主题的正文字色 —— 这一步很重要：
    公式图是**透明底**，若墨色写死为近黑，贴在深色主题页面上
    会变成“黑字贴深底”看不见。让墨色跟主题走，才是对的。

    注意：主题的 rgb() 返回的是 pptx 的 RGBColor **对象**，
    不能直接传给 matplotlib（它要 '#RRGGBB' 字符串），
    必须在这里统一归一化。
    """
    if color is None:
        color = _T()['body']
    color = _color_hex(color)
    p = formula.render_formula(tex, fontsize=fontsize, dpi=dpi, color=color)
    if p is None:
        _FORMULA_FAILS.append(tex)
    return p


def _color_hex(c):
    """把 '#RRGGBB' / RGBColor / (r,g,b) 统一成 '#RRGGBB' 字符串。"""
    if isinstance(c, str):
        s = c.strip()
        if s.startswith('#'):
            return s
        # 允许 'E4E4E4' 这种无 # 写法
        return '#' + s if len(s) == 6 else s
    try:
        return '#%02X%02X%02X' % (int(c[0]), int(c[1]), int(c[2]))
    except Exception:
        return '#111111'


def _png_emu(path):
    """PNG 像素 -> 逻辑 EMU（按 96 DPI）"""
    with Image.open(path) as im:
        w, h = im.size
    return int(w / 96 * EMU_IN), int(h / 96 * EMU_IN)


def _scale_to_h(w, h, target_h):
    if h <= 0:
        return w, target_h
    r = target_h / float(h)
    return int(w * r), target_h


# ---------------------------------------------------------------- 幻灯片基元

def blank_slide(prs):
    for i in (6, 5, 0):
        try:
            return prs.slides.add_slide(prs.slide_layouts[i])
        except Exception:
            continue
    return prs.slides.add_slide(prs.slide_layouts[0])


def add_bar(slide, text, right_text=''):
    t = _T()
    margin = t.emu('margin')
    bar_h = t.emu('bar_h')
    bar = slide.shapes.add_shape(1, 0, 0, SLIDE_W, bar_h)
    bar.fill.solid()
    bar.fill.fore_color.rgb = t.rgb('accent')
    bar.line.fill.background()
    bar.shadow.inherit = False

    tf = bar.text_frame
    tf.margin_left = margin
    tf.margin_right = margin
    tf.margin_top = 0
    tf.margin_bottom = 0
    tf.vertical_anchor = MSO_ANCHOR.MIDDLE
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.LEFT
    r = p.add_run()
    r.text = text
    r.font.size = Pt(t.pt('size_bar'))
    r.font.bold = True
    r.font.name = t['font']
    r.font.color.rgb = t.rgb('accent_fg')

    if right_text:
        tb = slide.shapes.add_textbox(
            SLIDE_W - margin - Inches(4.5), 0, Inches(4.5), bar_h)
        tf2 = tb.text_frame
        tf2.margin_top = 0
        tf2.margin_bottom = 0
        tf2.vertical_anchor = MSO_ANCHOR.MIDDLE
        p2 = tf2.paragraphs[0]
        p2.alignment = PP_ALIGN.RIGHT
        r2 = p2.add_run()
        r2.text = right_text
        r2.font.size = Pt(t.pt('size_bar_right'))
        r2.font.name = t['font']
        r2.font.color.rgb = t.rgb('accent_fg')


def _textbox(slide, left, top, width, height, text, size, color,
             bold=False, spacing=1.28, align=PP_ALIGN.LEFT, wrap=True):
    tb = slide.shapes.add_textbox(left, top, width, height)
    tf = tb.text_frame
    tf.word_wrap = wrap
    tf.margin_left = 0
    tf.margin_right = 0
    tf.margin_top = 0
    tf.margin_bottom = 0
    for i, ln in enumerate((text or '').split('\n')):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = align
        p.line_spacing = spacing
        r = p.add_run()
        r.text = ln
        r.font.size = Pt(size)
        r.font.bold = bold
        r.font.name = _T()['font']
        r.font.color.rgb = color
    return tb


def add_plain(slide, left, top, width, height, text, size=16,
              color=None, bold=False, spacing=1.28, align=PP_ALIGN.LEFT):
    """整块纯文本。"""
    if color is None:
        color = C('body')
    return _textbox(slide, left, top, width, height, text, size, color,
                    bold, spacing, align)


# ---------------------------------------------------------------- 流式排版核心

def _split_text_atoms(text, size, max_w):
    """
    把一段纯文字切成「行内可放的整块」。
    关键：**不能按单字切**，否则每个字一个 textbox —— 字距会乱、无法连字。
    规则：中文可在任意字间断行；连续的 ASCII 词（数字/单位/单词）作为一个整体不可拆。
    """
    atoms = []          # [(str, w)]
    buf = ''
    buf_w = 0

    def flush_buf():
        nonlocal buf, buf_w
        if buf:
            atoms.append((buf, buf_w))
        buf, buf_w = '', 0

    i = 0
    while i < len(text):
        ch = text[i]
        wide = ord(ch) > 0x2E80
        if wide:
            flush_buf()
            atoms.append((ch, vis_w(ch, size)))
            i += 1
        else:
            # 累积连续的窄字符（数字、单位、英文）
            buf += ch
            buf_w += vis_w(ch, size)
            i += 1
    flush_buf()
    return atoms


def flow_mixed(slide, left, top, width, height, text,
               size=17, color=None, bold=False,
               tex_size=0, spacing=1.28, align=PP_ALIGN.LEFT):
    """
    把「中文 + $LaTeX$」混排文本**流式**铺进 (left, top, width, height)。

    做法：
      1. 展开成「原子」：中文单字可断行，ASCII 连续串不可断，公式整块不可断
      2. 逐原子量宽，超宽就换行 —— 得到行列表
      3. 同行相邻文字原子**合并成一个 textbox**（保字距），公式贴图
    返回 (实际底部 y, 是否放得下)。
    """
    if color is None:
        color = C('body')
    if tex_size <= 0:
        tex_size = size + 7

    segs = split_mixed(text)
    if not segs:
        return top, True

    body_h = int(size * spacing * 12700)
    tex_h = int((tex_size + 7) * 1.30 * 12700)

    # ---- 1) 展开成行：每行是 [('text', str, w) | ('tex', png, w, h)]
    lines = []
    cur = []
    cur_w = 0

    def flush():
        nonlocal cur, cur_w
        if cur:
            lines.append(cur)
        cur, cur_w = [], 0

    def push_text_atoms(chunk, allow_break=True):
        nonlocal cur_w
        for atom, aw in _split_text_atoms(chunk, size, width):
            if cur_w + aw > width and cur:
                flush()
            # 与同行上一个文字原子合并，保住字距
            if cur and cur[-1][0] == 'text':
                cur[-1] = ('text', cur[-1][1] + atom, cur[-1][2] + aw)
            else:
                cur.append(('text', atom, aw))
            cur_w += aw

    for kind, content in segs:
        if kind == 'text':
            for ci, chunk in enumerate(content.split('\n')):
                if ci > 0:
                    flush()
                push_text_atoms(chunk)
        else:
            png = _render_tex(content, fontsize=tex_size, color=color)
            if not png:
                push_text_atoms(f'${content}$')
                continue
            pw, ph = _png_emu(png)
            pw, ph = _scale_to_h(pw, ph, tex_h)
            if cur_w + pw > width and cur:
                flush()
            cur.append(('tex', png, pw, ph))
            cur_w += pw + int(size * 12700 * 0.12)   # 公式后留缝
    flush()

    # ---- 2) 逐行落 shape
    y = top
    limit = top + height
    for line in lines:
        lh = tex_h if any(a[0] == 'tex' for a in line) else body_h
        if y + lh > limit and y > top:
            return y, False          # 放不下，交由调用方
        x = left
        for a in line:
            if a[0] == 'text':
                _, s, w = a
                _textbox(slide, x, y, max(w + int(size * 12700 * 0.5), 1),
                         lh, s, size, color, bold, spacing,
                         PP_ALIGN.LEFT, wrap=False)
                x += w
            else:
                _, png, w, h = a
                off = (lh - h) // 2
                slide.shapes.add_picture(png, x, y + off, width=w, height=h)
                x += w + int(size * 12700 * 0.12)
        y += lh
    return y, True


def flow_plain(slide, left, top, width, height, text, size=16,
               color=None, bold=False, spacing=1.28,
               align=PP_ALIGN.LEFT, wrap=True):
    """纯文本，按 \n 分段，估行高。返回 (底 y, 放得下)。"""
    if color is None:
        color = C('body')
    lh = line_h(size, spacing)
    y = top
    limit = top + height
    for ln in (text or '').split('\n'):
        if y + lh > limit and y > top:
            return y, False
        if ln.strip():
            nlines = 1
            if wrap:
                nlines = max(1, math.ceil(vis_w(ln, size) / float(width)))
            _textbox(slide, left, y, width, int(lh * nlines), ln, size,
                     color, bold, spacing, align)
            y += int(lh * nlines)
        else:
            y += int(lh * 0.5)
    return y, True


# ---------------------------------------------------------------- 单题页

def slide_question(prs, idx, q, total, mode='stem'):
    t = _T()
    margin = t.emu('margin')
    cw = _cw()
    body_top = t.emu('body_top')
    body_bottom = t.emu('body_bottom')
    slide = blank_slide(prs)
    typ = TYPE_LABELS.get(q.get('question_type'), '')
    bar_left = (t['bar_left'] or '第 {idx} 题 / 共 {total} 题').format(
        idx=idx, total=total)
    add_bar(slide, bar_left,
            f'{typ} · {"★" * (q.get("difficulty") or 3)}')

    y = body_top
    avail = body_bottom - y

    if mode == 'stem':
        stem_raw = q.get('stem') or ''
        stem_lead, _so = db.split_stem_options(stem_raw)
        # 按锚点把图插回正文原位（所见即所得）——只取题干区那一段，
        # 选项区之后的图交给下面统一的配图循环。
        segs = db.split_text_by_anchors(stem_raw,
                                        q.get('attachments') or [], 'stem')
        budget = len(stem_lead)
        for kind, val in segs:
            if budget <= 0:
                break
            if kind == 'text':
                piece = val[:budget]
                budget -= len(piece)
                if piece:
                    y, _ = flow_mixed(slide, margin, y, cw,
                                      body_bottom - y, piece,
                                      size=t.pt('size_stem'),
                                      tex_size=t.pt('size_tex'))
            else:
                pv = db.abs_attach_path(val.get('rel_path') or val.get('path'))
                if not pv or not os.path.exists(pv):
                    continue
                pw2, ph2 = _png_emu(pv)
                rem2 = int(body_bottom - y)
                if ph2 > rem2 and rem2 > Inches(1.0):
                    r2 = rem2 / float(ph2)
                    pw2, ph2 = int(pw2 * r2), int(ph2 * r2)
                if pw2 > cw:
                    r2 = cw / float(pw2)
                    pw2, ph2 = int(pw2 * r2), int(ph2 * r2)
                if ph2 <= int(body_bottom - y):
                    slide.shapes.add_picture(pv, margin, y,
                                             width=pw2, height=ph2)
                    y += ph2 + Inches(0.12)
        y += Inches(0.14)

        figs = q.get('attachments') or []
        # 只渲染**没在正文里插过**的图（无锚点，或锚点落在选项区之后）
        _anchored = {a.get('id') for a in (q.get('attachments') or [])
                     if a.get('anchor_offset') is not None
                     and int(a.get('anchor_offset')) >= 0
                     and (a.get('field') or 'stem') == 'stem'
                     and int(a.get('anchor_offset')) <= len(stem_lead)}
        # ★ 2026-09-29：不再写死 figs[:2]。
        #   旧代码只渲染前 2 张，第 3 张起**静默丢弃**，用户看不出来。
        #   现在全部渲染；单页放不下就由调用方分页。
        for att in figs:
            if att.get('id') in _anchored:
                continue                      # 已在正文原位插过，不重复
            p = db.abs_attach_path(att.get('rel_path') or att.get('path'))
            if not p or not os.path.exists(p):
                continue
            pw, ph = _png_emu(p)
            # 🔴 旧代码：ph = min(ph, body_bottom-y); if ph < Inches(0.5): break
            # 小图（如 60×40 的截图）高不到 0.5in 就被直接跳过，
            # 结果「粘了图但导出里没有」。现在改为：
            #   1) 剩余高度不够就收尾（真的放不下才算）；
            #   2) 图本身小于最小展示高度就等比放大到最小高度，
            #      而不是丢弃。
            rem = int(body_bottom - y)
            if rem < Inches(0.6):
                # 本页真的没地方了 —— 但绝不丢图：
                # 另开一张「配图（续）」页，从顶部继续排。
                slide = blank_slide(prs)
                add_bar(slide, bar_left, f'{typ} · 配图（续）')
                y = body_top
                rem = int(body_bottom - y)
            MIN_H = Inches(1.2)
            if ph < MIN_H:
                r = MIN_H / float(ph)
                pw, ph = int(pw * r), int(ph * r)
            if ph > rem:
                r = rem / float(ph)
                pw, ph = int(pw * r), int(ph * r)
            if pw > cw:
                r = cw / float(pw)
                pw, ph = int(pw * r), int(ph * r)
            slide.shapes.add_picture(p, margin, y, width=pw, height=ph)
            y += ph + Inches(0.12)

        opts = q.get('options') or []
        stem_text = q.get('stem') or ''
        lead_text, stem_opts = db.split_stem_options(stem_text)
        if stem_opts:
            opts = [{'label': a, 'content': b} for a, b in stem_opts]
        if opts:
            y += Inches(0.06)
            labels = 'ABCDEFGH'
            ans_set = db.parse_answer_letters(
                q.get('answer') or '',
                [o.get('label') or '' for o in opts])
            for i, o in enumerate(opts):
                if y >= body_bottom - Inches(0.3):
                    break
                lab = o.get('label') or (labels[i] if i < len(labels) else '?')
                otext = o.get('content') or o.get('text') or ''
                is_ans = lab.upper() in ans_set
                y, _ = flow_mixed(slide, margin + Inches(0.15), y,
                                  cw - Inches(0.15),
                                  body_bottom - y,
                                  f'{lab}. {otext}',
                                  size=t.pt('size_option'),
                                  tex_size=t.pt('size_option_tex'),
                                  bold=is_ans,
                                  color=(C('answer') if is_ans else None))
                y += Inches(0.05)
    else:
        stem = q.get('stem') or ''
        if len(stem) > 110:
            stem = stem[:110] + '…'
        y, _ = flow_mixed(slide, margin, y, cw, Inches(1.5),
                          stem, size=t.pt('size_stem_echo'),
                          color=C('muted'),
                          tex_size=t.pt('size_stem_echo') + 2)
        y += Inches(0.2)

        ans = q.get('answer') or ''
        if ans:
            y, _ = flow_mixed(slide, margin, y, cw, Inches(1.2),
                              f"{t['answer_prefix']}{ans}",
                              size=t.pt('size_answer'), color=C('answer'),
                              bold=True, tex_size=t.pt('size_answer_tex'))
            y += Inches(0.25)

        sol = q.get('solution') or ''
        if sol:
            y, _ = flow_plain(slide, margin, y, cw, Inches(0.4),
                              t['solution_label'], size=t.pt('size_label'),
                              color=C('solution'), bold=True)
            y += Inches(0.1)
            flow_mixed(slide, margin, y, cw, body_bottom - y,
                       sol, size=t.pt('size_solution'),
                       tex_size=t.pt('size_solution_tex'))
    return slide


# ---------------------------------------------------------------- 封面 / 分节 / 结尾

def slide_cover(prs, title, subtitle='', n_questions=0, total_score=0):
    t = _T()
    margin = t.emu('margin')
    cw = _cw()
    slide = blank_slide(prs)
    bg = slide.shapes.add_shape(1, 0, 0, SLIDE_W, SLIDE_H)
    bg.fill.solid()
    bg.fill.fore_color.rgb = t.rgb('cover_bg')
    bg.line.fill.background()
    bg.shadow.inherit = False

    st = slide.shapes.add_shape(1, 0, Inches(2.62), SLIDE_W, Inches(0.08))
    st.fill.solid()
    st.fill.fore_color.rgb = t.rgb('accent')
    st.line.fill.background()
    st.shadow.inherit = False

    add_plain(slide, margin, Inches(1.45), cw, Inches(1.0),
              title, size=t.pt('size_title'), color=C('title'), bold=True,
              align=PP_ALIGN.CENTER)
    if subtitle:
        add_plain(slide, margin, Inches(2.9), cw, Inches(0.6),
                  subtitle, size=17, color=C('muted'), align=PP_ALIGN.CENTER)
    info = []
    if n_questions:
        info.append(f'共 {n_questions} 题')
    if total_score:
        info.append(f'满分 {total_score:g} 分')
    if info:
        add_plain(slide, margin, Inches(4.0), cw, Inches(0.5),
                  '  ·  '.join(info), size=15, color=C('muted'),
                  align=PP_ALIGN.CENTER)
    footer = t['cover_footer']
    if footer:
        add_plain(slide, margin, Inches(6.7), cw, Inches(0.4),
                  footer, size=10, color=C('muted'),
                  align=PP_ALIGN.CENTER)
    return slide


def slide_section(prs, text):
    t = _T()
    margin = t.emu('margin')
    cw = _cw()
    slide = blank_slide(prs)
    bg = slide.shapes.add_shape(1, 0, 0, SLIDE_W, SLIDE_H)
    bg.fill.solid()
    bg.fill.fore_color.rgb = t.rgb('accent')
    bg.line.fill.background()
    bg.shadow.inherit = False
    add_plain(slide, margin, Inches(3.1), cw, Inches(1.4),
              text, size=t.pt('size_section'), color=C('accent_fg'),
              bold=True, align=PP_ALIGN.CENTER)
    return slide


def slide_end(prs, text=None):
    t = _T()
    margin = t.emu('margin')
    cw = _cw()
    slide = blank_slide(prs)
    add_plain(slide, margin, Inches(3.3), cw, Inches(1.0),
              text or t['end_text'], size=t.pt('size_end'),
              color=C('title'), bold=True,
              align=PP_ALIGN.CENTER)
    return slide


# ---------------------------------------------------------------- 组装

def _load_paper_questions(pid):
    paper = db.get_paper(pid)
    if not paper:
        raise ValueError(f'试卷 {pid} 不存在')
    qs = []
    for q in paper['questions']:
        qd = db.get_question(q['id'])
        if qd:
            qd['_score'] = q.get('pq_score') or 0
            qs.append(qd)
    order = {'choice_single': 0, 'choice_multi': 1, 'fill': 2,
             'judgment': 3, 'experiment': 4, 'calculation': 5}
    qs.sort(key=lambda x: order.get(x['question_type'], 9))
    return paper, qs


def _load_single_questions(qids):
    qs = []
    for qid in qids:
        qd = db.get_question(qid)
        if qd:
            qs.append(qd)
    return {'title': '选题讲评', 'description': ''}, qs


def export_paper_pptx(pid, out_path, include_answer=True,
                      include_solution=True, include_figures=True,
                      answer_on_new_slide=True, title=None,
                      section_slides=True, cover=True, theme=None):
    """导出一份试卷为 .pptx。返回 (out_path, stats)

    theme: 主题名（内置名 / 用户主题名 / JSON 路径）或 Theme 对象。
    """
    _FORMULA_FAILS.clear()
    paper, qs = _load_paper_questions(pid)
    return _build(paper, qs, out_path, include_answer, include_solution,
                  include_figures, answer_on_new_slide, title,
                  section_slides, cover, theme)


def export_questions_pptx(qids, out_path, include_answer=True,
                          include_solution=True, include_figures=True,
                          answer_on_new_slide=True, title=None,
                          section_slides=False, cover=True, theme=None):
    """导出一组题目（id 列表）为 .pptx。"""
    _FORMULA_FAILS.clear()
    paper, qs = _load_single_questions(qids)
    return _build(paper, qs, out_path, include_answer, include_solution,
                  include_figures, answer_on_new_slide, title,
                  section_slides, cover, theme)


def _build(paper, qs, out_path, include_answer, include_solution,
           include_figures, answer_on_new_slide, title,
           section_slides, cover, theme=None):
    global _THEME
    _THEME = load_theme(theme) if theme is not None else load_theme('默认蓝')
    t = _THEME

    prs = Presentation()
    prs.slide_width = SLIDE_W
    prs.slide_height = SLIDE_H

    total = len(qs)
    total_score = sum(q.get('_score') or 0 for q in qs)

    if cover:
        slide_cover(prs, title or t['cover_title'] or paper.get('title')
                    or '物理讲评',
                    t['cover_subtitle'] or paper.get('description') or '',
                    total, total_score)

    n_slides = 0
    last_type = None
    has_ans = include_answer and (include_solution or
                                  any(q.get('answer') for q in qs))
    for i, q in enumerate(qs, 1):
        typ = q.get('question_type')
        if section_slides and typ != last_type and typ in TYPE_LABELS:
            slide_section(prs, TYPE_LABELS[typ])
            last_type = typ

        qq = q
        if not include_figures:
            qq = dict(q)
            qq['attachments'] = []

        slide_question(prs, i, qq, total, mode='stem')
        n_slides += 1
        if has_ans and answer_on_new_slide:
            qq2 = dict(qq)
            if not include_solution:
                qq2['solution'] = ''
            slide_question(prs, i, qq2, total, mode='answer')
            n_slides += 1

    if has_ans:
        slide_end(prs)

    d = os.path.dirname(os.path.abspath(out_path))
    if d:
        os.makedirs(d, exist_ok=True)
    prs.save(out_path)

    stats = {
        'questions': total,
        # 真实页数（含配图续页）；n_slides 只数了题目页，会少算
        'slides': len(prs.slides._sldIdLst),
        'slides_by_question': n_slides,
        'total_score': total_score,
        'formula_fail': len(_FORMULA_FAILS),
        'formula_fails': list(_FORMULA_FAILS),
        'bytes': os.path.getsize(out_path) if os.path.exists(out_path) else 0,
    }
    return out_path, stats


# ---------------------------------------------------------------- CLI

def _cli():
    import argparse
    ap = argparse.ArgumentParser(description='导出讲题 PPT')
    ap.add_argument('paper_id', type=int)
    ap.add_argument('-o', '--out', default=None)
    ap.add_argument('--no-answer', action='store_true')
    ap.add_argument('--no-section', action='store_true')
    a = ap.parse_args()

    db.init_db()
    paper = db.get_paper(a.paper_id)
    if not paper:
        print(f'试卷 {a.paper_id} 不存在')
        return 1
    safe = ''.join(c for c in paper['title']
                   if c not in '\\/:*?"<>|').strip() or f'paper{a.paper_id}'
    out = a.out or os.path.join(db.export_dir(), safe + '.pptx')
    path, stats = export_paper_pptx(
        a.paper_id, out, include_answer=not a.no_answer,
        section_slides=not a.no_section)
    print(f'导出成功: {path}')
    print(f"  题目 {stats['questions']} 道，幻灯片 {stats['slides']} 页，"
          f"共 {stats['total_score']:g} 分")
    print(f"  公式渲染失败 {stats['formula_fail']} 个")
    for tex in stats['formula_fails'][:10]:
        print(f'    - {tex}')
    return 0


if __name__ == '__main__':
    sys.exit(_cli())
