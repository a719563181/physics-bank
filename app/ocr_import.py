"""
ocr_import.py — 扫描件 / 照片 / PDF 批量导入（OCR）
在 venv-ocr 环境里跑（含 paddleocr，不含 torch）。

设计要点：
1. 按「几何位置」重组阅读顺序，而不是直接用 API 返回顺序
   —— 实测 API 顺序对双栏试卷很差，必须自己排。
2. 自动切分题目：识别以「数字.」开头的行作为新题起点，
   选项行（A. B. C. D.）归入当前题的选项。
3. 输出「待校对草稿」（JSON + 文本），人工校对后再入库 —— 
   物理题正确性容不得自动化模糊地带。
4. 公式/符号 OCR 不可靠，草稿里标记低置信度片段供人工重点看。
"""
import os
import re
import sys
import json
import time

APP = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, APP)

try:
    import figure_detect as _figmod
except Exception:      # 找不到模块（精简部署）时静默降级
    _figmod = None


# ---- 配图检测：把插图从页里切出并归到对应题 ----------------------

def detect_page_figures(page_img, work_dir, page_no, questions,
                        conf_thresh=0.30):
    """检测本页配图，裁出 PNG，并标记归属的题目下标。

    归属规则：图的 y 中心落在某题的 [y0, y1] 区间内 → 归该题。
    若不在任何题的 y 区间内（图在题干上方/跨题），归给 y 上最接近的
    且在其上方的那道题（图通常跟在题干下方，或和图号标注同区）。
    都不匹配时 q_index=None（不丢图，由人工在导入界面确认）。

    返回 [{'path','crop','page','x0','y0','x1','y1','conf','q_index'}, ...]
    """
    if _figmod is None:
        return []
    figs = _figmod.detect_figures(page_img, text_boxes=[],
                                  conf_thresh=conf_thresh)
    if not figs:
        return []
    fig_dir = os.path.join(work_dir, 'figures')
    os.makedirs(fig_dir, exist_ok=True)
    out = []
    for i, f in enumerate(figs, 1):
        p = os.path.join(fig_dir, 'p%03d_fig%d.png' % (page_no, i))
        try:
            _figmod.crop_figure(page_img, f, p, margin=6)
        except Exception:
            continue
        cy = (f['y0'] + f['y1']) / 2.0
        qi = _assign_figure_to_question(f, questions, cy)
        out.append({'path': p, 'page': page_no,
                    'x0': f['x0'], 'y0': f['y0'],
                    'x1': f['x1'], 'y1': f['y1'],
                    'conf': f.get('conf'), 'q_index': qi})
    return out


# ---- 版面增强结果的后续处理（2026-10-22）--------------------

_LATEX_MARK = re.compile(r'\u27e6LATEX:(.+?)\u27e7', re.S)


def _apply_latex_marks(text):
    """把题干里的 ⟦LATEX:xxx⟧ 标记换成 $xxx$（公式 LaTeX 化）"""
    if not text:
        return text
    return _LATEX_MARK.sub(lambda m: '$%s$' % m.group(1).strip(), text)


def _grid_to_text(grid):
    """二维表格 → 纯文本表（用 | 分隔，保留结构）"""
    if not grid:
        return ''
    rows = []
    for row in grid:
        cells = [(c or '').strip() for c in row]
        if not any(cells):
            continue
        rows.append(' | '.join(cells))
    if len(rows) < 2:
        return '\n'.join(rows)
    # 加一个分隔行，读起来像表格
    ncol = max(len(r.split(' | ')) for r in rows)
    sep = ' | '.join(['---'] * ncol)
    return '\n'.join([rows[0], sep] + rows[1:])


# 碎片候选：极短、纯符号/数字/字母，不含中文和句读
_FRAG_CHARS = r'0-9A-Za-z\u0370-\u03ff\u2200-\u22ff\u2212\-+/=^_{}()\[\]<>|,²³'
_FRAG_RE = re.compile(r'^[\s\u3000]*[' + _FRAG_CHARS + r']{1,10}[\s\u3000]*$')
_CN_ANY = re.compile(r'[\u4e00-\u9fa5，。；：！？、\u201c\u201d\u3001]')

# 独立成行的单位（m/s²、N/kg）：这是**合法内容**，绝不是碎片。
# ★ 2026-10-22 踩坑：把 m/s² 当碎片并进上一行，导致 Q7 整题摊平。
# 单位词表（长的在前，避免 m 抢走 mm）
_UNAMES = (r'(?:mol|rad|min|kg|Hz|Pa|cd|lx|Bq|Gy|Sv|kN|km|cm|mm|nm|ms'
            r'|Ω|℃|m|s|g|N|J|W|A|V|C|T|K|h|d)')
# 一个「单位块」= 单位词 [ / 单位词 ] [ ² | ³ ]
_UBLK = (r'(?:' + _UNAMES + r')'
         r'(?:/(?:' + _UNAMES + r')(?:\^[23]|[²³]|[23](?![0-9A-Za-z]))?)?'
         r'(?:\^[23]|[²³])?')
# 整行只有单位 → 这是合法内容，不是碎片
_UNIT_ONLY = re.compile(
    r'^[\s(（\[]*' + _UBLK + r'(?:[\s·.\*]' + _UBLK + r')*' +
    r'[\s)）\].,;，。：:;]*$')


def _is_frag(t):
    """碎片：竖排分式被拆出来的那一小片。

    必须同时满足：
      ① 长 ≤ 10 且只含符号/数字/字母（无汉字、无句读）
      ② **不是**一个完整单位（m/s² 是合法内容，不是碎片）
      ③ 不能是"孤零零的标号"（A. / D.）—— 标号属于行首，不往上并
    """
    s = (t or '').strip()
    if not s or len(s) > 10:
        return False
    if _CN_ANY.search(s):
        return False
    if _UNIT_ONLY.match(s):
        return False
    if re.match(r'^[A-Ha-h]\s*[.、．)）]$', s):   # 选项标号
        return False
    if re.match(r'^\d{1,2}\s*[.、．]$', s):        # 题号
        return False
    return bool(_FRAG_RE.match(s))



def merge_fraction_fragments(lines, gap_ratio=2.2):
    """把「竖排分式被拆成多行」的碎片合并回去（2026-10-22）。

    实测形态（真实试卷 Q5/Q7/Q10/Q17）：

        D.                 <- 选项行，本身没有内容
        n2-1               <- 分子
        n                  <- 分母上半
        2                  <- 分母下半
        m                  <- 单位

    旧行为把它们逐行拼成 `D. n2-1 n 2 m`，选项彻底碎掉。

    判定依据（两条同时成立）：
      ① 几何：碎片与上一行垂直紧邻（间距 <= 行高 2.2 倍）
      ② 文字：短短纯符号，且不含中文

    返回新行列表；碎片文本用空格串接进「宿主行」，坐标取并集。
    """
    if not lines or len(lines) < 2:
        return lines

    def line_h(ln):
        return max(1.0, float(ln[5]) - float(ln[3]))

    out = []
    i = 0
    n = len(lines)
    while i < n:
        text, conf, x0, y0, x1, y1 = lines[i]
        parts = [text]
        hx0, hy0, hx1, hy1 = float(x0), float(y0), float(x1), float(y1)
        prev_bottom = float(y1)
        prev_h = line_h(lines[i])
        j = i + 1
        while j < n:
            t2, c2, x20, y20, x21, y21 = lines[j]
            if not _is_frag(t2):
                break
            tol = max(prev_h, line_h(lines[j])) * gap_ratio
            if float(y20) - prev_bottom > tol:      # 离得太远：不是同一块
                break
            parts.append(t2)
            hx0 = min(hx0, float(x20))
            hy1 = max(hy1, float(y21))
            hx1 = max(hx1, float(x21))
            prev_bottom = float(y21)
            prev_h = line_h(lines[j])
            j += 1
        if len(parts) > 1:
            merged = ' '.join(p.strip() for p in parts if p and p.strip())
            out.append((merged, conf, int(hx0), int(hy0), int(hx1), int(hy1)))
            i = j
        else:
            out.append(lines[i])
            i += 1
    return out



# 已知物理单位（用来判断「这个 token 是单位，不是分子」）
_UNITS = {
    'm', 's', 'kg', 'g', 'n', 'j', 'w', 'a', 'v', 'hz', 'pa', 'c', 't', 'k',
    'mol', 'rad', 'f', 'h', 'wb', 'va', 'ev', 'km', 'cm', 'mm', 'nm', 'ms',
    'min', 'h', 'd',
}


def _to_plain_math(s, superscript_hint=False):
    """把 OCR 裸文本写成 LaTeX。

    下标：v0 -> v_{0}，F1 -> F_{1}，l1 -> l_{1}
    上标：m/s2 -> m/s^{2}，m2 -> m^{2}
    特例：n2 在纯式里是 n^2（题目里「第 ns」「n2-1」都是幂）
    """
    s = (s or '').strip()
    if not s:
        return s
    s = s.replace('²', '^{2}').replace('³', '^{3}')
    # 单位里的数字是上标：m/s2 -> m/s^{2}
    s = re.sub(r'(?<=/)([A-Za-z]+)([23])(?![0-9A-Za-z])', r'\1^{\2}', s)
    if superscript_hint:
        # 纯数学式里的 n2 / x2 —— 当幂处理
        s = re.sub(r'(?<![A-Za-z])([A-Za-z])([23])(?![0-9A-Za-z])', r'\1^{\2}', s)
    # 其余：单字母 + 单个数字 = 下标（v0 F1 l1 t2）
    s = re.sub(r'(?<![A-Za-z])([A-Za-z])([0-9])(?![0-9A-Za-z])', r'\1_{\2}', s)
    return s


def _is_unit_tok(t):
    return (t or '').lower().strip('.') in _UNITS


def _is_math_tok(t):
    if not t:
        return False
    if not re.match(r'^[0-9A-Za-z\u0370-\u03ff\u2200-\u22ff\u2212.\-+/=^_{}()\[\]<>|,]{1,14}$', t):
        return False
    return bool(re.search(r'[0-9]', t)) or bool(re.search(r'[=+\-*/^_<>]', t))


def _looks_denominator(t):
    """token 能不能单独当分母：纯数字/短式，不能是单位词"""
    if not t or t.endswith('.') or t.endswith('，'):
        return False
    if not re.match(r'^[0-9A-Za-z(){}\-+*/^_]{1,14}$', t):
        return False
    if not re.search(r'[0-9]', t):
        return False
    return not _is_unit_tok(t)


def rebuild_frag_text(text):
    """把合并后的碎片串重建成可读的分式（2026-10-22）。

    输入形如（merge_fraction_fragments 的产物）：
        'A. 1 6m/s²'       -> 'A. $\frac{1}{6}$ m/s²'
        'A. 1 6 m/s²'      -> 'A. $\frac{1}{6}$ m/s²'
        'C. 2 3 t'         -> 'C. $\frac{2}{3}$ t'

    安全第一：认不出就原样返回，绝不猜。
    """
    if not text:
        return text
    s = re.sub(r'\s+', ' ', text.strip())
    toks = s.split(' ')
    if len(toks) < 2:
        return text

    pre = ''
    if re.match(r'^[A-Ha-h][.、．)）]$', toks[0]):
        pre = toks[0]
        toks = toks[1:]
    if len(toks) < 2:
        return text

    num = toks[0]
    if not _is_math_tok(num) or num.endswith('.'):
        return text

    # ★ 分母候选有两种形态：
    #   ① 独立 token：'6'
    #   ② 紧贴单位：'6m/s²'（碎片合并时单位没被并进来，粘在了一起）
    raw_den = toks[1]
    den = raw_den
    tail = ''
    m = re.match(r'^([0-9A-Za-z(){}\-+*/^_]{1,12}?)([A-Za-z][A-Za-z0-9/²³^]{0,10})$',
                 raw_den)
    if m and re.search(r'[0-9]', m.group(1)) and _UNIT_ONLY.match(m.group(2)):
        den, tail = m.group(1), m.group(2)
    elif not _looks_denominator(raw_den):
        return text

    if not _looks_denominator(den):
        return text

    rest = ' '.join(toks[2:]).strip()
    if tail:
        rest = (tail + (' ' + rest if rest else '')).strip()

    frac = '$\\frac{%s}{%s}$' % (_to_plain_math(num, False),
                                 _to_plain_math(den, False))
    out = (pre + ' ' if pre else '') + frac + (' ' + rest if rest else '')
    return out.strip()



def _assign_figure_to_question(fig, questions, cy):
    """把图归到最匹配的题目下标。"""
    if not questions:
        return None
    best = None
    best_d = None
    for i, q in enumerate(questions):
        qy0 = q.get('y0')
        qy1 = q.get('y1')
        if qy0 is None or qy1 is None:
            continue
        if qy0 <= cy <= qy1:
            return i
        # 图在题下方：离题底越近越可能属于它
        d = abs(cy - qy1) if cy > qy1 else abs(qy0 - cy)
        if best_d is None or d < best_d:
            best_d = d
            best = i
    return best


# ---- 阅读顺序重组 -------------------------------------------------

def group_lines(texts, boxes):
    """
    texts: list[str]
    boxes: list[[x0,y0,x1,y1]]
    返回按阅读顺序排好的 [(text, x0, y0, x1, y1), ...]
    策略：先按 y 聚类成「行带」，行带内按 x 排序；
          再检测是否双栏（左右两簇），若是则先左栏后右栏。
    """
    items = []
    for t, b in zip(texts, boxes):
        try:
            x0, y0, x1, y1 = [float(v) for v in b[:4]]
        except Exception:
            continue
        if not t or not t.strip():
            continue
        items.append({'text': t.strip(), 'x0': x0, 'y0': y0, 'x1': x1, 'y1': y1,
                      'cy': (y0 + y1) / 2, 'h': max(1.0, y1 - y0)})
    if not items:
        return []

    W = max(i['x1'] for i in items)
    H = max(i['y1'] for i in items)

    # --- 检测双栏：统计「中线附近的竖直空白」---
    mid = W / 2
    left_n = sum(1 for i in items if i['x1'] < mid * 1.15)
    right_n = sum(1 for i in items if i['x0'] > mid * 0.85)
    straddle = sum(1 for i in items if i['x0'] < mid * 0.85 and i['x1'] > mid * 1.15)
    # 两侧都有足够多的行，且跨栏行很少 → 判定双栏
    two_col = (left_n >= 4 and right_n >= 4 and straddle <= max(2, len(items) * 0.06))

    def sort_band(band):
        return sorted(band, key=lambda i: i['x0'])

    def cluster_rows(sub):
        """把若干 item 按 y 聚成行带"""
        if not sub:
            return []
        s = sorted(sub, key=lambda i: i['cy'])
        bands, cur = [], [s[0]]
        for it in s[1:]:
            ref = cur[-1]
            tol = max(ref['h'], it['h']) * 0.6
            if abs(it['cy'] - ref['cy']) <= tol:
                cur.append(it)
            else:
                bands.append(cur)
                cur = [it]
        bands.append(cur)
        return bands

    out = []
    if two_col:
        lefts = [i for i in items if i['x0'] < mid]
        rights = [i for i in items if i['x0'] >= mid]
        for col in (lefts, rights):
            for band in cluster_rows(col):
                out.extend(sort_band(band))
    else:
        for band in cluster_rows(items):
            out.extend(sort_band(band))
    return out


# ---- OCR 核心 -----------------------------------------------------

_OCR = None


def _gpu_available():
    """探测当前环境能否真的用上 GPU。

    注意：CPU 版 paddle 的 is_compiled_with_cuda() 返回 False，
    所以这个过程本身是安全的 —— 没装 GPU 版就会退回 CPU，不会报错。
    """
    try:
        import paddle
        if not paddle.device.is_compiled_with_cuda():
            return False, 'CPU 版 paddle（未编译 CUDA）'
        n = paddle.device.cuda.device_count()
        if n < 1:
            return False, '无可见 CUDA 设备'
        name = paddle.device.cuda.get_device_name(0)
        return True, name
    except Exception as e:
        return False, f'探测失败: {e}'


def get_ocr(use_gpu=None):
    """
    获取（并缓存）PaddleOCR 实例。

    use_gpu:
        None（默认）= 自动探测：能用 GPU 就用，否则退回 CPU
        True        = 强制 GPU（不可用时报错，不静默降级）
        False       = 强制 CPU

    ⚠️ 不要静默降级：如果用户以为在用 GPU 而实际跑 CPU，
    254s/页 的等待会让人误以为是卡死。所以强制 GPU 时宁可报错。
    """
    global _OCR
    if _OCR is not None:
        return _OCR

    # ★ 离线模型目录：引擎包旁若带了 official_models 就用它，不去联网下载
    try:
        import figure_detect
        figure_detect._ensure_model_dir()
    except Exception:
        pass

    ok, info = _gpu_available()
    if use_gpu is None:
        use_gpu = ok
    elif use_gpu and not ok:
        raise RuntimeError(f'要求使用 GPU，但不可用：{info}')

    from paddleocr import PaddleOCR
    kw = dict(
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=False,
        lang='ch',
    )
    if use_gpu:
        # PaddleOCR 3.x 用 device 参数；显式指定 GPU。
        kw['device'] = 'gpu:0'
        # GPU 版没有 oneDNN bug，但保留 mkldnn=False 会让 GPU 推理走窄路，
        # 所以 GPU 下不关 mkldnn。
    else:
        kw['enable_mkldnn'] = False   # 绕开 PaddlePaddle 3.3.1 oneDNN bug
    _OCR = PaddleOCR(**kw)
    _OCR._oc_device_info = ('gpu' if use_gpu else 'cpu', info)
    return _OCR


def ocr_device_info():
    """给界面用：返回 (是否GPU, 描述)。"""
    ok, info = _gpu_available()
    return ok, (f'GPU · {info}' if ok else f'CPU · {info}')


def ocr_image(img_path, ocr=None):
    """对单张图跑 OCR，返回按阅读顺序排好的行 [(text, conf, x0,y0,x1,y1)]"""
    if ocr is None:
        ocr = get_ocr()
    result = ocr.predict(img_path)
    texts, scores, boxes = [], [], []
    for res in result:
        texts = list(res.get('rec_texts', []))
        scores = list(res.get('rec_scores', []))
        bb = res.get('rec_boxes', None)
        if bb is not None:
            boxes = [[float(v) for v in row[:4]] for row in bb]
        else:
            polys = res.get('rec_polys', None) or res.get('dt_polys', None)
            if polys is not None:
                for poly in polys:
                    xs = [float(p[0]) for p in poly]
                    ys = [float(p[1]) for p in poly]
                    boxes.append([min(xs), min(ys), max(xs), max(ys)])
        break

    if not boxes or len(boxes) != len(texts):
        # 没有坐标就不排序，退化为原顺序
        return [(t, s, 0, 0, 0, 0) for t, s in zip(texts, scores)]

    ordered = group_lines(texts, boxes)
    conf_map = {}
    for t, s in zip(texts, scores):
        conf_map.setdefault(t.strip(), float(s))
    return [(o['text'], conf_map.get(o['text'], 0.0),
             o['x0'], o['y0'], o['x1'], o['y1']) for o in ordered]


# ---- PDF 处理 -----------------------------------------------------

def _require_pdf_deps():
    """PDF 路径依赖自检。

    ★ PyMuPDF 必须装在这个 OCR 环境里。主 venv 有、venv-ocr 没有是历史坑：
      图片 OCR 不需要 fitz，只有 PDF 才用到 —— 所以缺了平时看不出来，
      一导 PDF 就 ModuleNotFoundError。这里提前给出安装指引。
    """
    missing = []
    try:
        import fitz  # noqa: F401
    except Exception:
        missing.append('PyMuPDF')
    if missing:
        raise RuntimeError(
            '这个 OCR 环境缺少 PDF 支持：%s\n'
            '在当前 OCR 解释器里执行：\n'
            '    python -m pip install "PyMuPDF==1.28.2"\n'
            '（当前解释器：%s）' % ('、'.join(missing), sys.executable))


def pdf_to_images(pdf_path, out_dir, dpi=200, max_pages=None):
    """PDF → 页面 PNG 列表。扫描型和文字型都走这条路（统一）。"""
    import fitz  # PyMuPDF，装在主 venv；此函数若在 venv-ocr 调用会缺
    os.makedirs(out_dir, exist_ok=True)
    doc = fitz.open(pdf_path)
    paths = []
    n = doc.page_count if max_pages is None else min(max_pages, doc.page_count)
    for i in range(n):
        page = doc[i]
        pix = page.get_pixmap(dpi=dpi)
        p = os.path.join(out_dir, f'page{i+1:03d}.png')
        pix.save(p)
        paths.append(p)
    doc.close()
    return paths


def render_pages(source_path, out_dir, dpi=150):
    """给预览用：把源文件渲染成页面 PNG 列表并返回 [(页码, 路径), ...]。

    ★ 预览只要「快」——用低 dpi，不跑 OCR。PDF 用渲染路径（扫描/文字型通吃）。
      图片文件就是它自己。
    """
    ext = os.path.splitext(source_path)[1].lower()
    if ext == '.pdf':
        _require_pdf_deps()
        paths = pdf_to_images(source_path, out_dir, dpi=dpi)
        return list(enumerate(paths, 1))
    return [(1, source_path)]


def pdf_extract_page_images(pdf_path, out_dir):
    """从扫描型 PDF 里直接抽原图（比重新渲染清晰）"""
    import fitz
    os.makedirs(out_dir, exist_ok=True)
    doc = fitz.open(pdf_path)
    out = []
    for i in range(doc.page_count):
        page = doc[i]
        imgs = page.get_images(full=True)
        for j, im in enumerate(imgs):
            xref = im[0]
            try:
                pix = fitz.Pixmap(doc, xref)
                if pix.n - pix.alpha >= 4:
                    pix = fitz.Pixmap(fitz.csRGB, pix)
                p = os.path.join(out_dir, f'p{i+1:03d}_img{j+1}.png')
                pix.save(p)
                out.append((i + 1, p))
            except Exception:
                pass
    doc.close()
    return out


# ---- 题目切分 -----------------------------------------------------

# ★ 统一「题目开始点」规则（用户 2026-10-07 定口径）：
#   一组可勾选的「识别依据」，默认认：
#     ① 数字起头          「12. 」「12、」「13. (6分)」
#     ② 「例」+数字        「例题3. 」「例3、」
#     ③ 「题」+数字        「题5、」「题目7.」
#   每份文档可独立传 qnum_cfg 覆盖（界面上的「识别依据」多选框）。
#
#   🔴 绝杀误切：点号后紧跟数字 = 小数，永不当题号。
#      这就是「3.62cm → 题 3，内容 62cm」的根因。
#      用负向先行断言 (?!\d) 在点号后拦掉。
#      ——「不额外排除小数」会造成大面积误切，故仍强制拦截。
#   🔴 绝杀误切：点号后紧跟数字 = 小数，永不当题号。
#      这就是「3.62cm → 题 3，内容 62cm」的根因。
#      ★ 但只拦【1~2 位】数字（小数就是这种），4 位以上放行，
#        否则「4.2018年8月26日…」这种题号紧贴年份的真卷题会被误杀。
#        用负向先行断言 (?!\d{1,2}(?!\d)) 拦小数。
RE_QNUM_PREFIX = None   # 已废弃（前身写法），保留名字防外部引用报错

DEFAULT_QNUM_CFG = {
    'plain': True,   # ① 数字起头
    'li': True,      # ② 「例」+数字
    'ti': True,      # ③ 「题」+数字
    'sep': '.、．',   # 允许的分隔符集合
}


def build_qnum_re(cfg=None):
    """按配置出一个题号正则。不传 cfg 用默认（三种都认）。"""
    c = dict(DEFAULT_QNUM_CFG)
    if cfg:
        c.update(cfg)
    seps = ''.join(re.escape(ch) for ch in (c.get('sep') or DEFAULT_QNUM_CFG['sep']))
    pre_parts = []
    if c.get('li'):
        pre_parts.append(r'例\s*题?')
    if c.get('ti'):
        pre_parts.append(r'题\s*目?')
    prefix = ''
    if pre_parts:
        prefix = '(?:%s)' % '|'.join(pre_parts)
    # prefix 是否必需：只有当「数字起头」被关掉、而 例/题 开着时才强制。
    #   全开时 prefix 可选（12. / 例题3. 都认）。
    prefix_req = prefix and not c.get('plain')
    # 小数拦截：点号后 1~2 位数字 = 小数（3.62 / 1.5 / 0.5），不当题号；
    #   但 4 位以上（年份 4.2018）放行 —— 那是题号紧贴年份。
    NOT_DEC = r'(?!\d{1,2}(?!\d))'
    if prefix:
        pre = (prefix if prefix_req else '(?:%s)?' % prefix)
        return re.compile(
            r'^\s*%s\s*(\d{1,2})\s*[%s]%s\s*(\S.*)$' % (pre, seps, NOT_DEC))
    if c.get('plain'):
        return re.compile(
            r'^\s*(\d{1,2})\s*[%s]%s\s*(\S.*)$' % (seps, NOT_DEC))
    # 都不认 → 永不匹配
    return re.compile(r'(?!)')


# 向后兼容：默认的全量正则（三种都认）
RE_QNUM = build_qnum_re()
RE_OPT = re.compile(r'^\s*([ABCD])\s*[.、．)）]\s*(.*)$')
# 纯数字/坐标轴碎片：不应当成题干
RE_JUNK = re.compile(
    r'^[\d\s.,、，%/√()（）\-—+*×÷=<>~^\[\]【】.．:：;；\'"“”‘’|\\]+$')
# 页眉页脚："第N页共M页" 之类
RE_FOOTER = re.compile(r'第\s*\d+\s*页\s*共\s*\d+\s*页|共\s*\d+\s*页')
# 页眉/页脚通用形态：『高一物理试题』『XX中学物理试卷』『第2页』
# ★ 2026-10-22 实测：这句被拼进 Q3 的选项 D，变成
#   『D. 在t1时刻甲乙两车瞬时速度相等高一物理试题』
RE_HEADER = re.compile(
    r'^\s*(?:[一-龥]{0,12}(?:中学|一中|二中|三中|高中|初中|学校|学院))?\s*'
    r'[一-龥]{0,8}(?:物理|化学|数学|语文|英语|生物)(?:试题|试卷|卷|答案)'
    r'\s*\d{0,2}\s*$'
    r'|^\s*(?:第\s*\d+\s*页(?:\s*共\s*\d+\s*页)?|共\s*\d+\s*页|Page\s*\d+)\s*$')
# ==================== 答案 / 解析 分区 ====================
# ★ 2026-10-23 用户实测：Word 卷导入后「题后的答案和解析被判为题干内容」。
#   根因是答案行「【答案】B」不是选项行也不是题号行，落到续行分支，
#   被 `cur['options'][-1]['content'] += t` 拼到选项 D 后面。
#   所以切题阶段就必须认出答案行并丢掉，而不是等切完再回头找。

# 答案/解析行的「标记词」：中英文、全角半角、带不带括号都要认
_ANS_MARKS = (r'答案|解答|解析|分析|点评|详解|详析|考点|考查|考纲|命题|'
              r'评析|思路|点拨|点睛|小结|知识拓展|易错点|方法指导|'
              r'answer|Answer|ANSWER|solution|Solution|SOLUTION')
# 形式一：带括号/方括号的标记 —— 【答案】【解析】(答案) [解析] 〔答案〕
RE_ANS_BRACKET = re.compile(
    r'^\s*[【\[〔(（]\s*(?:' + _ANS_MARKS + r')\s*[】\]〕)）]')
# 形式二：裸标记 + 分隔符 —— 「答案：B」「解析：」「【分析】」已由上一行覆盖
RE_ANS_PLAIN = re.compile(
    r'^\s*(?:' + _ANS_MARKS + r')\s*[：:．.]')
# 形式三：选项字母后直接跟解析 —— 「D. 由 v=v0-at 得…，答案是 B」（题 3）
#   这种行本身是选项形态，但内容是解析。靠行内含「答案是/故选/故选：」
#   这类收尾语判定。
RE_ANS_TAIL = re.compile(r'(?:故选|应选|故答案为|答案是|故答案|正确答案是)\s*[:：]?\s*[A-H]')
# 分隔线 / 分节标题：「参考答案与解析」「答案与解析」「参考答案」
RE_ANS_HEAD = re.compile(
    # 「参考答案与解析」「答案与解析」—— 整行
    r'^\s*[【\[〔(（]?\s*(?:参考答案?|答案)\s*(?:与|和|及)\s*'
    r'(?:解析|解答|详解|评分标准|试题解析)\s*[】\]〕)）]?\s*$'
    r'|^\s*(?:参考答案?|答案)\s*(?:与|和|及)\s*(?:解析|解答|详解)\s*$'
    # 行尾/行中出现：「高三物理十月月考卷（A卷） — 参考答案」
    #   真实 Word 卷把答案区标题直接接在第 8 题题干行尾。
    r'|(?:^|[\s—–\-－:：])参考答案\s*$'
    r'|(?:^|[\s—–\-－:：])参考答案\s*(?:与|和|及)\s*(?:解析|解答|详解)')
# 末尾答案表：「1.B 2.A 3.C」/「1-5 BADCB」/「1—5：BADCB」
RE_ANS_TABLE = re.compile(
    r'^\s*\d{1,2}\s*[-~—－至]\s*\d{1,2}\s*[：:.]?\s*[A-H]{2,}')
RE_ANS_TABLE2 = re.compile(
    r'^\s*(?:\d{1,2}\s*[.、．:：]\s*[A-H]\s*){3,}$')


def _apply_answer_block(questions, pending):
    """把「参考答案与解析」区的块分回前面的题。

    pending: [{'num': int|None, 'text': str}, ...]，按原文顺序。
    规则：
      1. 带题号 → 找 number 相同的题
      2. 不带题号 → 接着上一个带回题号的那题（解析折行）
      3. 都接不上 → 按顺序给第 i 道题（只在这批题数和块数不匹配时兜底）
    绝不生成新题。
    """
    if not questions or not pending:
        return
    by_num = {}
    for q in questions:
        try:
            by_num.setdefault(int(str(q.get('number', '')).strip()), q)
        except (TypeError, ValueError):
            continue

    cur_q = None
    leftovers = []
    for item in pending:
        num = item.get('num')
        text = (item.get('text') or '').strip()
        if not text:
            continue
        if num is not None:
            cur_q = by_num.get(num)
            if cur_q is None:
                # 题号对不上：可能是答案区自己的编号（如答案解析从 1 重排）
                leftovers.append(text)
                cur_q = None
                continue
        if cur_q is None:
            leftovers.append(text)
            continue
        _push_ans(cur_q, text)

    # 兜底：答案区没标题号、或被整段重排过 → 按顺序均分
    if leftovers:
        hit = [q for q in questions if q.get('answer') or q.get('analysis')]
        if not hit:
            for q, text in zip(questions, leftovers):
                _push_ans(q, text)


def _strip_ans_tail(t):
    """选项正文里如果混进了「，答案是 B」这类收尾语，切掉。"""
    m = RE_ANS_TAIL.search(t or '')
    if not m:
        return t
    return (t[:m.start()]).rstrip(' ，,。;；')


def _push_ans(q, text):
    """把答案 / 解析片段记进题目。

    优先级：
      1. 字母答案（【答案】B / 答案：B / 故选 B）→ q['answer'] = 'B'
      2. 非字母答案（填空/计算题，「答案：$R=12Omega$」）→ 整段进 answer
         但只取到下一个标记词为止，否则会把后面的「解析：…」一起吞掉
      3. 其余全部并入 q['analysis']
    """
    text = (text or '').strip()
    if not text:
        return
    q.setdefault('answer', '')
    q.setdefault('analysis', '')

    def add_ana(s):
        s = (s or '').strip()
        if not s:
            return
        q['analysis'] = (q['analysis'] + ' ' + s).strip() \
            if q['analysis'] else s

    # ---- 1. 字母答案 ----
    if not q['answer']:
        m = RE_ANS_LETTER.search(text)
        if m:
            q['answer'] = re.sub(r'[\s,，、/]', '', m.group(1))
        else:
            # 「故选 B」「答案是 A」—— RE_ANS_TAIL 无捕获组，单独取
            m2t = re.search(r'(?:故选|应选|故答案为|答案是|故答案|正确答案是)'
                            r'\s*[：:]?\s*([A-H])', text)
            if m2t:
                q['answer'] = m2t.group(1)

    # ---- 2. 非字母答案 ----
    if not q['answer']:
        m2 = re.match(r'^\s*[【\[〔]?\s*(?:答案|正确答案)\s*[】\]〕]?\s*'
                      r'[：:]\s*(.+)$', text)
        if m2:
            raw = m2.group(1).strip()
            cut = _RE_ANS_MARK_ANY.search(raw)
            val = raw[:cut.start()].strip() if cut else raw
            if val and not re.match(r'^[A-H]\b', val):
                q['answer'] = val
                if cut:
                    add_ana(raw[cut.start():].strip())
                return

    # ---- 3. 余下算解析 ----
    body = RE_ANS_LETTER.sub(' ', text)
    body = re.sub(r'^\s*[【\[〔]\s*(?:解析|分析|详解|点评|考点|解答|'
                  r'思路|点拨|评析|小结)\s*[】\]〕]\s*', ' ', body)
    body = re.sub(r'^\s*(?:解析|分析|详解|点评|解答)\s*[：:]\s*', ' ', body)
    add_ana(body)



def is_answer_line(t):
    """这一行是不是答案/解析内容（不该拼进题干或选项）"""
    if not t:
        return False
    if RE_ANS_BRACKET.match(t):
        return True
    if RE_ANS_PLAIN.match(t):
        return True
    if RE_ANS_HEAD.match(t):
        return True
    if RE_ANS_TABLE.match(t) or RE_ANS_TABLE2.match(t):
        return True
    return False


# 行内 search 版（去掉 ^ 锚点）：用于从「D. 20 m/s【答案】B…」里
# 找到答案起点，把选项正文和答案尾巴切开。
_RE_ANS_BRACKET_ANY = re.compile(
    r'[【\[〔(（]\s*(?:' + _ANS_MARKS + r')\s*[】\]〕)）]')
# 抓「【答案】B」「答案：B」「正确答案是 AC」里的字母组
RE_ANS_LETTER = re.compile(
    r'[【\[〔]?\s*(?:答案|正确答案)\s*[】\]〕]?\s*[：:]?\s*'
    r'([A-H](?:\s*[,，、/]?\s*[A-H])*)')
_RE_ANS_MARK_ANY = re.compile(
    r'(?:^|\s|[【〔(（])(?:' + _ANS_MARKS + r')\s*[：:】〕)）]')
_RE_ANS_PLAIN_ANY = re.compile(
    r'(?:^|\s)(?:' + _ANS_MARKS + r')\s*[：:]')



def _has_ans_tail(t):
    """行内含「故选 B」「答案是 A」这类收尾语 —— 说明这是解析不是选项"""
    return bool(RE_ANS_TAIL.search(t))


def _split_tail_answer(t):
    """把「D. 20 m/s【答案】B【解析】…」拆成 (选项正文, 答案块)。

    返回 (keep, ans)。keep 是要留在选项里的部分，ans 是从答案起点开始的尾巴。
    找不到答案起点就返回 (t, '')。
    """
    m = None
    for rx in (_RE_ANS_BRACKET_ANY, _RE_ANS_PLAIN_ANY):
        mm = rx.search(t)
        if mm and (m is None or mm.start() < m.start()):
            m = mm
    if not m:
        return t, ''
    return t[:m.start()].strip(), t[m.start():].strip()


# 题干尾巴上的分节标题（「多选题（共 2 小题，共 8 分）」）要切掉
RE_STEM_TAIL = re.compile(
    r'\s*(?:单项|多项|不定项|多选|单选|选择|选)?\s*题\s*'
    r'[（(]\s*共\s*\d+\s*小?题[，,]?\s*共?\s*\d+\s*分\s*[）)]'
    r'|\s*(?:单项|多项|不定项|多选|单选|选择|选)?\s*题\s*'
    r'[（(]\s*共\s*\d+\s*分\s*[）)]'
    r'|\s*(?:填空|实验|计算|解答|判断|简答)题\s*'
    r'[（(]\s*共\s*\d+\s*小?题[，,]?\s*共?\s*\d+\s*分\s*[）)]\s*$')

RE_SECTION = re.compile(
    r'^\s*(?:第\s*[一二三四五六七八九十]+\s*[卷部分]|'
    r'[一二三四五六七八九十]+\s*[、.]\s*|'
    r'(?:单项|多项|不定项)?选择?题|填空题|实验题|计算题|解答题|'
    r'作图题|判断题|简答题)')

# ★ 卷首须知区：编号 1/2/3 的「注意事项」极容易被当成题 1/2/3，
#   把题号计数顶高，导致真卷从 1 重新开始时全被判为「不递增」而丢弃。
RE_NOTICE = re.compile(
    r'^\s*(?:\d{1,2}\s*[.、．]\s*)?'
    r'(?:答卷前|考生须知|注意事项|本试卷[分共]|全卷[共分]|'
    r'答题前|请将答案|请用\s*\d|须用\s*\d|考试时间|满分\s*\d|'
    r'第\s*[IJⅠⅡ一二]\s*卷|铅笔|签字笔|答题卡|中性笔|橡皮|涂黑|'
    r'准考证|姓名|考场|填涂|作答|答题区域|卷面|'
    r'回答选择题|选出每小?题答案|考试结束后|只需要?上交|'
    r'收回答题卡|上交答题卡|用橡皮擦|答案标号)')
# 卷首须知区的总开关区域：从卷首开始，直到第一道真题为止的
# 「回答选择题时…」「考试结束后…」这类无编号说明句，用下面这条识别。
RE_NOTICE_PROSE = re.compile(
    r'^(?:回答选择题|选出每小?题|考试结束后|只需要?上交|交回答题卡|'
    r'选涂其他|用橡皮擦|不准使用|不得使用|答题时)')
# 题号计数上限：超过这个数说明极可能已进入下一题（防跑飞）
_QNUM_CAP = 60


def _is_junk(t):
    """判断是不是坐标轴碎片/页脚这类噪声，不该算题干"""
    if not t or len(t) < 2:
        return True
    if RE_FOOTER.search(t):
        return True
    if RE_HEADER.match(t):
        return True
    # 纯数字/符号行
    if RE_JUNK.match(t):
        return True
    # 含有中文的回答句 —— 不管数字多少，都是正文，不是坐标轴碎片。
    # ★ 修正：像「2018年8月26日，在雅加达…」这种日期开头的题，
    #   数字占比也会过 45%，旧逻辑会把它当碎片丢掉（本题卷题 4）。
    if sum(1 for c in t if '\u4e00' <= c <= '\u9fff') >= 5:
        return False
    # 主要都是数字（如 "64.2 4.8 5.4 6 6.66.8 8.4"）
    digits = sum(c.isdigit() for c in t)
    if digits >= len(t) * 0.45:
        return True
    return False


def split_by_boundaries(ordered_lines, page_no, boundaries, state):
    """★ 人工分界切分（用户 2026-10-07 新增需求）。

    boundaries: {页码: [y比例, ...]}，每个 y 是「一道题开始」的横线位置
                （0~1，相对页高，从上到下）。空列表=本页不分。
    语义：线以上的行归上一题（续行）；从线开始的行开新题。

    与自动切分不同：这里**完全不看题号**，用户点的就是权威分界。
    返回题目列表；跨页续承由 state 携带（cur 不带出，每页独立切，
    但上一页未完成的题会在本页首次遇到分界线时收尾）。
    """
    questions = []
    cur = None

    def flush():
        nonlocal cur
        if cur and (cur['stem'].strip() or cur['options']):
            questions.append(cur)
        cur = None

    cuts = sorted(boundaries.get(page_no, []) or [])
    # 没有就给本页开头切一刀（整页算一道）——实际界面会保证至少一个点
    if not cuts:
        cuts = [0.0]

    # 页高：取所有行 y1 的最大值（与 group_lines 同口径）
    page_h = max([max(y0, y1) for _, _, _, y0, _, y1 in ordered_lines
                  if y0 or y1] or [0.0])

    for text, conf, x0, y0, x1, y1 in ordered_lines:
        t = text.strip()
        if not t:
            continue
        if RE_FOOTER.search(t):
            continue
        # 行中心相对页高的比例（0~1，从上到下）
        if page_h > 0:
            yr = ((y0 + y1) / 2.0) / page_h
        else:
            yr = 0.5
        # 找出本行前面有几个分界点落在它上方
        n_before = sum(1 for c in cuts if c <= yr + 1e-6)
        # 需要开新题：本行跨过了新的分界线
        if n_before > (cur.get('_cut_idx', 0) if cur else 0) or cur is None:
            flush()
            cut_idx = n_before
            cur = {'number': '', 'stem': '', 'options': [], 'raw': [],
                   'low_conf': [], 'section': '', 'manual': True,
                   '_cut_idx': cut_idx, '_page': page_no,
                   'number': str(len(questions) + 1),
                   'page': page_no}
        m_o = RE_OPT.match(t)
        if m_o and cur is not None and not cur['stem']:
            # 题干还没开始时是选项 → 但更可能是题干里嵌的，先当题干
            pass
        if m_o and cur is not None and cur['stem']:
            cur['options'].append({'label': m_o.group(1).upper(),
                                   'content': m_o.group(2).strip(),
                                   'conf': conf})
            if conf < 0.90:
                cur['low_conf'].append(t)
            cur['raw'].append(t)
            continue
        if _is_junk(t):
            cur['raw'].append(t)
            if conf < 0.90:
                cur['low_conf'].append(t)
            continue
        if cur['options']:
            cur['options'][-1]['content'] += t
        else:
            cur['stem'] = (cur['stem'] + ' ' + t).strip()
        cur['raw'].append(t)
        if conf < 0.90:
            cur['low_conf'].append(t)

    flush()
    return questions


def _track_y(cur, y0, y1):
    """维护一道题在页面上的 y 范围（用于把配图归到对应题）。

    图属于「其 y 区间与本题 y 区间重叠、且图在题下方或内部」的那道题。
    """
    y0 = float(y0 or 0)
    y1 = float(y1 or 0)
    if y1 <= 0 and y0 <= 0:
        return
    if 'y0' not in cur or y0 < cur['y0']:
        cur['y0'] = y0
    if 'y1' not in cur or y1 > cur['y1']:
        cur['y1'] = y1


def split_questions(ordered_lines, state=None, qnum_cfg=None,
                    drop_answers=False):
    """
    把 OCR 行切成题目草稿。
    返回 [{'number','stem','options':[{label,content}],'raw':[...],'low_conf':[...]}]

    ★ state：跨页续承（本题卷核心 bug）。
      run_ocr 是【逐页】调用本函数的，如果每题号都从 0 重新起算，
      第 3 页的「10.」就会被当成「跳得太大」而整页丢光。
      传入一个 dict 即可把 section / last_num / pending 带到下一页。
      不传时行为与旧版一致（单页独立）。

    ★ qnum_cfg：本次「识别依据」（见 build_qnum_re）。
      传 None 用默认（数字/例/题 都认）。

    ★ drop_answers：丢弃答案/解析行（【答案】B / 解析：… / 故选 B）。
      旧行为是把它们当续行，拼到上一个选项后面 —— Word 卷实测
      「D. 20 m/s【答案】B【解析】…」整块进选项 D，见 _split_tail_answer。
      为 False 时保留旧行为（OCR 扫的答题卡有时答案就在卷面上，
      拼进去反而有用），为 True 时丢掉并记到 q['answer'] / q['analysis']。
    """
    re_qnum = build_qnum_re(qnum_cfg)
    questions = []
    cur = None
    st = state if state is not None else {}
    section = st.get('section', '')
    # 题号必须大体递增，避免页码/年份/图轴数字被当题号。
    # ★ 但遇到「回落」（如卷首注意事项 1/2/3 之后真卷又从 1 开始）
    #   要重新起算，否则真卷的前几道题会被整片丢掉。
    last_num = st.get('last_num', 0)
    pending_junk = st.get('pending_junk', [])
    _pending_ans_flip = False
    in_notice = st.get('in_notice', False)
    # 本页是否已认过题号；用于判断「跳得太大」该不该体谅
    page_seen = 0
    # ★ 答案区模式：遇到「参考答案与解析」后，后面全是答案解析，
    #   不能再切题（否则生成一堆 STEM='答案：C' 的伪题）。
    #   ans_seq 记录答案区里按顺序出现的题号，用于回填。
    answer_mode = st.get('answer_mode', False)
    ans_pending = []      # 暂存答案区内待归位的结果 [{'num','text'}]
    ans_seq = []          # 答案区里出现过的原始题号（保持顺序）

    def flush():
        nonlocal cur
        if cur and (cur['stem'].strip() or cur['options']):
            questions.append(cur)
        cur = None

    for text, conf, x0, y0, x1, y1 in ordered_lines:
        t = text.strip()
        if not t:
            continue
        if _pending_ans_flip:
            # 上一行的尾巴是「— 参考答案」，本行起进入答案区
            flush()
            answer_mode = True
            _pending_ans_flip = False


        # ---- 答案区模式：一行都不切题，只按题号分块收集 ----
        # ★ 必须放在最前：旧位置在 m_q 之后，导致「1. 答案：C」先被当成
        #   一道新题登记，答案区形同虚设（真实卷实测冒出 7 道伪题）。
        if answer_mode:
            if RE_FOOTER.search(t) or RE_HEADER.match(t):
                continue
            mt = re.match(r'^\s*(\d{1,2})\s*[.、．]\s*(.*)$', t)
            if mt:
                ans_pending.append({'num': int(mt.group(1)),
                                    'text': mt.group(2).strip()})
                continue
            # 不带题号的续行 → 并进上一块（解析折行）
            if ans_pending:
                ans_pending[-1]['text'] = (
                    ans_pending[-1]['text'] + ' ' + t).strip()
            continue

        # 整行就是答案区标题（「参考答案与解析」）→ 立刻进答案区。
        # ★ 必须在页眉规则之前：「XX卷 — 参考答案」长得像页眉，
        #   先被 RE_HEADER 吞掉的话答案区标志就丢了。
        if drop_answers and not answer_mode and RE_ANS_HEAD.search(t):
            # 只认「整行/行尾就是标题」的情况；题干中间出现的不算
            if re.search(r'(?:^|[\s—–\-－:：])参考答案\s*$', t) or \
                    RE_ANS_HEAD.fullmatch(t):
                flush()
                answer_mode = True
                section = t
                continue

        # 页眉页脚：直接丢弃，不污染任何题
        if RE_FOOTER.search(t) or RE_HEADER.match(t):
            continue

        # 答案区分隔标题：「参考答案」「参考答案与解析」「答案与解析」
        if False and drop_answers and RE_ANS_HEAD.search(t):
            # 标题接在题干行尾（真实卷常见）：
            #   「8.（10分）下列说法正确的是（　） 高三物理十月月考卷 — 参考答案」
            mt2 = re.search(r'(?:^|[\s—–\-－:：])参考答案\s*$', t)
            if mt2 and mt2.start() > 0:
                head = t[:mt2.start()].strip()
                if head:
                    t = head             # 前半段继续按普通行处理
                    _pending_ans_flip = True
            else:
                flush()
                answer_mode = True
                section = t
                continue

        if RE_SECTION.match(t) and not RE_OPT.match(t) and not re_qnum.match(t):
            section = t
            in_notice = False   # 进入正题/新分节，须知区结束
            continue

        m_q = re_qnum.match(t)
        m_o = RE_OPT.match(t)

        # ---- 卷首须知区：整段排除，绝不当题号 ----
        if RE_NOTICE.match(t) or RE_NOTICE_PROSE.match(t):
            # ★ 不要把里面的「1./2./3.」写进 pending_junk ——
            #   它们会被挂到第一道题上，看起来像几道废题。
            in_notice = True
            m_q = None
            m_o = None
            continue

        # 本节标题（如「二、选择题」），题号往往在新节从 1 重来 → 重置计数
        if RE_SECTION.match(t) and not RE_OPT.match(t) and not re_qnum.match(t):
            last_num = 0

        # ---- 题号判断：大局递增，允许回落重起 ----
        if m_q:
            num = int(m_q.group(1))
            body = m_q.group(2).strip()
            # 本页已认过的最大题号；用来判断是不是新的一页（页内续承）
            if in_notice:
                # 须知区里的数字不配当题号；但真卷紧跟须知后从 1 重新
                # 起算，必须放行，否则前几道题会连片消失（核心 bug）。
                if num <= 3 and len(body) >= 4:
                    last_num = 0
                    in_notice = False
                else:
                    m_q = None
            elif _is_junk(body):
                m_q = None
            elif num > _QNUM_CAP:
                m_q = None
            elif num == last_num + 1 or (
                    0 < num - last_num <= 5):
                # 正常递进
                in_notice = False
            elif num <= last_num:
                # ★ 回落：真卷题号从 1/某小节重新开始；旧逻辑在这里
                #   会连续吞掉若干道题。只在「落到 1 附近」时认。
                #   但必须限「内容确实像题干」，否则图轴残留
                #   「3. t/s」这类会被扶成一道伪题。
                if num <= 3 and len(body) >= 4:
                    last_num = 0
                    in_notice = False
                else:
                    m_q = None
            else:
                # num 比 last_num 大很多：可能是换页开头（last_num 还是
                # 上一页的尾号）或者是年份/分值噪声。
                # ★ 本页还没认过题号 → 当成新页开头放行。
                #   但仍要开页首行本身长得像题干（别把「t/s」这类
                #   图轴残留扶成一道题）。
                if page_seen == 0 and len(body) >= 4:
                    in_notice = False
                else:
                    m_q = None

        if m_q:
            page_seen += 1

        if m_o and cur is not None and not m_q:
            body = m_o.group(2).strip()
            tail = ''
            if drop_answers and cur['options']:
                L = m_o.group(1).upper()
                # (c-1) 「D. 由 v=v0-at 得…答案是 B」—— 字母和上一个选项
                #       相同、且带「故选/答案是」收尾语 → 这是解析，不是选项
                if L == cur['options'][-1]['label'] and _has_ans_tail(body):
                    cur['raw'].append(t)
                    _push_ans(cur, body)
                    cur['in_ans'] = True
                    continue
                # (c-2) 字母对不上但含收尾语 → 上一选项被截断，把它补回来
                if _has_ans_tail(body) and not _has_ans_tail(
                        cur['options'][-1]['content']):
                    # 只有当上一选项看起来不完整（没有数字/字母结尾）时才
                    # 认为这行是解析；否则可能真是下一个选项。
                    prev = cur['options'][-1]['content']
                    if len(prev) <= 2 or not re.search(r'[0-9A-Za-z]$', prev):
                        cur['raw'].append(t)
                        _push_ans(cur, body)
                        cur['in_ans'] = True
                        continue
            if drop_answers:
                # 「D. 20 m/s【答案】B【解析】…」→ 选项只留 "20 m/s"
                body, tail = _split_tail_answer(body)
            cur['options'].append({
                'label': m_o.group(1).upper(),
                'content': body,
                'conf': conf})
            if tail:
                _push_ans(cur, tail)
                cur['in_ans'] = True
            if conf < 0.90:
                cur['low_conf'].append(t)
            cur['raw'].append(t)
            _track_y(cur, y0, y1)
            continue

        if m_q:
            flush()
            last_num = int(m_q.group(1))
            in_notice = False
            stem0 = m_q.group(2).strip()
            inline_ans = ''
            if drop_answers:
                # 「下列单位中…的是（答案：A）」→ 题干去掉括号，答案进 answer
                mm = re.search(r'[（(]\s*(?:答案|正确答案)\s*[：:]?\s*'
                               r'([A-H](?:\s*[,，、/]?\s*[A-H])*)\s*[）)]',
                               stem0)
                if mm:
                    inline_ans = mm.group(0)
                    stem0 = (stem0[:mm.start()] +
                             stem0[mm.end():]).strip()
            stem0 = RE_STEM_TAIL.sub('', stem0).strip()
            cur = {'number': str(last_num), 'stem': stem0,
                   'options': [], 'raw': [t], 'low_conf': [],
                   'section': section,
                   'y0': float(y0), 'y1': float(y1)}
            if inline_ans:
                _push_ans(cur, inline_ans)
            if conf < 0.90:
                cur['low_conf'].append(t)
            continue



        # ---- 答案 / 解析行：丢掉，不当题干也不当选项 ----
        if drop_answers and cur is not None:
            # (a) 明确的答案/解析标记行
            if is_answer_line(t):
                cur['raw'].append(t)
                _push_ans(cur, t)
                # ★ 之后的无标记行仍属解析（题 2 的折行：
                #   「…例如传送带上的物体。」「滑动摩擦力大小只与…」
                #   独立成段，没有【解析】前缀，但显然不是选项 D 的正文）
                if cur['options']:
                    cur['in_ans'] = True
                continue
            # (b) 上一行是「解析：」，这一行是它的折行
            if cur.get('in_ans'):
                cur['raw'].append(t)
                _push_ans(cur, t)
                continue

        # ---- 续行 ----
        if cur is None:
            # 开头没题号的碎片，先囤着，等第一道真题出现时当题干前缀
            pending_junk.append(t)
            continue
        # 噪声行不拼进题干（坐标轴数字、纯符号）
        if _is_junk(t):
            cur['raw'].append(t)
            if conf < 0.90:
                cur['low_conf'].append(t)
            continue
        if cur['options']:
            cur['options'][-1]['content'] += t
        else:
            cur['stem'] = (cur['stem'] + ' ' + t).strip()
            # ★ 题干尾巴常混进下一个分节标题：
            #   「…$x=v_0t+\frac{1}{2}at^2$ 多选题（共 2 小题，共 8 分）」
            cur['stem'] = RE_STEM_TAIL.sub('', cur['stem']).strip()
        # ★ 行尾截断：最后一段续行处理完之后，若本行尾部是
        #   「… 高三物理十月月考卷（A卷） — 参考答案」，说明答案区
        #   从下一行开始。这里只是打标记，t 已经并进题干，不重切。
        if drop_answers and not answer_mode:
            mt2 = re.search(r'(?:^|[\s—–\-－:：])参考答案\s*$', t)
            if mt2 and mt2.start() > 0:
                _pending_ans_flip = True

        cur['raw'].append(t)
        _track_y(cur, y0, y1)
        if conf < 0.90:
            cur['low_conf'].append(t)

    flush()

    # ---- 答案回填：按题号 / 顺序把答案区内容分给前面的题 ----
    if ans_pending:
        _apply_answer_block(questions, ans_pending)

    # 开头囤的碎片（往往是标题）挂到第一道题上单独记录，便于人工看
    if pending_junk and questions:
        questions[0]['preamble'] = pending_junk
        pending_junk = []

    if state is not None:
        state['answer_mode'] = answer_mode
        state['section'] = section
        state['last_num'] = last_num
        state['pending_junk'] = pending_junk
        state['in_notice'] = in_notice
    return questions


def guess_type(q):
    """猜题型。能看出单/多选就细分，看不出时按选项数猜。"""
    sec = q.get('section', '')
    if '多选' in sec or '多项' in sec:
        return 'choice_multi'
    if '单选' in sec or '单项' in sec:
        return 'choice_single'
    if '选择' in sec:
        # 光写“选择题”，看选项数与答案字母数
        return _guess_choice_kind(q)
    if '填空' in sec:
        return 'fill'
    if '判断' in sec:
        return 'judgment'
    if '实验' in sec:
        return 'experiment'
    if '计算' in sec or '解答' in sec:
        return 'calculation'
    if len(q['options']) >= 2:
        return _guess_choice_kind(q)
    return 'calculation'


def _guess_choice_kind(q):
    """选择题细分：答案两个及以上字母 -> 多选，否则单选。"""
    import re as _re
    n = len(_re.findall(r'[A-Ha-h]', q.get('answer') or ''))
    return 'choice_multi' if n >= 2 else 'choice_single'


def guess_answer(q):
    """从选项里找被圈/被划的答案标记（OCR 很难拿到，多数为空）"""
    for o in q['options']:
        c = o['content']
        if '√' in c or '✓' in c or '【' in c and '】' in c:
            return o['label']
    return ''


# ---- 主流程 -------------------------------------------------------

def run_ocr(source_path, work_dir, out_json=None, dpi=200, max_pages=None,
            verbose=True, qnum_cfg=None, boundaries=None,
            no_figures=False, fig_conf=0.30,
            no_enhance=False, no_math=False, no_table=False, max_math=12):
    """
    对 PDF 或图片跑 OCR，产出草稿 JSON。
    返回草稿 dict。

    qnum_cfg: 「识别依据」配置（见 build_qnum_re），None 用默认。
    boundaries: 人工分界 {页码: [y比例, ...]}；给了就【不看题号】、
                完全按用户点的横线切（用户 2026-10-07 需求）。
    """
    t0 = time.time()
    os.makedirs(work_dir, exist_ok=True)
    ext = os.path.splitext(source_path)[1].lower()

    log = []

    def say(*a):
        msg = ' '.join(str(x) for x in a)
        log.append(msg)
        if verbose:
            print(msg, flush=True)

    if ext == '.pdf':
        say(f'[1/4] PDF 转图片: {os.path.basename(source_path)}')
        _require_pdf_deps()
        try:
            pages = pdf_extract_page_images(source_path,
                                            os.path.join(work_dir, 'pages'))
            if not pages:
                raise RuntimeError('无内嵌图片')
            page_imgs = []
            seen = set()
            for pno, p in sorted(pages):
                if pno not in seen:
                    seen.add(pno)
                    page_imgs.append(p)
            say(f'      抽出内嵌图 {len(pages)} 张，首页图 {len(page_imgs)} 张')
        except Exception as e:
            say(f'      内嵌图抽取失败({e})，改用页面渲染')
            page_imgs = pdf_to_images(source_path,
                                      os.path.join(work_dir, 'pages'),
                                      dpi=dpi, max_pages=max_pages)
    else:
        page_imgs = [source_path]
        say(f'[1/4] 单张图片: {os.path.basename(source_path)}')

    ocr = get_ocr()
    _isgpu, _desc = ocr_device_info()
    say(f'[2/4] OCR 引擎就绪（{_desc}）')

    all_q = []
    page_meta = []
    carry = {}   # ★ 跨页续承：不传会把第 3 页起的题全丢掉
    for idx, img in enumerate(page_imgs, 1):
        say(f'[3/4] 识别 第 {idx}/{len(page_imgs)} 页 ...')
        tt = time.time()
        lines = ocr_image(img, ocr)
        dt = time.time() - tt
        say(f'      {len(lines)} 行，耗时 {dt:.1f}s')
        page_meta.append({'page': idx, 'image': img, 'lines': len(lines),
                          'seconds': round(dt, 1)})
        # ★ 版面增强（2026-10-22）：多栏 / 公式 / 表格
        #   必须在 split_questions 之前，因为：
        #   ① 公式 LaTeX 要写回 lines 才能进题干
        #   ② 多栏模式会改变行的阅读顺序
        enh = {}
        if not no_enhance:
            try:
                import layout_enhance as LE
                enh = LE.enhance_page(
                    img, lines, page_no=idx, verbose=False,
                    do_math=not no_math, do_table=not no_table,
                    do_columns=True, max_math=max_math)
                bits = []
                if enh.get('n_cols', 1) > 1:
                    bits.append(f"{enh['n_cols']} 栏")
                if enh.get('math'):
                    bits.append(f"公式 {len(enh['math'])}")
                if enh.get('tables'):
                    bits.append(f"表格 {len(enh['tables'])}")
                if bits:
                    say('      版面增强：' + '、'.join(bits))
            except Exception as e:
                say(f'      版面增强跳过：{e}')

        # 把公式 LaTeX 拼回对应行（用 ⟦LATEX:...⟧ 标记，切题后再解析）
        # ★ 三条纪律（2026-10-22 实测）：
        #   ① 纯式（整行就是公式）→ 用 LaTeX 替掉原文
        #   ② 混排（中文+公式）→ 只能【取并集】：把原文里没有的数字/符号
        #      交上去，绝不能整行替换，否则中文全丢
        #   ③ 模型失败/被闸门拦下 → 原封不动，不许丢行
        if enh.get('math'):
            new_lines = list(lines)
            for li, info in enh['math'].items():
                if not (0 <= li < len(new_lines)):
                    continue
                lx = info.get('latex')
                if not lx:
                    continue          # ①失败：保持原文
                t, c, x0, y0, x1, y1 = new_lines[li]
                if info.get('kind') == 'pure':
                    merged = '⟦LATEX:%s⟧' % lx   # ②纯式：整行替掉
                else:
                    merged = '%s ⟦LATEX:%s⟧' % (t, lx)  # ③混排：并集
                new_lines[li] = (merged, c, x0, y0, x1, y1)
            lines = new_lines

        # ★ 竖排分式碎片合并（2026-10-22）：必须在切题和公式标记之后。
        #   真实试卷里 1/6 这类分式会被 OCR 拆成 '1' '6' 两行，
        #   旧逻辑逐行拼成 'A. 1 6 m/s²'，选项全碎。
        try:
            lines = merge_fraction_fragments(lines)
        except Exception as e:
            say(f'      碎片合并跳过：{e}')

        # ★ 多栏：按 (栏号, y) 重排阅读顺序
        if enh.get('n_cols', 1) > 1 and enh.get('line_col'):
            col = enh['line_col']
            order = sorted(range(len(lines)),
                           key=lambda i: (col[i] if i < len(col) else 0,
                                          lines[i][3], lines[i][2]))
            lines = [lines[i] for i in order]

        qs = split_questions(lines, state=carry, qnum_cfg=qnum_cfg)
        if boundaries:
            qs = split_by_boundaries(lines, idx, boundaries, carry)
            say(f'      人工分界：本页切成 {len(qs)} 段')

        # ★ 配图检测（2026-10-12）：用版面分析模型找出本页的插图，
        #   裁出来并挂到 y 区间对应的那道题。图不会混进题干文字。
        figs = []
        if not no_figures:
            try:
                figs = detect_page_figures(img, work_dir, idx, qs,
                                           conf_thresh=fig_conf)
                if figs:
                    say(f'      配图：本页切出 {len(figs)} 张'
                        + (f'，归属 {len(set(f["q_index"] for f in figs))} 道题'
                           if any(f.get('q_index') is not None for f in figs)
                           else '（未匹配到题）'))
            except Exception as e:
                say(f'      配图检测跳过：{e}')
        for q in qs:
            q['page'] = idx
            q['source_image'] = img
            all_q.append(q)
        # 把本页切出的图挂到对应题上（figures 字段）
        for f in figs:
            qi = f.get('q_index')
            if qi is not None and 0 <= qi < len(qs):
                qs[qi].setdefault('figures', []).append(f)
            elif all_q:
                # 没匹配到：挂到本页第一题，标记待确认
                all_q[-len(qs) if qs else 0].setdefault('figures', []).append(
                    dict(f, unassigned=True))
        if figs:
            page_meta[-1]['figures'] = len(figs)

        # ★ 表格：按 y 区间归到题，转成文本表格嵌进题干
        if enh.get('tables'):
            for tb in enh['tables']:
                bb = tb.get('bbox')
                if not bb:
                    continue
                cy = (bb[1] + bb[3]) / 2.0
                qi = _assign_figure_to_question(
                    {'y0': bb[1], 'y1': bb[3]}, qs, cy)
                grid = tb.get('grid') or []
                txt = _grid_to_text(grid)
                if qi is not None and 0 <= qi < len(qs) and txt:
                    qs[qi]['stem'] = (qs[qi].get('stem') or '') + '\n' + txt
                    qs[qi].setdefault('tables', []).append(
                        {'grid': grid, 'score': tb.get('score')})
                page_meta[-1].setdefault('tables', 0)
                page_meta[-1]['tables'] += 1

        # ★ 解析 ⟦LATEX:...⟧ 标记：从题干里换回 $LaTeX$
        #   顺带把前面合并出来的碎片串重建成真分式
        for q in qs:
            q['stem'] = rebuild_frag_text(_apply_latex_marks(q.get('stem') or ''))
            for o in q.get('options') or []:
                o['content'] = rebuild_frag_text(
                    _apply_latex_marks(o.get('content') or ''))

    draft = {
        'source': source_path,
        'source_name': os.path.basename(source_path),
        'created': time.strftime('%Y-%m-%d %H:%M:%S'),
        'pages': page_meta,
        'total_seconds': round(time.time() - t0, 1),
        'questions': all_q,
        'log': log,
        'qnum_cfg': (dict(DEFAULT_QNUM_CFG, **(qnum_cfg or {}))),
        'manual': bool(boundaries),
    }

    if out_json:
        os.makedirs(os.path.dirname(os.path.abspath(out_json)), exist_ok=True)
        with open(out_json, 'w', encoding='utf-8') as f:
            json.dump(draft, f, ensure_ascii=False, indent=2)
        say(f'[4/4] 草稿已存: {out_json}')

    return draft


def draft_to_text(draft):
    """草稿转可读文本，给人工校对用"""
    out = []
    out.append(f"来源: {draft['source_name']}")
    out.append(f"页数: {len(draft['pages'])}   "
               f"识别题目: {len(draft['questions'])} 道   "
               f"耗时: {draft['total_seconds']}s")
    out.append('=' * 68)
    for i, q in enumerate(draft['questions'], 1):
        out.append('')
        sec = f"  [{q['section']}]" if q.get('section') else ''
        tag = {'choice_single': '单选', 'choice_multi': '多选',
               'fill': '填空', 'judgment': '判断',
               'experiment': '实验', 'calculation': '计算'}.get(guess_type(q), '?')
        out.append(f"--- 第{i}道  (原题号 {q['number'] or '?'}) "
                   f"[{tag}] 第{q.get('page','?')}页{sec} ---")
        out.append(f"题干: {q['stem']}")
        for o in q['options']:
            out.append(f"   {o['label']}. {o['content']}")
        if q['low_conf']:
            out.append(f"   ⚠ 低置信度 {len(q['low_conf'])} 处，请重点核对:")
            for lc in q['low_conf'][:3]:
                out.append(f"      {lc}")
    return '\n'.join(out)


if __name__ == '__main__':
    # 用法 A：渲染预览页  -> python ocr_import.py --render <源> <输出目录>
    # 用法 B：跑 OCR
    #   python ocr_import.py <源> <工作目录> <出JSON> [识别依据] [分界JSON]
    if len(sys.argv) > 1 and sys.argv[1] == '--render':
        rsrc = sys.argv[2]
        rout = sys.argv[3]
        pages = render_pages(rsrc, rout, dpi=150)
        print(json.dumps({'pages': [{'page': n, 'image': p} for n, p in pages]},
                         ensure_ascii=False))
        raise SystemExit(0)

    src = sys.argv[1]
    wd = sys.argv[2] if len(sys.argv) > 2 else \
        os.path.join(os.path.dirname(src), '_ocr_work')
    oj = sys.argv[3] if len(sys.argv) > 3 else os.path.join(wd, 'draft.json')
    # 可选第 4 参：识别依据，如 "plain,li" 或 "plain"（默认全开）
    cfg = None
    if len(sys.argv) > 4 and sys.argv[4].strip():
        want = {s.strip() for s in sys.argv[4].split(',') if s.strip()}
        cfg = {'plain': 'plain' in want,
               'li': 'li' in want,
               'ti': 'ti' in want}
    # 可选第 5 参：人工分界 JSON 文件
    bnd = None
    if len(sys.argv) > 5 and sys.argv[5].strip():
        with open(sys.argv[5], 'r', encoding='utf-8') as f:
            bnd = {int(k): v for k, v in json.load(f).items()}
        cfg = {'plain': True, 'li': True, 'ti': True}  # 人工分界时不用题号
    d = run_ocr(src, wd, oj, qnum_cfg=cfg, boundaries=bnd,
                no_enhance=os.environ.get('PHYSICS_BANK_ENH', '1') == '0',
                no_math=os.environ.get('PHYSICS_BANK_ENH_MATH', '1') == '0',
                no_table=os.environ.get('PHYSICS_BANK_ENH_TABLE', '1') == '0')
    print()
    print(draft_to_text(d))
