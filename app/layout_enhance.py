# -*- coding: utf-8 -*-
"""
layout_enhance.py — OCR 三件套增强：公式 / 表格 / 多栏

背景（2026-10-22 实测结论）
--------------------------
1. `LayoutDetection`（PP-DocLayout_plus-L）**不会**标注 formula 区域，
   真实物理试卷 30 个框里 0 个 formula。所以公式区不能等版面模型给，
   必须**主动判断哪些文字行含数学内容**。
2. `FormulaRecognition`（PP-FormulaNet_plus-M）单块 0.3~2s（GPU），
   可接受；但只对**裁出来的小图**准，整页喂进去会崩。
3. `TableStructureRecognition`（SLANet）输出 HTML 表格，score 0.9+。
4. 版面模型**无 column 标签**，多栏只能靠 box 的 x 坐标聚类。

设计
----
- `detect_columns(boxes, page_w)`  —— 按 x 聚类判断单栏/双栏，给出每行的栏号
- `find_math_lines(lines)`         —— 从 OCR 文字行里挑出「像公式」的行
- `ocr_formula_crop(page_img, bbox)` —— 裁图 → 公式模型 → LaTeX
- `detect_tables(page_img, boxes)` —— 找表格区并识别成 HTML/网格
- `enhance_page(...)`              —— 一次调用把三件事做完

依赖 paddle 的模型都在 OCR 子进程里跑，主 venv import 本模块不会炸
（模型全惰性加载）。
"""

import os
import re
import sys
import math

APP = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, APP)


# ==================== 惰性模型 ====================

# ==================== 离线模型目录（2026-10-22）====================

def ensure_model_dir():
    """让 paddlex 优先用「随包携带」的模型，不联网。

    引擎包（runtime-ocr）旁边可以放一个 official_models/ 目录，里面是
    预先下好的模型。设置 PADDLE_PDX_CACHE_HOME 后 paddlex 会去
    <cache>/official_models/<模型名> 找，命中就不再下载。

    找不到就什么都不做，让 paddlex 走默认缓存 + 自动下载（联网场景）。
    返回实际使用的模型目录，None 表示未干预。
    """
    if os.environ.get('_PB_MODEL_DIR_DONE'):
        return os.environ.get('_PB_MODEL_DIR')
    os.environ['_PB_MODEL_DIR_DONE'] = '1'

    db_root = None
    try:
        import db
        db_root = getattr(db, 'ROOT', None)
    except Exception:
        pass

    cands = []
    # ① 用户显式指定优先
    env = os.environ.get('PHYSICS_BANK_MODEL_DIR')
    if env:
        cands.append(env)
    # ② 各种可能的模型目录
    #    db.ROOT 会被 PHYSICS_BANK_HOME 影响（测试/便携场景），所以不能只靠它；
    #    从 APP（= <项目>\app）往上走一级才是项目根，不受 HOME 影响。
    proj = os.path.dirname(APP)
    for root in (proj, db_root):
        if not root:
            continue
        cands.append(os.path.join(root, 'runtime-ocr', 'official_models'))
        cands.append(os.path.join(root, 'official_models'))
        cands.append(os.path.join(root, 'build', 'ocr_pack',
                                  'runtime-ocr', 'official_models'))

    cands.append(os.path.join(os.path.dirname(os.path.dirname(APP)),
                              'official_models'))

    for c in cands:
        if c and os.path.isdir(c) and os.listdir(c):
            c = os.path.abspath(c)
            # ★ Paddle Inference 不支持非 ASCII 路径（实测：F:\中文 len=43 失败，
            #   ASCII len=111 成功，报错是 json parse_error/empty input）。
            #   用户机器上程序可能装在「D:\我的题库」这种目录，所以在中文路径下
            #   把模型镜像到纯 ASCII 的临时目录再加载。
            if not c.isascii():
                c2 = _mirror_to_ascii(c)
                if c2:
                    c = c2
            os.environ['PADDLE_PDX_CACHE_HOME'] = os.path.dirname(c)
            os.environ['_PB_MODEL_DIR'] = c
            return c
    return None


def _mirror_to_ascii(src_dir, need=None):
    """把模型目录镜像到纯 ASCII 临时路径（Paddle 不认非 ASCII 路径）。

    目标：%LOCALAPPDATA%/PhysicsBank/model_cache/<ver>/official_models
    已存在且完整就复用，不重复拷。返回镜像后的 official_models 路径。
    """
    import hashlib
    import shutil
    try:
        base = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~')
        tag = hashlib.md5(src_dir.lower().encode('utf-8')).hexdigest()[:8]
        dst = os.path.join(base, 'PhysicsBank', 'model_cache', tag,
                           'official_models')
        if not dst.isascii():
            # 用户的 LOCALAPPDATA 本身含中文（极少见），改用盘符根
            dst = os.path.join(os.environ.get('SystemDrive', 'C:') + '\\',
                               'ProgramData', 'PhysicsBank', 'model_cache',
                               tag, 'official_models')
        if not dst.isascii():
            return None

        # 已镜像且列表一致 → 直接复用
        try:
            a = sorted(os.listdir(src_dir))
            b = sorted(os.listdir(dst))
            if a == b and all(
                    os.path.isdir(os.path.join(dst, x))
                    and os.listdir(os.path.join(dst, x)) for x in b):
                return dst
        except Exception:
            pass

        os.makedirs(dst, exist_ok=True)
        for name in os.listdir(src_dir):
            s = os.path.join(src_dir, name)
            d = os.path.join(dst, name)
            if os.path.isdir(s):
                if os.path.isdir(d):
                    shutil.rmtree(d, ignore_errors=True)
                shutil.copytree(s, d)
        return dst
    except Exception:
        return None


ensure_model_dir()

_LAYOUT = None
_FORMULA = None
_TABLE = None


def get_layout():
    global _LAYOUT
    if _LAYOUT is None:
        from paddleocr import LayoutDetection
        _LAYOUT = LayoutDetection()
    return _LAYOUT


def get_formula():
    global _FORMULA
    if _FORMULA is None:
        os.environ.setdefault('PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK', '1')
        from paddleocr import FormulaRecognition
        _FORMULA = FormulaRecognition()
    return _FORMULA


def get_table():
    global _TABLE
    if _TABLE is None:
        os.environ.setdefault('PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK', '1')
        from paddleocr import TableStructureRecognition
        _TABLE = TableStructureRecognition()
    return _TABLE


# ==================== 工具 ====================

def _to_bbox(coord):
    """版面模型 coordinate → (x0,y0,x1,y1) 整数"""
    if not coord:
        return None
    if isinstance(coord[0], (list, tuple)):
        xs = [p[0] for p in coord]
        ys = [p[1] for p in coord]
        return int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))
    return (int(coord[0]), int(coord[1]), int(coord[2]), int(coord[3]))


# ==================== 多栏检测 ====================

def detect_columns(boxes, page_w, min_gap_ratio=0.04):
    """判断页面是单栏还是双栏，返回 (n_cols, splits)。

    splits 是分隔线的 x 坐标列表。做法：
    1. 取所有 text 类框
    2. 按 x 起点做直方图，找「空白峡谷」（连续无框覆盖的列区间）
    3. 峡谷宽度 > page_w * min_gap_ratio 且位于中部 → 判为分栏线
    """
    tb = []
    for b in boxes:
        if b.get('label') not in ('text', 'paragraph_title', 'content',
                                  'abstract', 'reference'):
            continue
        bb = _to_bbox(b.get('coordinate'))
        if bb:
            tb.append(bb)
    if len(tb) < 6:
        return 1, []

    # 覆盖度：每个 x 像素被多少框覆盖
    cover = [0] * page_w
    for x0, y0, x1, y1 in tb:
        x0 = max(0, x0)
        x1 = min(page_w, x1)
        for x in range(x0, x1):
            cover[x] += 1

    # 找连续空白区间
    gaps = []
    run_start = None
    for x in range(page_w):
        if cover[x] == 0:
            if run_start is None:
                run_start = x
        else:
            if run_start is not None:
                gaps.append((run_start, x - 1))
                run_start = None
    if run_start is not None:
        gaps.append((run_start, page_w - 1))

    # 只保留「够宽 + 在中部」的峡谷
    min_w = page_w * min_gap_ratio
    splits = []
    for g0, g1 in gaps:
        w = g1 - g0
        mid = (g0 + g1) / 2.0
        if w >= min_w and 0.25 * page_w < mid < 0.75 * page_w:
            splits.append(int(mid))

    # 取最宽的那个作为分栏线（避免页边距也被算进来）
    if splits:
        return 2, [max(splits, key=lambda s: -abs(s - page_w / 2))]
    return 1, []


def column_of(bbox, splits):
    """给一个框判断属于第几栏（0 起）"""
    if not splits:
        return 0
    cx = (bbox[0] + bbox[2]) / 2.0
    n = 0
    for s in splits:
        if cx > s:
            n += 1
    return n


# ==================== 公式候选 ====================

# 明显的数学符号/结构特征（在 OCR 文本里出现即高度可疑）
_MATH_TOKENS = re.compile(
    r'[≈≠≤≥±×÷·√∫∑∏∞∝∠⊥∥°′″]'
    r'|\^\d|\^\{|_\{|\\frac|\\sqrt'
    r'|\d\s*/\s*\d'          # 1/2
)

# 这些说明「很可能不是公式」
_NOT_MATH = re.compile(
    r'^[A-D][.、．)）]'      # 选项行
    r'|^第\s*\d+\s*题'
    r'|^[（(]\s*\d+\s*[）)]\s*$'  # 纯题号
)

# 中文常见字（用来判「这是一句话」）
_CN = re.compile(r'[一-龥]')


def _strip_marks(t):
    """去掉 OCR 里已有的 $...$ / ⟦LATEX:...⟧ 标记，只留主内容"""
    t = re.sub(r'\u27e6LATEX:.+?\u27e7', ' ', t or '')
    # 只在 $ 成对出现时才剥，否则整个字符串会被当成一对
    if t.count('$') >= 2:
        t = re.sub(r'\$[^$]+\$', ' ', t)
    return t.strip()


# 纯单位：整行就是「单位本身」，不是公式。
# ★ 实测：Q7 的选项是 1/6 这类分式，PDF 里分式右边跟着 m/s²，
#   OCR 把单位切成了一个独立的文本块。旧逻辑把这块丢给公式模型，
#   模型把 m/s² 识别成 $\mathbf{m}/s^{2}$，于是四个选项全变成
#   `m/s² $\mathbf{m}/s^{2}$` —— 选项全废。
_UBASE = (r'(?:mol|rad|min|kg|Hz|Pa|cd|lx|Bq|Gy|Sv|kN|km|cm|mm|nm|ms'
          r'|m|s|g|N|J|W|A|V|C|T|K|h|d|\u03a9|\u2103)')
_UBLK = (r'(?:' + _UBASE + r')'
         r'(?:/(?:' + _UBASE + r')(?:\^[23]|[²³]|[23](?![0-9A-Za-z]))?)?'
         r'(?:\^[23]|[²³])?')
_PURE_UNIT = re.compile(
    r'^[\s(（\[]*' + _UBLK + r'(?:[\s·.\*]' + _UBLK + r')*'
    r'[\s)）\].,;，。：:;]*$')


def is_pure_unit(text):
    """整行是不是只有单位，比如 `m/s²` `N/kg` `kg·m/s²`"""
    t = _strip_marks(text)
    if not t or len(t) > 24:
        return False
    return bool(_PURE_UNIT.match(t))


def is_pure_expr(text):
    """整行是不是「纯数学式子」，没有任何中文字。

    纯式才适合整行交给公式模型；中英混排（`前时间通过的位移比后时间...少 1.5vt`）
    把中文也发去公式模型必然丢内容。
    """
    t = _strip_marks(text)
    if not t or len(t) < 2:
        return False
    cn = _CN.findall(t)
    # 允许英文单词（如选项里的 write up），但一个汉字都不能有
    if cn:
        return False
    # 至少要有一个数学特征，不要把 `ABCD` `图甲` 这类当纯式
    return bool(re.search(r'[=≈≠≤≥+\-*/×÷·^_(){}<>\[\]\d]', t))


def looks_like_math(text):
    """粗判一行 OCR 文本是否含数学内容\n
    关键：**中文词多的行不能当公式**。但也不能把
    `v = v0 + at` 这种真公式挡掉（它也有空格和字母）。
    所以用**信号打分**，不是单一阈值。
    """
    t = (text or '').strip()
    if len(t) < 3 or len(t) > 200:
        return False
    if _NOT_MATH.search(t):
        return False
    # ★ 纯单位不是公式（否则 Q7 那样四个选项全废）
    if is_pure_unit(t):
        return False

    cn = len(_CN.findall(t))
    # 中文极多（占一半以上）→ 是句话，不是公式
    if cn and cn >= len(t) * 0.45:
        return False

    score = 0
    # 强信号：等号/不等号/标准数学符号
    if re.search(r'[=≈≠≤≥]', t):
        score += 3
    if re.search(r'[√∫∑∏±×÷·°]', t):
        score += 2
    # 中强：上标下标/分式/带单位的数量级
    if re.search(r'\^|²|³|_\{', t):
        score += 2
    if re.search(r'\d\s*/\s*\d', t):
        score += 2
    # 弱信号：数字+字母混排（如 v0 / 2kg / 3s）
    if re.search(r'\d\s*[A-Za-z]|[A-Za-z]\s*\d', t):
        score += 1
    if re.search(r'[一-龥]\s*[=≈]\s*[\dA-Za-z]', t):
        score += 1

    # 数学字符密度
    math_chars = sum(1 for c in t
                     if c in '=+-*/^_(){}[]<>|.,0123456789²³')
    dense = math_chars / float(len(t))
    if dense > 0.45:
        score += 2
    elif dense > 0.30:
        score += 1

    return score >= 3


def find_math_lines(lines):
    """从 ocr_image 的 [(text, conf, x0,y0,x1,y1)] 里挑公式候选

    分两类（kinds 字段），它们后续处理不同：
      'pure' —— 整行就是一个数学式，可以整行交给公式模型
      'mixed' —— 中英混排，只能用「与原文取并集」的方式补 LaTeX

    返回 [{'index','text','bbox','conf','kind'}]，index 是在 lines 里的下标。
    """
    out = []
    for i, ln in enumerate(lines):
        text, conf, x0, y0, x1, y1 = ln
        if conf < 0.35:
            continue
        if is_pure_unit(text):
            continue
        if not looks_like_math(text):
            continue
        out.append({'index': i, 'text': text, 'conf': conf,
                    'kind': 'pure' if is_pure_expr(text) else 'mixed',
                    'bbox': (int(x0), int(y0), int(x1), int(y1))})
    return out


# ==================== 公式识别 ====================

def recognize_formula(page_img, bbox, pad=6):
    """裁一块区域丢给公式模型，返回 LaTeX 或 None。

    ⚠ 实测发现模型会把「填空横线」误认成分式（如 `\\frac{m/s^2}{m/s^2}`），
    所以这里加两道呶口：
    1. 结果必须能被 mathtext 渲染（否则属于乱码）
    2. 分式两边的分子/分母不能一样（横线误判的典型特征）
    """
    lx = _recognize_formula_raw(page_img, bbox, pad)
    if not lx:
        return None
    if not _latex_sane(lx):
        return None
    return lx


def _latex_sane(lx):
    """粗验 LaTeX 结果是否可信"""
    if not lx or len(lx) < 2:
        return False
    # ① 分子分母完全相同的分式 → 几乎肯定是横线误判
    for m in re.finditer(r'\\frac\{([^{}]*)\}\{([^{}]*)\}', lx):
        if m.group(1).strip() == m.group(2).strip():
            return False
    # ② 多重嵌套但内容全一样（同上症状）
    if re.search(r'\\frac\{([^{}]+)\}\{\1\}', lx):
        return False
    # ③ 尝试用项目自己的渲染器验一下（不抛异常就算过）
    try:
        import formula
        return bool(formula.is_renderable(lx))
    except Exception:
        return True


def _recognize_formula_raw(page_img, bbox, pad=6):
    """纯裁图 + 模型推理，不做质量判断"""
    try:
        from PIL import Image
        im = Image.open(page_img).convert('RGB')
        W, H = im.size
        x0 = max(0, bbox[0] - pad)
        y0 = max(0, bbox[1] - pad)
        x1 = min(W, bbox[2] + pad)
        y1 = min(H, bbox[3] + pad)
        if x1 - x0 < 12 or y1 - y0 < 8:
            return None
        crop = im.crop((x0, y0, x1, y1))
        # 太小的放大一点，模型对小图不友好
        if crop.width < 64:
            sc = 64.0 / crop.width
            crop = crop.resize((int(crop.width * sc),
                                int(crop.height * sc)),
                               Image.LANCZOS)
        tmp = os.path.join(os.path.dirname(page_img),
                           '_fm_%d_%d.png' % (x0, y0))
        crop.save(tmp)
        try:
            m = get_formula()
            res = m.predict(tmp)
            for r in res:
                d = r.json if hasattr(r, 'json') else r
                if callable(d):
                    d = d()
                rd = d.get('res', d)
                lx = rd.get('rec_formula')
                if lx:
                    return str(lx).strip()
        finally:
            try:
                os.remove(tmp)
            except Exception:
                pass
    except Exception:
        return None
    return None


def enhance_math_lines(page_img, lines, max_lines=12, verbose=False):
    """把含数学的行重新用公式模型识别。

    返回 {line_index: {'latex':..., 'text':...}}，其中：
      latex  —— 公式模型给的结果，None 表示模型失败/被质量闸门拦下
      text   —— OCR 原文

    ★ 为什么必须把 text 一起带回去（2026-10-22 实测踩坑）：
      原先调用方是拿 LaTeX **替换**原文。模型一旦失败或被闸门拦下，
      返回 None，调用方就把这一行丢了 —— 导致 `$m/s^2$`、`x=10t- \\frac{1}{4}t^2`
      这类整块内容凭空消失，题目直接缺一块。谁都不能保证模型 100% 成功，
      所以失败时必须能退回原文。
    """
    cands = find_math_lines(lines)
    if not cands:
        return {}
    cands = cands[:max_lines]
    out = {}
    for c in cands:
        lx = recognize_formula(page_img, c['bbox'])
        if lx and len(lx) < 2:
            lx = None
        out[c['index']] = {'latex': lx, 'text': c['text']}
        if verbose:
            print('      公式 #%d: %s' % (c['index'],
                                         (lx or '（保留原文）')[:70]))
    return out


# ==================== 表格 ====================

def _html_to_grid(html):
    """把 SLANet 输出的 HTML 表格转成二维文本数组"""
    cells = re.findall(r'<td[^>]*>(.*?)</td>', html, re.S)
    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', html, re.S)
    grid = []
    for r in rows:
        cs = re.findall(r'<td[^>]*>(.*?)</td>', r, re.S)
        grid.append([re.sub(r'<[^>]+>', '', c).strip() for c in cs])
    return grid


def recognize_table(page_img, bbox=None):
    """识别表格，返回 {'html','grid','score'} 或 None"""
    try:
        from PIL import Image
        src = page_img
        tmp = None
        if bbox:
            im = Image.open(page_img).convert('RGB')
            W, H = im.size
            box = (max(0, bbox[0]), max(0, bbox[1]),
                   min(W, bbox[2]), min(H, bbox[3]))
            if box[2] - box[0] < 30 or box[3] - box[1] < 20:
                return None
            im = im.crop(box)
            tmp = os.path.join(os.path.dirname(page_img),
                               '_tb_%d_%d.png' % (box[0], box[1]))
            im.save(tmp)
            src = tmp
        try:
            m = get_table()
            res = m.predict(src)
            for r in res:
                d = r.json if hasattr(r, 'json') else r
                if callable(d):
                    d = d()
                rd = d.get('res', d)
                html = rd.get('structure')
                if isinstance(html, list):
                    html = ''.join(html)
                if not html:
                    continue
                grid = _html_to_grid(html)
                # 全空表格不要
                if not any(any(c for c in row) for row in grid):
                    continue
                return {'html': html, 'grid': grid,
                        'score': rd.get('structure_score')}
        finally:
            if tmp:
                try:
                    os.remove(tmp)
                except Exception:
                    pass
    except Exception:
        return None
    return None


# ==================== 统一增强入口 ====================

def enhance_page(page_img, lines, work_dir=None, page_no=1,
                 do_math=True, do_table=True, do_columns=True,
                 verbose=False, max_math=12):
    """一页 OCR 结果的综合增强。

    参数
    ----
    page_img : 页图路径
    lines    : ocr_image 返回的 [(text, conf, x0,y0,x1,y1)]
    do_*     : 各功能开关

    返回 dict：
      {
        'n_cols': 1|2,
        'splits': [x...],
        'line_col': [0,0,1,...]         # 每行属于第几栏
        'math': {line_idx: latex},
        'tables': [{'grid','html','score','bbox'}],
        'layout': [{'label','score','bbox'}]  # 原版面结果（可选）
      }
    """
    out = {'n_cols': 1, 'splits': [], 'line_col': [],
           'math': {}, 'tables': [], 'layout': []}
    if not lines and not (do_table):
        return out

    try:
        from PIL import Image
        W, H = Image.open(page_img).size
    except Exception:
        W = H = 0

    boxes = []
    if do_columns or do_table:
        try:
            lm = get_layout()
            res = lm.predict(page_img)
            for r in res:
                d = r.json if hasattr(r, 'json') else r
                if callable(d):
                    d = d()
                rd = d.get('res', d)
                for b in rd.get('boxes', []) or []:
                    bb = _to_bbox(b.get('coordinate'))
                    if bb:
                        boxes.append({'label': b.get('label'),
                                      'score': b.get('score', 0),
                                      'bbox': bb})
                break
        except Exception as e:
            if verbose:
                print('      版面分析跳过:', e)
    out['layout'] = boxes

    # ---- 多栏 ----
    if do_columns and W:
        n, splits = detect_columns(boxes, W)
        out['n_cols'] = n
        out['splits'] = splits
        out['line_col'] = [column_of((ln[2], ln[3], ln[4], ln[5]), splits)
                           for ln in lines]
        if verbose and n > 1:
            print('      检测到 %d 栏，分界 x=%s' % (n, splits))

    # ---- 表格 ----
    if do_table:
        tb_boxes = [b for b in boxes if b['label'] == 'table']
        for b in tb_boxes[:4]:       # 每页最多 4 个表
            t = recognize_table(page_img, b['bbox'])
            if t:
                t['bbox'] = b['bbox']
                out['tables'].append(t)
                if verbose:
                    print('      表格: %dx%d score=%s' % (
                        len(t['grid']), len(t['grid'][0]) if t['grid'] else 0,
                        t.get('score')))

    # ---- 公式 ----
    if do_math and lines:
        out['math'] = enhance_math_lines(page_img, lines,
                                         max_lines=max_math, verbose=verbose)

    return out
