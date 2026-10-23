# -*- coding: utf-8 -*-
"""
figure_detect.py — 从试卷页图里检测「配图区域」并裁剪。

背景（用户 2026-10-12 需求）：
    OCR 只认文字行，把插图（轨迹图/电路图/装置图）拆成零散文字标注
    混进题干，图的边界和「这是一张图」的事实都丢了。
    本模块负责：找出页面上的图区域 → 裁成独立 PNG →
    当作题目配图挂到对应题上。

★ 主路线（2026-10-12，用户选「方案 B」）：
    用 PaddleOCR 的 **PP-DocLayout_plus-L** 版面分析模型检测
    text / image / table / formula 等区域，取 image/figure 类区域即为配图。
    实测（用户样张）：
      图 1（题干+右侧装置图）→ image @ (327,94,549,214) conf 0.665 ✓
      图 2（整幅配图）        → image @ (1,9,286,159)    conf 0.876 ✓
    真实试卷 page1 → image @ (905,1743,1407,1924) conf 0.913 ✓

★ 为什么之前的手写像素启发式不行（保留为 fallback）：
    图 1 里「图」与「文字」在垂直方向并排 —— 左侧题干延伸到 x≈320，
    右侧装置图在 x≈330–565，共用同一段 y 区间；且图自带文字标注
    （引线/纸盖/发射筒…）会被当成"文字列"，把图所在列屏蔽掉。
    盲调三版：48px 一条 → 0 个。没有视觉模型时不该硬调启发式。

★ 模型不可用时的 fallback：像素版面启发式（能出几张是几张，
  质量不如模型，但不至于完全没功能）。
"""
import os
import numpy as np


# PP-DocLayout 的「图形」类标签（含中英文可能写法，统一小写比对）
FIGURE_LABELS = {
    'image', 'figure', 'chart', 'picture', 'seal', 'formula', 'table',
}
# 这些不算配图（公式/表格是否要另说，先只取 image/figure/picture/chart）
FIGURE_LABELS = {'image', 'figure', 'picture', 'chart'}

# ---- fallback 启发式调参（偏保守：宁可少切）----
MIN_AREA_RATIO = 0.008
MIN_W_RATIO = 0.05
MIN_H_RATIO = 0.03
MAX_AREA_RATIO = 0.80
DENSITY_MIN = 0.010
DENSITY_MAX = 0.90
EDGE_MARGIN = 0.025
INK_THRESH = 200

_LAYOUT_MODEL = None


# =====================================================================
#  主路线：PP-DocLayout 版面分析
# =====================================================================
def get_layout_model(name='PP-DocLayout_plus-L'):
    """惰性加载版面分析模型（同一进程内复用）。"""
    global _LAYOUT_MODEL
    if _LAYOUT_MODEL is None:
        _ensure_model_dir()
        from paddleocr import LayoutDetection
        _LAYOUT_MODEL = LayoutDetection(model_name=name)
    return _LAYOUT_MODEL


def _ensure_model_dir():
    """优先用随包模型目录（离线不联网）；找不到就不干预，走默认缓存。

    引擎包旁的 official_models\\ 里预先放好模型，
    把 PADDLE_PDX_CACHE_HOME 指到它的父目录，paddlex 就会直接命中。
    """
    if os.environ.get('_PB_MODEL_DIR_DONE'):
        return
    os.environ['_PB_MODEL_DIR_DONE'] = '1'
    cands = []
    env = os.environ.get('PHYSICS_BANK_MODEL_DIR')
    if env:
        cands.append(env)
    db_root = None
    try:
        import db
        db_root = getattr(db, 'ROOT', None)
    except Exception:
        pass
    # proj = <项目>\app 的上一级；不受 PHYSICS_BANK_HOME 影响
    proj = os.path.dirname(APP)
    for root in (proj, db_root):
        if not root:
            continue
        cands.append(os.path.join(root, 'official_models'))
        cands.append(os.path.join(root, 'runtime-ocr', 'official_models'))
        cands.append(os.path.join(root, 'build', 'ocr_pack',
                                  'runtime-ocr', 'official_models'))
    for c in cands:
        if c and os.path.isdir(c) and os.listdir(c):
            c = os.path.abspath(c)
            # ★ 必须绝对路径：paddlex 会切目录后读 config，相对路径读空
            os.environ['PADDLE_PDX_CACHE_HOME'] = os.path.dirname(c)
            return


def _json_res(res):
    """从 PaddleOCR 结果对象里取 res 字典。"""
    try:
        d = res.json
    except Exception:
        d = res if isinstance(res, dict) else {}
    if isinstance(d, dict) and 'res' in d:
        d = d['res']
    return d or {}


def _coord_of(b):
    c = b.get('coordinate') or b.get('bbox') or b.get('block_bbox')
    if isinstance(c, dict):
        return int(c['x0']), int(c['y0']), int(c['x1']), int(c['y1'])
    if c is None:
        return None
    return int(c[0]), int(c[1]), int(c[2]), int(c[3])


def detect_figures_layout(page_img_path, conf_thresh=0.30, model_name=None):
    """用版面分析模型找配图。返回 [{'x0','y0','x1','y1','conf','label'}, ...]。

    模型不可用（没装/加载失败）时抛异常，由 detect_figures 兜底。
    """
    model = get_layout_model(model_name) if model_name else get_layout_model()
    res = model.predict(page_img_path, batch_size=1)[0]
    data = _json_res(res)
    boxes = data.get('boxes', [])
    figs = []
    for b in boxes:
        label = str(b.get('label', '')).strip().lower()
        if label not in FIGURE_LABELS:
            continue
        conf = float(b.get('score') or 0.0)
        if conf < conf_thresh:
            continue
        c = _coord_of(b)
        if not c:
            continue
        x0, y0, x1, y1 = c
        if x1 <= x0 or y1 <= y0:
            continue
        figs.append({'x0': x0, 'y0': y0, 'x1': x1, 'y1': y1,
                     'conf': round(conf, 3), 'label': label,
                     'area': (x1 - x0) * (y1 - y0)})
    figs.sort(key=lambda f: (f['y0'], f['x0']))
    return figs


# =====================================================================
#  统一入口：优先模型，失败退回启发式
# =====================================================================
def detect_figures(page_img_path, text_boxes=None, use_layout=True,
                   conf_thresh=0.30, debug=False):
    """找一页里的配图。返回 [{'x0','y0','x1','y1',...,'source'}, ...]。

    优先用 PP-DocLayout；不可用时退回像素启发式（需提供 text_boxes）。
    """
    if use_layout:
        try:
            figs = detect_figures_layout(page_img_path, conf_thresh=conf_thresh)
            for f in figs:
                f['source'] = 'layout'
            if debug:
                print('  [layout] 检出配图 %d 个 -> ' % len(figs)
                      + ', '.join(f"({f['x0']},{f['y0']},{f['x1']},{f['y1']})"
                                  f" c={f.get('conf')}" for f in figs[:6]))
            return figs
        except Exception as e:
            if debug:
                print('  [layout] 不可用，退回启发式：%s' % e)
    figs = detect_figures_heuristic(page_img_path, text_boxes or [], debug=debug)
    for f in figs:
        f['source'] = 'heuristic'
    return figs


# =====================================================================
#  Fallback：像素版面启发式（行带局部）
# =====================================================================
def _load_gray(page_img_path):
    from PIL import Image
    im = Image.open(page_img_path).convert('L')
    return np.asarray(im, dtype=np.uint8)


def _box_overlaps_text(x0, y0, x1, y1, boxes, pad=4):
    if not boxes:
        return 0.0
    area = max(1, (x1 - x0) * (y1 - y0))
    cov = 0.0
    for (a, b, c, d) in boxes:
        ix = max(0, min(x1, c + pad) - max(x0, a - pad))
        iy = max(0, min(y1, d + pad) - max(y0, b - pad))
        cov += ix * iy
    return min(1.0, cov / area)


def _connected_blocks(mask, min_area):
    try:
        from scipy import ndimage
        lab, n = ndimage.label(mask)
        if n == 0:
            return []
        out = []
        for i, s in enumerate(ndimage.find_objects(lab), 1):
            if s is None:
                continue
            ys, xs = s
            if int((lab[s] == i).sum()) >= min_area:
                out.append((xs.start, ys.start, xs.stop, ys.stop))
        return out
    except Exception:
        return _grid_blocks(mask, min_area)


def _grid_blocks(mask, min_area, cell=24):
    H, W = mask.shape[:2]
    gh, gw = H // cell + 1, W // cell + 1
    dens = np.zeros((gh, gw), dtype=float)
    for j in range(gh):
        for i in range(gw):
            sub = mask[j*cell:(j+1)*cell, i*cell:(i+1)*cell]
            if sub.size:
                dens[j, i] = sub.mean()
    hot = dens > 0.10
    blocks = []
    used = np.zeros_like(hot, dtype=bool)
    for j in range(gh):
        for i in range(gw):
            if hot[j, i] and not used[j, i]:
                j2, i2 = j, i
                while j2 + 1 < gh and hot[j2+1, i:i2+1].any():
                    j2 += 1
                while i2 + 1 < gw and hot[j:j2+1, i2+1].any():
                    i2 += 1
                used[j:j2+1, i:i2+1] = True
                x0, y0 = i*cell, j*cell
                x1, y1 = min(W, (i2+1)*cell), min(H, (j2+1)*cell)
                if int(mask[y0:y1, x0:x1].sum()) >= min_area:
                    blocks.append((x0, y0, x1, y1))
    return blocks


def _clean_noise(mask):
    try:
        from scipy import ndimage
        return ndimage.binary_opening(mask, structure=np.ones((3, 3)))
    except Exception:
        m = mask
        e = m.copy()
        e[1:, :] &= m[:-1, :]; e[:-1, :] &= m[1:, :]
        e[:, 1:] &= m[:, :-1]; e[:, :-1] &= m[:, 1:]
        d = e.copy()
        d[1:, :] |= e[:-1, :]; d[:-1, :] |= e[1:, :]
        d[:, 1:] |= e[:, :-1]; d[:, :-1] |= e[:, 1:]
        return d


def detect_figures_heuristic(page_img_path, text_boxes, debug=False):
    """行带局部像素启发式（fallback）。text_boxes: [[x0,y0,x1,y1],...]"""
    gray = _load_gray(page_img_path)
    H, W = gray.shape[:2]
    page_area = float(H * W)
    ink = (gray < INK_THRESH)
    min_area = page_area * MIN_AREA_RATIO

    band_h = max(6, H // 80)
    mask = np.zeros_like(ink)
    for y0 in range(0, H, band_h):
        y1 = min(H, y0 + band_h)
        here = [b for b in text_boxes if b[3] > y0 and b[1] < y1]
        band_ink = ink[y0:y1]
        if not here:
            mask[y0:y1] = band_ink
            continue
        cover = np.zeros(W, dtype=bool)
        for (a, b, c, d) in here:
            a = max(0, int(a) - 2); c = min(W, int(c) + 2)
            if c > a:
                cover[a:c] = True
        band = band_ink.copy()
        band[:, cover] = False
        mask[y0:y1] = band

    for (a, b, c, d) in text_boxes:
        a = max(0, int(a) - 1); b = max(0, int(b) - 1)
        c = min(W, int(c) + 1); d = min(H, int(d) + 1)
        mask[b:d, a:c] = False

    mask = _clean_noise(mask)
    blocks = _connected_blocks(mask.astype(np.uint8), int(min_area / 2))

    figs = []
    for (x0, y0, x1, y1) in blocks:
        bw, bh = x1 - x0, y1 - y0
        if bw <= 0 or bh <= 0:
            continue
        a_ratio = (bw * bh) / page_area
        if a_ratio < MIN_AREA_RATIO or a_ratio > MAX_AREA_RATIO:
            continue
        if bw / W < MIN_W_RATIO or bh / H < MIN_H_RATIO:
            continue
        if (x0 <= W * EDGE_MARGIN and x1 >= W * (1 - EDGE_MARGIN)
                and bh / H < 0.05):
            continue
        sub = ink[y0:y1, x0:x1]
        dens = float(sub.mean())
        if dens < DENSITY_MIN or dens > DENSITY_MAX:
            continue
        if _box_overlaps_text(x0, y0, x1, y1, text_boxes) > 0.55:
            continue
        figs.append({'x0': int(x0), 'y0': int(y0), 'x1': int(x1),
                     'y1': int(y1), 'area': int(bw * bh), 'conf': None,
                     'label': 'heuristic'})
    figs = _merge_nearby(figs, H, W)
    figs.sort(key=lambda f: (f['y0'], f['x0']))
    if debug:
        print('  [heuristic] 检出配图 %d 个' % len(figs))
    return figs


def _merge_nearby(figs, page_h, page_w, gap_ratio=0.025):
    if not figs:
        return []
    gap = page_h * gap_ratio
    gap_x = page_w * 0.04
    merged = []
    for f in sorted(figs, key=lambda f: f['y0']):
        hit = None
        for m in merged:
            y_ok = (-m['y1'] + f['y0']) <= gap and f['y0'] <= m['y1'] + gap
            hgap = max(m['x0'] - f['x1'], f['x0'] - m['x1'])
            if y_ok and hgap <= gap_x:
                hit = m
                break
        if hit is None:
            merged.append(dict(f))
        else:
            hit['x0'] = min(hit['x0'], f['x0'])
            hit['y0'] = min(hit['y0'], f['y0'])
            hit['x1'] = max(hit['x1'], f['x1'])
            hit['y1'] = max(hit['y1'], f['y1'])
            hit['area'] = (hit['x1'] - hit['x0']) * (hit['y1'] - hit['y0'])
    return merged


def crop_figure(page_img_path, fig, out_path, margin=6):
    """按区域裁出配图（外扩 margin 像素），存 PNG。返回 out_path。"""
    from PIL import Image
    im = Image.open(page_img_path).convert('RGB')
    W, H = im.size
    x0 = max(0, fig['x0'] - margin)
    y0 = max(0, fig['y0'] - margin)
    x1 = min(W, fig['x1'] + margin)
    y1 = min(H, fig['y1'] + margin)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    im.crop((x0, y0, x1, y1)).save(out_path)
    return out_path
