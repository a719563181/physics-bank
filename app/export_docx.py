"""
export_docx.py — 试卷导出为 Word (.docx)
用 python-docx 直接写 OMML 原生公式，不依赖 pandoc 外部进程。
LaTeX 是唯一真源；导出时把 $...$ 转成 Word 原生公式对象。
"""
import os
import re
import sys
import json
import tempfile
import subprocess

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db

from docx import Document
from docx.shared import Pt, Cm, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn, nsmap
from docx.oxml import parse_xml

# ---------------------------------------------------------------- LaTeX -> OMML

_PANDOC = None


def find_pandoc():
    """定位 pandoc 可执行文件"""
    global _PANDOC
    if _PANDOC:
        return _PANDOC
    for cand in ('pandoc', 'pandoc.exe'):
        try:
            subprocess.run([cand, '--version'], capture_output=True, timeout=10)
            _PANDOC = cand
            return cand
        except Exception:
            pass
    # 常见安装位置
    import glob
    pats = [
        r'C:\Program Files\Pandoc\pandoc.exe',
        r'C:\Program Files (x86)\Pandoc\pandoc.exe',
        os.path.expandvars(r'%LOCALAPPDATA%\Pandoc\pandoc.exe'),
        os.path.expandvars(r'%APPDATA%\local\Pandoc\pandoc.exe'),
    ]
    for p in pats:
        if os.path.exists(p):
            _PANDOC = p
            return p
    for p in glob.glob(os.path.expandvars(r'%USERPROFILE%\scoop\apps\pandoc\**\pandoc.exe'),
                       recursive=True):
        _PANDOC = p
        return p
    return None


_MATH_CACHE = {}

# 记录所有降级为纯文本的公式，导出报告里回显，避免静默失败
_FALLBACKS = []


def latex_to_omml(latex):
    """
    单条 LaTeX 公式 -> OMML XML 字符串。
    用 pandoc 转一个最小 md，再抽出 <m:oMath> 片段。
    返回 None 表示失败。
    """
    if latex in _MATH_CACHE:
        return _MATH_CACHE[latex]
    pd = find_pandoc()
    if not pd:
        return None

    md = f'${latex}$\n'
    tmpd = tempfile.mkdtemp(prefix='omml_')
    src = os.path.join(tmpd, 'f.md')
    dst = os.path.join(tmpd, 'f.docx')
    try:
        with open(src, 'w', encoding='utf-8') as f:
            f.write(md)
        r = subprocess.run([pd, src, '-o', dst],
                           capture_output=True, timeout=60)
        if r.returncode != 0:
            _MATH_CACHE[latex] = None
            return None
        doc = Document(dst)
        # 从 document.xml 里抠出 m:oMath
        xml = doc.element.body.xml
        m = re.search(r'<m:oMath[ >].*?</m:oMath>', xml, re.S)
        if not m:
            _MATH_CACHE[latex] = None
            return None
        frag = m.group(0)
        # 片段来自 pandoc 的 document.xml，自身不带 xmlns:m，parse_xml 会报
        # "Namespace prefix m on oMath is not defined" —— 必须手动补上。
        if 'xmlns:m' not in frag:
            frag = frag.replace(
                '<m:oMath',
                '<m:oMath xmlns:m="http://schemas.openxmlformats.org/'
                'officeDocument/2006/math"', 1)
        _MATH_CACHE[latex] = frag
        return frag
    except Exception:
        _MATH_CACHE[latex] = None
        return None
    finally:
        try:
            import shutil
            shutil.rmtree(tmpd, ignore_errors=True)
        except Exception:
            pass


_TEX_SPLIT = re.compile(r'\$(.+?)\$', re.S)


def _add_anchor_picture(doc, par, att, default_cm=7.5):
    """把一张图作为**行内图**追加到已有段落 par 上。

    行内图（run 级 add_picture）而不是新建居中段落 —— 因为图是
    插在文字中间，标点/后续文字应该紧跟在图后面。
    width_pct 设为图片占正文栏宽的百分比（0 = 用 default_cm）。
    """
    path = db.abs_attach_path(att.get('rel_path') or att.get('path'))
    if not path or not os.path.exists(path):
        return None
    pct = int(att.get('width_pct') or 0)
    try:
        from docx.shared import Cm as _Cm
        from PIL import Image as _Img
        with _Img.open(path) as im:
            px_w, px_h = im.size
        if pct:
            col_cm = 16.0                # A4 正文栏宽（左右各 2.5cm 边距）
            w = _Cm(max(1.0, col_cm * pct / 100.0))
        else:
            # 不超栏宽、不超过默认 7.5cm，保持宽高比
            w_cm = min(default_cm, 16.0)
            w = _Cm(w_cm)
        # 若图片很宽的，自动缩到栏宽
        if pct:
            run = par.add_run()
            run.add_picture(path, width=w)
        else:
            run = par.add_run()
            run.add_picture(path, width=w)
        return run
    except Exception:
        return None


def add_rich_text(par, text, base_font='宋体', size=10.5):
    """
    往段落里写「含 $LaTeX$ 的混合文本」：
    普通文字用 run，公式用 OMML 原生对象。
    公式渲染失败则退化为原文 $...$。
    """
    pos = 0
    for m in _TEX_SPLIT.finditer(text):
        head = text[pos:m.start()]
        if head:
            _add_plain(par, head, base_font, size)
        tex = m.group(1).strip()
        frag = latex_to_omml(tex) if tex else None
        if frag:
            try:
                par._p.append(parse_xml(frag))
            except Exception as e:
                _FALLBACKS.append((tex, repr(e)))
                _add_plain(par, f'${tex}$', base_font, size)
        else:
            if tex:
                _FALLBACKS.append((tex, 'pandoc 未生成 oMath'))
            _add_plain(par, f'${tex}$', base_font, size)
        pos = m.end()
    tail = text[pos:]
    if tail:
        _add_plain(par, tail, base_font, size)


def _add_plain(par, text, font='宋体', size=10.5):
    if not text:
        return
    run = par.add_run(text)
    run.font.name = font
    run.font.size = Pt(size)
    run._element.rPr.rFonts.set(qn('w:eastAsia'), font)
    return run


# ---------------------------------------------------------------- 试卷生成

def make_question_doc(template=None):
    """新建一个带中文字体默认设置的 Document"""
    doc = Document()
    st = doc.styles['Normal']
    st.font.name = '宋体'
    st.font.size = Pt(10.5)
    st.element.rPr.rFonts.set(qn('w:eastAsia'), '宋体')
    return doc


def _set_header(doc, title, subtitle=''):
    h = doc.add_paragraph()
    h.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = h.add_run(title)
    r.font.size = Pt(16)
    r.bold = True
    r.font.name = '黑体'
    r._element.rPr.rFonts.set(qn('w:eastAsia'), '黑体')
    if subtitle:
        s = doc.add_paragraph()
        s.alignment = WD_ALIGN_PARAGRAPH.CENTER
        sr = s.add_run(subtitle)
        sr.font.size = Pt(10.5)
        sr.font.name = '宋体'
        sr._element.rPr.rFonts.set(qn('w:eastAsia'), '宋体')


def export_paper(pid, out_path, include_answer=True, include_solution=True,
                 include_figures=True, title=None, order_by_type=True):
    """
    导出一份试卷为 .docx。
    返回 (out_path, stats_dict)
    """
    _FALLBACKS.clear()
    paper = db.get_paper(pid)
    if not paper:
        raise ValueError(f'试卷 {pid} 不存在')

    qs = []
    for q in paper['questions']:
        qd = db.get_question(q['id'])
        if qd:
            qd['_score'] = q.get('pq_score') or 0
            qs.append(qd)

    if order_by_type:
        # 单选 -> 多选 -> 填空 -> 判断 -> 实验 -> 计算
        order = {'choice_single': 0, 'choice_multi': 1, 'fill': 2,
                 'judgment': 3, 'experiment': 4, 'calculation': 5}
        qs.sort(key=lambda x: order.get(x['question_type'], 9))

    doc = make_question_doc()

    t = title or paper['title']
    _set_header(doc, t)
    if paper.get('description'):
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        _add_plain(p, paper['description'], '楷体', 10.5)

    total_score = sum(q['_score'] for q in qs)

    # 题型分组编号
    TYPE_NAMES = {'choice_single': '单选题', 'choice_multi': '多选题',
                  'fill': '填空题', 'judgment': '判断题',
                  'experiment': '实验题', 'calculation': '计算题'}
    groups = {}
    for q in qs:
        groups.setdefault(q['question_type'], []).append(q)
    if not order_by_type:
        groups = {'mixed': qs}

    n_formula_ok = 0
    n_formula_fail = 0
    n_figs = 0
    qnum = 0
    answer_book = []

    for tkey, items in groups.items():
        # 大题标题
        if tkey != 'mixed':
            tscore = sum(q['_score'] for q in items)
            h = doc.add_paragraph()
            head = f"{TYPE_NAMES.get(tkey, tkey)}"
            if tscore:
                head += f"（共 {len(items)} 小题，共 {tscore:g} 分）"
            else:
                head += f"（共 {len(items)} 小题）"
            hr = h.add_run(head)
            hr.bold = True
            hr.font.size = Pt(12)
            hr.font.name = '黑体'
            hr._element.rPr.rFonts.set(qn('w:eastAsia'), '黑体')

        for q in items:
            qnum += 1
            # 题号 + 题干（选项已并入题干文本，先切出来）
            stem = (q['stem'] or '').strip()
            lead_text, stem_opts = db.split_stem_options(stem)
            # 兼容旧数据：选项仍在选项表里时用表里的
            legacy_opts = []
            if not stem_opts and q.get('options'):
                legacy_opts = [(o['label'], o['content'] or '') for o in q['options']]
            opts_all = stem_opts or legacy_opts
            if stem.startswith(('（', '(')):
                pass
            par = doc.add_paragraph()
            par.paragraph_format.space_after = Pt(2)
            sc = f"（{q['_score']:g}分）" if q['_score'] else ''
            lead = f"{qnum}. {sc}"
            _add_plain(par, lead, '宋体', 10.5)

            # ── 题干正文 + 按锚点插回的图片（所见即所得）──
            # 注意：锚点 offset 按完整 stem（含选项行）计算，所以图可能在
            # 「选项区」里面或后面。不能因为「题干区字符数用完」就 break，
            # 否则尾部的图会被静默丢掉（历史 bug：第 3 张起的图全消失）。
            segs = db.split_text_by_anchors(
                q['stem'] or '',
                q['attachments'] if include_figures else [], 'stem')
            budget = len(lead_text)        # 题干区还没写掉的字符数
            opt_segs = []                  # 落在选项区之后的图文片段
            for kind, val in segs:
                if budget > 0:
                    if kind == 'text':
                        piece = val[:budget]
                        budget -= len(piece)
                        if piece:
                            ok0 = _count_math(piece)
                            add_rich_text(par, piece, '宋体', 10.5)
                            n_formula_fail += _count_math_fail(piece)
                            n_formula_ok += (ok0 - _count_math_fail(piece))
                    else:
                        if _add_anchor_picture(doc, par, val) is not None:
                            n_figs += 1
                    continue
                # 题干区已写完，剩下的先在手里拿着，等选项写完再排
                opt_segs.append((kind, val))

            # 选项（选择题）：用「答案」字段里的字母决定是否加粗标记
            if opts_all:
                ans_set = db.parse_answer_letters(
                    q.get('answer') or '', [o[0] for o in opts_all])
                for lab, content in opts_all:
                    op = doc.add_paragraph()
                    op.paragraph_format.space_after = Pt(0)
                    op.paragraph_format.left_indent = Cm(0.74)
                    _add_plain(op, f"{lab}. ", '宋体', 10.5)
                    add_rich_text(op, content or '', '宋体', 10.5)

            # 选项区之后的图文片段（前面攒下的），继续按序输出
            for kind, val in opt_segs:
                if kind == 'text':
                    if val.strip():
                        tp = doc.add_paragraph()
                        tp.paragraph_format.space_after = Pt(2)
                        ok1 = _count_math(val)
                        add_rich_text(tp, val, '宋体', 10.5)
                        n_formula_fail += _count_math_fail(val)
                        n_formula_ok += (ok1 - _count_math_fail(val))
                else:
                    if _add_anchor_picture(doc, par, val) is not None:
                        n_figs += 1

            # 配图：只处理**没有锚点**的图（旧数据 / anchor_offset<0）。
            # 有锚点的图已经在上面随正文插到原位了，这里再插会重复。
            if include_figures:
                for a in q['attachments']:
                    off = a.get('anchor_offset')
                    if off is not None and int(off) >= 0:
                        continue
                    # 没锚点但明确属于解析区的：交给答案页渲染，不倒堆到题干下
                    if (a.get('anchor_offset') is None
                            and (a.get('field') or 'stem') == 'solution'):
                        continue
                    path = db.abs_attach_path(a['rel_path'])
                    if os.path.exists(path):
                        try:
                            ip = doc.add_paragraph()
                            ip.alignment = WD_ALIGN_PARAGRAPH.CENTER
                            ip.add_run().add_picture(path, width=Cm(7.5))
                            n_figs += 1
                        except Exception:
                            pass

            answer_book.append({
                'num': qnum, 'id': q['id'],
                'answer': q['answer'] or '', 'solution': q['solution'] or '',
                'attachments': q.get('attachments') or []})

    # ---------------- 答案页 ----------------
    if include_answer and answer_book:
        doc.add_page_break()
        _set_header(doc, f"{t} — 参考答案")
        if include_solution:
            for ab in answer_book:
                ap = doc.add_paragraph()
                ap.paragraph_format.space_after = Pt(2)
                _add_plain(ap, f"{ab['num']}. ", '宋体', 10.5)
                if ab['answer']:
                    _add_plain(ap, "答案：", '宋体', 10.5).bold = True
                    add_rich_text(ap, ab['answer'], '宋体', 10.5)
                if ab['solution']:
                    sp = doc.add_paragraph()
                    sp.paragraph_format.left_indent = Cm(0.74)
                    sp.paragraph_format.space_after = Pt(4)
                    _add_plain(sp, "解析：", '楷体', 10)
                    # 解析里也可以插图（field='solution'），按锚点插回原位
                    if include_figures and ab.get('attachments'):
                        segs2 = db.split_text_by_anchors(
                            ab['solution'], ab['attachments'], 'solution')
                        for kind2, val2 in segs2:
                            if kind2 == 'text':
                                if val2:
                                    ok1 = _count_math(val2)
                                    add_rich_text(sp, val2, '楷体', 10)
                                    n_formula_fail += _count_math_fail(val2)
                                    n_formula_ok += (ok1 - _count_math_fail(val2))
                            else:
                                if _add_anchor_picture(doc, sp, val2) is not None:
                                    n_figs += 1
                    else:
                        add_rich_text(sp, ab['solution'], '楷体', 10)
        else:
            buf = []
            for ab in answer_book:
                buf.append(f"{ab['num']}.{ab['answer']}")
            p = doc.add_paragraph()
            add_rich_text(p, '   '.join(buf), '宋体', 10.5)

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    doc.save(out_path)

    stats = {
        'paper_id': pid, 'title': t, 'out': out_path,
        'questions': len(qs), 'total_score': total_score,
        'formula_ok': n_formula_ok, 'formula_fail': n_formula_fail,
        'figures': n_figs,
        'pandoc': find_pandoc() or '(未找到)',
        'fallbacks': list(_FALLBACKS),
    }
    return out_path, stats


def _count_math(text):
    return len(_TEX_SPLIT.findall(text or ''))


def _count_math_fail(text):
    """统计渲染失败的公式数"""
    n = 0
    for m in _TEX_SPLIT.finditer(text or ''):
        tex = m.group(1).strip()
        if tex and not latex_to_omml(tex):
            n += 1
    return n


if __name__ == '__main__':
    db.init_db()
    pid = int(sys.argv[1]) if len(sys.argv) > 1 else None
    out = sys.argv[2] if len(sys.argv) > 2 else os.path.join(
        db.export_dir(), 'test.docx')
    if pid:
        p, s = export_paper(pid, out)
        print(json.dumps(s, ensure_ascii=False, indent=2))
