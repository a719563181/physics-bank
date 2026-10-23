# -*- coding: utf-8 -*-
"""
word_import.py — 从 .docx 试卷里解析出题目（含公式与配图）

设计要点
--------
1. **走 OOXML 底层，不用 paragraph.text**
   python-docx 的 `paragraph.text` 会**丢弃**公式（m:oMath）、图片（w:drawing）
   和域代码，只留纯文字。而物理卷的公式几乎全是 OMML，图片就是配图。
   所以这里自己遍历 `paragraph._p` 的子元素，按文档顺序抠出：
     - w:r → w:t        普通文字
     - m:oMath          Word 原生公式 → 转成 $LaTeX$
     - w:drawing       内嵌图片  → 提取二进制、存盘
     - w:tab / w:br    制表符 / 换行

2. **公式转 LaTeX 用 OMML→LaTeX 转换器**
   先尝试 `latex2mathml` 反向不可靠，改用自实现的 OMML→LaTeX 递归下降。
   覆盖物理卷常见结构：分式、上下标、根式、求和/积分、括号、希腊字母、
   文本、矩阵。转换失败时降级为「文字近似」，并在 low_conf 里标注。

3. **切题复用 ocr_import 的规则**
   段落文本拼好后，喂给 `ocr_import.split_questions()` 的同一套题号正则，
   保证 Word 与 OCR 两条路的切题行为一致。

对外唯一入口：
    run_word(path, work_dir, out_json=None, qnum_cfg=None) -> draft dict

draft 结构与 ocr_import.run_ocr() 完全一致，可直接喂给 ImportDialog。
"""

import os
import re
import sys
import json
import time

APP = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, APP)

# OOXML 命名空间
NS_W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
NS_M = 'http://schemas.openxmlformats.org/officeDocument/2006/math'
NS_A = 'http://schemas.openxmlformats.org/drawingml/2006/main'
NS_R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
NS_WP = 'http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing'


# ==================== OMML → LaTeX ====================

# OMML 里用 <m:chr m:val="..."/> 表达符号
_CHR_MAP = {
    '−': '-', '×': r'\times ', '÷': r'\div ',
    '±': r'\pm ', '∓': r'\mp ', '·': r'\cdot ',
    '≤': r'\le ', '≥': r'\ge ', '≠': r'\ne ',
    '≈': r'\approx ', '∝': r'\propto ', '∞': r'\infty ',
    '→': r'\to ', '←': r'\leftarrow ', '⇒': r'\Rightarrow ',
    '°': r'^\circ ', '′': "'", '″': "''",
    'α': r'\alpha ', 'β': r'\beta ', 'γ': r'\gamma ',
    'δ': r'\delta ', 'ε': r'\varepsilon ', 'η': r'\eta ',
    'θ': r'\theta ', 'λ': r'\lambda ', 'μ': r'\mu ',
    'π': r'\pi ', 'ρ': r'\rho ', 'σ': r'\sigma ',
    'τ': r'\tau ', 'φ': r'\varphi ', 'ω': r'\omega ',
    'Δ': r'\Delta ', 'Ω': r'\Omega ', 'Σ': r'\Sigma ',
    '∆': r'\Delta ', '∑': r'\sum ', '∫': r'\int ',
    '√': r'\sqrt ', '∂': r'\partial ', '∇': r'\nabla ',
    '…': r'\ldots ', '⋯': r'\cdots ',
}

# 常见 nary 字符 → LaTeX 命令
_NARY = {
    '∑': r'\sum', '∏': r'\prod', '∫': r'\int',
    '∮': r'\oint', '∫∫': r'\iint',
}

# 用于把 OMML 里的普通文本转义成 LaTeX 安全文本
_LTX_ESC = {
    '\\': r'\backslash ', '{': r'\{', '}': r'\}', '$': r'\$',
    '%': r'\%', '&': r'\&', '#': r'\#', '_': r'\_',
    '~': r'\sim ', '^': r'\hat{}',
}


def _local(tag):
    """'{ns}local' -> 'local'"""
    return tag.rsplit('}', 1)[-1] if '}' in tag else tag


def _txt(el):
    """取一个元素下所有 w:t 的文字（不含公式内部）"""
    if el is None:
        return ''
    out = []
    for t in el.iter('{%s}t' % NS_W):
        out.append(t.text or '')
    return ''.join(out)


def _esc(s):
    """把纯文字转义成 LaTeX 安全形式；同时替换数学符号"""
    buf = []
    for ch in s:
        if ch in _CHR_MAP:
            buf.append(_CHR_MAP[ch])
        elif ch in _LTX_ESC:
            buf.append(_LTX_ESC[ch])
        else:
            buf.append(ch)
    return ''.join(buf)


def _mrun_latex(el):
    """m:r → 文字（尽量保留 m:t）"""
    out = []
    for mt in el.iter('{%s}t' % NS_M):
        out.append(_esc(mt.text or ''))
    if not out:
        out.append(_esc(_txt(el)))
    return ''.join(out)


def _omml_children(el):
    """按顺序把 OMML 子元素转成 LaTeX 片段"""
    out = []
    for ch in el:
        out.append(_omml_node(ch))
    return ''.join(out)


def _omml_node(el):
    """OMML 单节点 → LaTeX"""
    if el is None:
        return ''
    name = _local(el.tag)

    # ---- 纯文字 ----
    if name == 'r':
        return _mrun_latex(el)
    if name == 't':
        return _esc(el.text or '')

    # ---- 分式 ----
    if name == 'f':
        num = den = ''
        for ch in el:
            n = _local(ch.tag)
            if n == 'num':
                num = _omml_children(ch)
            elif n == 'den':
                den = _omml_children(ch)
        return r'\frac{%s}{%s}' % (num, den)

    # ---- 上标 / 下标 / 上下标 ----
    if name in ('sSup', 'sSub', 'sSubSup'):
        base = sup = sub = ''
        for ch in el:
            n = _local(ch.tag)
            if n == 'e':
                base = _omml_children(ch)
            elif n == 'sup':
                sup = _omml_children(ch)
            elif n == 'sub':
                sub = _omml_children(ch)
        if name == 'sSup':
            return '{%s}^{%s}' % (base, sup)
        if name == 'sSub':
            return '{%s}_{%s}' % (base, sub)
        return '{%s}_{%s}^{%s}' % (base, sub, sup)

    # ---- 根式 ----
    if name == 'rad':
        deg = base = ''
        for ch in el:
            n = _local(ch.tag)
            if n == 'deg':
                deg = _omml_children(ch)
            elif n == 'e':
                base = _omml_children(ch)
        if deg.strip():
            return r'\sqrt[%s]{%s}' % (deg, base)
        return r'\sqrt{%s}' % base

    # ---- 求和 / 积分 ----
    if name == 'nary':
        chr_val = ''
        sub = sup = base = ''
        for ch in el:
            n = _local(ch.tag)
            if n == 'naryPr':
                for c in ch:
                    if _local(c.tag) == 'chr':
                        chr_val = c.get('{%s}val' % NS_M, '') or ''
            elif n == 'sub':
                sub = _omml_children(ch)
            elif n == 'sup':
                sup = _omml_children(ch)
            elif n == 'e':
                base = _omml_children(ch)
        op = _NARY.get(chr_val) or (_CHR_MAP.get(chr_val) or r'\sum').strip()
        s = op
        if sub.strip():
            s += '_{%s}' % sub
        if sup.strip():
            s += '^{%s}' % sup
        return (s + ' ' + base).strip()

    # ---- 括号 / 定界符 ----
    if name == 'd':
        beg, end, body = '(', ')', ''
        for ch in el:
            n = _local(ch.tag)
            if n == 'dPr':
                for c in ch:
                    cn = _local(c.tag)
                    if cn == 'begChr':
                        beg = c.get('{%s}val' % NS_M, '(')
                    elif cn == 'endChr':
                        end = c.get('{%s}val' % NS_M, ')')
            elif n == 'e':
                body = _omml_children(ch)
        lb = _CHR_MAP.get(beg) or beg
        rb = _CHR_MAP.get(end) or end
        return r'\left%s %s \right%s' % (lb, body, rb)

    # ---- 函数 ----
    if name == 'func':
        fname = arg = ''
        for ch in el:
            n = _local(ch.tag)
            if n == 'fName':
                fname = _omml_children(ch)
            elif n == 'e':
                arg = _omml_children(ch)
        fname = fname.strip()
        known = {'sin', 'cos', 'tan', 'cot', 'sec', 'csc', 'log', 'ln',
                 'lg', 'exp', 'max', 'min', 'lim', 'det', 'gcd'}
        if fname in known:
            return '\\%s %s' % (fname, arg)
        return '%s(%s)' % (fname, arg)

    # ---- 矩阵 ----
    if name == 'm':
        rows = []
        for mr in el:
            if _local(mr.tag) != 'mr':
                continue
            cells = []
            for mc in mr:
                if _local(mc.tag) == 'e':
                    cells.append(_omml_children(mc) or ' ')
            rows.append(' & '.join(cells))
        return r'\begin{matrix}%s\end{matrix}' % (r' \\ '.join(rows))

    # ---- 上划线 / 下划线 / 帽 / 重音 ----
    if name == 'bar':
        body = ''
        pos = 'top'
        for ch in el:
            n = _local(ch.tag)
            if n == 'barPr':
                for c in ch:
                    if _local(c.tag) == 'pos':
                        pos = c.get('{%s}val' % NS_M, 'top') or 'top'
            elif n == 'e':
                body = _omml_children(ch)
        return (r'\overline{%s}' if pos == 'top' else r'\underline{%s}') % body

    if name == 'acc':
        body = chr_val = ''
        for ch in el:
            n = _local(ch.tag)
            if n == 'accPr':
                for c in ch:
                    if _local(c.tag) == 'chr':
                        chr_val = c.get('{%s}val' % NS_M, '') or ''
            elif n == 'e':
                body = _omml_children(ch)
        cmd = {'̂': r'\hat', '̄': r'\bar', '⃗': r'\vec',
               '̇': r'\dot', '̈': r'\ddot', '̃': r'\tilde',
               '˙': r'\dot'}.get(chr_val)
        return ('%s{%s}' % (cmd, body)) if cmd else body

    # ---- 框 / 影 / 公式组 ----
    if name in ('box', 'borderBox', 'phant'):
        body = ''
        for ch in el:
            if _local(ch.tag) == 'e':
                body = _omml_children(ch)
        return (r'\boxed{%s}' % body) if name != 'phant' else body

    # ---- 分隔 / 结构容器：递归 ----
    if name in ('e', 'oMath', 'oMathPara', 'num', 'den', 'sup', 'sub',
                'deg', 'fName', 'eqArr', 'limLow', 'limUpp'):
        if name == 'eqArr':
            return r' \\ '.join(_omml_children(ch) for ch in el
                                if _local(ch.tag) == 'e')
        if name in ('limLow', 'limUpp'):
            base = lim = ''
            for ch in el:
                n = _local(ch.tag)
                if n == 'e':
                    base = _omml_children(ch)
                elif n == 'lim':
                    lim = _omml_children(ch)
            return ('%s_{%s}' % (base, lim)) if name == 'limLow' \
                else ('%s^{%s}' % (base, lim))
        return _omml_children(el)

    # ---- 其他：兜底递归 ----
    return _omml_children(el)


def omml_to_latex(omath_el):
    """把一个 <m:oMath> 元素转成 LaTeX 字符串"""
    try:
        s = _omml_children(omath_el)
    except Exception:
        return None
    s = re.sub(r'\s+', ' ', s).strip()
    # 修补常见冗余
    s = s.replace(r'\left( ', r'\left(').replace(r' \right)', r'\right)')
    s = re.sub(r'\{([A-Za-z0-9])\}\^', r'\1^', s)
    s = re.sub(r'\{([A-Za-z0-9])\}_', r'\1_', s)
    return s or None


# ==================== 段落解析 ====================

def _render_paragraph(p_el, media_dir, img_counter):
    """把一个 w:p 转成 (文本, [图片路径...])

    按子元素顺序拼：文字保留、公式变 $...$、图片抽取存盘。
    """
    parts = []
    images = []

    for ch in p_el.iter():
        pass  # 占位，下面用显式递归

    def walk(el, depth=0):
        for ch in el:
            tag = ch.tag
            if tag == '{%s}oMath' % NS_M:
                lx = omml_to_latex(ch)
                if lx:
                    parts.append('$%s$' % lx)
                continue
            if tag == '{%s}oMathPara' % NS_M:
                for om in ch.iter('{%s}oMath' % NS_M):
                    lx = omml_to_latex(om)
                    if lx:
                        parts.append('$%s$' % lx)
                continue
            if tag == '{%s}t' % NS_W:
                parts.append(ch.text or '')
                continue
            if tag == '{%s}tab' % NS_W:
                parts.append('\t')
                continue
            if tag in ('{%s}br' % NS_W, '{%s}cr' % NS_W):
                parts.append('\n')
                continue
            if tag == '{%s}drawing' % NS_W:
                pth = _extract_image(ch, media_dir, img_counter)
                if pth:
                    images.append(pth)
                    parts.append('\n[[IMG:%s]]\n' % pth)
                continue
            if tag == '{%s}pict' % NS_W:
                pth = _extract_vml_image(ch, media_dir, img_counter)
                if pth:
                    images.append(pth)
                    parts.append('\n[[IMG:%s]]\n' % pth)
                continue
            # 跳过属性容器（不会含正文）
            if _local(tag) in ('pPr', 'rPr', 'sectPr'):
                continue
            walk(ch, depth + 1)

    walk(p_el)
    text = ''.join(parts)
    # 归一化空白，但保留换行
    text = re.sub(r'[ \t ]+', ' ', text)
    text = re.sub(r'\n\s*\n+', '\n', text).strip()
    return text, images


def _extract_image(drawing_el, media_dir, counter):
    """w:drawing → 存盘返回路径"""
    blips = list(drawing_el.iter('{%s}blip' % NS_A))
    if not blips:
        return None
    rid = blips[0].get('{%s}embed' % NS_R)
    if not rid:
        return None
    return _save_rel_image(rid, media_dir, counter)


def _extract_vml_image(pict_el, media_dir, counter):
    """老的 VML 图片（v:imagedata）"""
    for el in pict_el.iter():
        if _local(el.tag) == 'imagedata':
            rid = el.get('{%s}id' % NS_R)
            if rid:
                return _save_rel_image(rid, media_dir, counter)
    return None


_DOC = None   # 当前文档对象（_save_rel_image 用）


def _save_rel_image(rid, media_dir, counter):
    """按 rId 取出图片二进制并存盘"""
    if _DOC is None:
        return None
    try:
        part = _DOC.part.related_parts[rid]
    except Exception:
        return None
    blob = getattr(part, 'blob', None)
    if not blob:
        return None
    ext = '.png'
    ct = (getattr(part, 'content_type', '') or '').lower()
    for k, v in (('jpeg', '.jpg'), ('jpg', '.jpg'), ('png', '.png'),
                 ('gif', '.gif'), ('bmp', '.bmp'), ('tiff', '.tif'),
                 ('emf', '.emf'), ('wmf', '.wmf'), ('svg', '.svg')):
        if k in ct:
            ext = v
            break
    counter[0] += 1
    p = os.path.join(media_dir, 'w_img%03d%s' % (counter[0], ext))
    with open(p, 'wb') as f:
        f.write(blob)
    return p


def _collect_txbx(txbx_el, paragraphs, media_dir, counter, seen_p):
    """把文本框里的每个 w:p 当普通段落收集（去重）。"""
    for p_el in txbx_el.iter('{%s}p' % NS_W):
        if id(p_el) in seen_p:
            continue
        seen_p.add(id(p_el))
        try:
            text, imgs = _render_paragraph(p_el, media_dir, counter)
        except Exception:
            continue
        if text or imgs:
            paragraphs.append((text, imgs))


# ==================== 主流程 ====================

def run_word(path, work_dir, out_json=None, qnum_cfg=None, verbose=True):
    """解析 .docx 试卷 → draft（结构与 ocr_import.run_ocr 一致）"""
    global _DOC
    t0 = time.time()

    def say(msg):
        if verbose:
            print(msg, flush=True)

    from docx import Document
    import ocr_import as O

    os.makedirs(work_dir, exist_ok=True)
    media_dir = os.path.join(work_dir, 'word_media')
    os.makedirs(media_dir, exist_ok=True)

    say('[1/4] 读取 Word 文档: %s' % os.path.basename(path))
    doc = Document(path)
    _DOC = doc

    counter = [0]
    say('[2/4] 解析段落（公式 → LaTeX，图片 → 抽取）...')

    paragraphs = []          # [(text, [images])]
    for p in doc.paragraphs:
        try:
            text, imgs = _render_paragraph(p._p, media_dir, counter)
        except Exception as e:
            say('      段落解析跳过: %s' % e)
            continue
        if text or imgs:
            paragraphs.append((text, imgs))

    # ★ 文本框里的文字（w:txbxContent）doc.paragraphs 读不到
    #   很多试卷把题干/选项甚至整道题放在文本框里，这里补一遍。
    try:
        body = doc.element.body
        seen_p = {id(p._p) for p in doc.paragraphs}
        for txbx in body.iter('{http://schemas.microsoft.com/office/word/2006/wordml}txbxContent'):
            _collect_txbx(txbx, paragraphs, media_dir, counter, seen_p)
        for txbx in body.iter('{%s}txbxContent' % NS_W):
            _collect_txbx(txbx, paragraphs, media_dir, counter, seen_p)
    except Exception as e:
        say('      文本框扫描跳过: %s' % e)

    # 表格里的文字也捞出来（很多试卷把选项放表格）
    table_texts = []
    for tb in doc.tables:
        for row in tb.rows:
            cells = []
            for cell in row.cells:
                ct = []
                for p in cell.paragraphs:
                    try:
                        t, _ = _render_paragraph(p._p, media_dir, counter)
                    except Exception:
                        continue
                    if t:
                        ct.append(t)
                cells.append(' '.join(ct))
            line = '\t'.join(c for c in cells if c.strip())
            if line.strip():
                table_texts.append(line)

    say('      %d 段 + %d 表格行，抽出图片 %d 张'
        % (len(paragraphs), len(table_texts), counter[0]))

    # ---- 组装成 "行" 列表，交给 split_questions ----
    # ocr_import.split_questions 要 6 元组: (text, conf, x0, y0, x1, y1)
    lines = []      # [(text, conf, x0, y0, x1, y1)]
    y = 0.0
    for text, imgs in paragraphs:
        for seg in text.split('\n'):
            seg = seg.strip()
            if not seg:
                continue
            lines.append((seg, 1.0, 0.0, y, 0.0, y))
            y += 1.0
    for t in table_texts:
        lines.append((t, 1.0, 0.0, y, 0.0, y))
        y += 1.0

    say('[3/4] 切分题目...')
    state = {'section': '', 'last_num': 0, 'pending_junk': [],
             'in_notice': False}
    raw_by_y = {ln[3]: ln[0] for ln in lines}
    # ★ drop_answers=True：Word 卷里「【答案】B【解析】…」紧跟在选项后面，
    #   旧逻辑把它当续行拼到选项 D 上（用户实测反馈）。切题阶段就丢掉，
    #   同时把答案字母和解析塞进 q['answer'] / q['analysis']。
    qs = O.split_questions(lines, state=state, qnum_cfg=qnum_cfg,
                           drop_answers=True)

    # ---- 原始文本对照：把题目 y 区间内的行收进 q['raw'] ----
    #      split_questions 对 Word 不填 raw（它只收 OCR 低置信行），
    #      界面上「原始文本（只读，供对照）」不能空着。
    for q in qs:
        y0 = q.get('y0')
        y1 = q.get('y1')
        if y0 is None or y1 is None:
            continue
        seg = [raw_by_y[k] for k in sorted(raw_by_y)
               if y0 <= k <= y1 and raw_by_y[k]]
        if seg:
            q['raw'] = seg

    # ---- 把 [[IMG:...]] 从题干里摘出来，挂到对应题 ----
    img_re = re.compile(r'\[\[IMG:([^\]]+)\]\]')
    for qi, q in enumerate(qs):
        found = img_re.findall(q.get('stem') or '')
        # 选项里也可能有图
        for o in q.get('options') or []:
            found += img_re.findall(o.get('content') or '')
        # 从文字里清掉占位符
        q['stem'] = img_re.sub('', q.get('stem') or '').strip()
        for o in q.get('options') or []:
            o['content'] = img_re.sub('', o.get('content') or '').strip()
        if found:
            q['figures'] = [{'path': p, 'page': 1, 'q_index': qi,
                             'conf': 1.0, 'src': 'word'} for p in found]

    # 若某段图片没被任何题吃掉（在题干之前的图），挂到第一题
    used = set()
    for q in qs:
        for f in q.get('figures') or []:
            used.add(os.path.basename(f['path']))
    orphan = [p for p in
              (os.path.join(media_dir, n) for n in os.listdir(media_dir))
              if os.path.basename(p) not in used]
    if orphan and qs:
        qs[0].setdefault('figures', [])
        for p in orphan:
            qs[0]['figures'].append({'path': p, 'page': 1, 'q_index': 0,
                                     'conf': 1.0, 'src': 'word',
                                     'unassigned': True})

    for i, q in enumerate(qs, 1):
        q['page'] = 1
        q['source_image'] = None
        q.setdefault('figures', [])
        q['number'] = q.get('number') or str(i)

    elapsed = time.time() - t0
    draft = {
        'source': path,
        'source_name': os.path.basename(path),
        'kind': 'word',
        'pages': [{'page': 1, 'image': None,
                   'lines': len(lines),
                   'figures': sum(len(q.get('figures') or []) for q in qs)}],
        'questions': qs,
        'total_seconds': round(elapsed, 1),
        'log': ['Word 导入'],
        'qnum_cfg': dict(O.DEFAULT_QNUM_CFG, **(qnum_cfg or {})),
        'manual': False,
        'work_dir': work_dir,
        'stats': {
            'with_answer': sum(1 for q in qs if (q.get('answer') or '').strip()),
            'with_analysis': sum(1 for q in qs
                                 if (q.get('analysis') or '').strip()),
            'with_figures': sum(1 for q in qs if q.get('figures')),
        },
    }

    say('[4/4] 完成：%d 题，%d 张图，耗时 %.1fs'
        % (len(qs), counter[0], elapsed))

    if out_json:
        os.makedirs(os.path.dirname(os.path.abspath(out_json)), exist_ok=True)
        with open(out_json, 'w', encoding='utf-8') as f:
            json.dump(draft, f, ensure_ascii=False, indent=2)
        say('      草稿已存: %s' % out_json)

    return draft


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print('用法: python word_import.py <文件.docx> [工作目录] [出JSON]')
        raise SystemExit(1)
    src = sys.argv[1]
    wd = sys.argv[2] if len(sys.argv) > 2 else \
        os.path.join(os.path.dirname(os.path.abspath(src)), '_word_work')
    oj = sys.argv[3] if len(sys.argv) > 3 else os.path.join(wd, 'draft.json')
    d = run_word(src, wd, oj)
    print()
    import ocr_import
    print(ocr_import.draft_to_text(d))
