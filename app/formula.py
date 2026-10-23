"""
formula.py — LaTeX 公式渲染
方案：matplotlib mathtext 离线渲染（纯 Python，无需 Node/网络）
带磁盘缓存
"""
import os
import hashlib
import matplotlib
matplotlib.use('Agg')   # 无 GUI 后端

# 关键：matplotlib 默认数学字体（dejavusans 的 rm）**不含 CJK 字形**，
# 所以 $F_{\text{合}}$ 里的「合」会被画成空方框（dummy symbol）。
# 物理题里 合力/摩擦力/总功 这类中文下标极常见，必须换成中文字体。
matplotlib.rcParams['mathtext.fontset'] = 'custom'
matplotlib.rcParams['mathtext.rm'] = 'Microsoft YaHei'
matplotlib.rcParams['mathtext.it'] = 'Microsoft YaHei:italic'
matplotlib.rcParams['mathtext.bf'] = 'Microsoft YaHei:bold'
matplotlib.rcParams['mathtext.sf'] = 'Microsoft YaHei'
matplotlib.rcParams['mathtext.tt'] = 'Microsoft YaHei'
matplotlib.rcParams['mathtext.cal'] = 'Microsoft YaHei'

import matplotlib.pyplot as plt
from matplotlib import mathtext

# 单条命令探测用的解析器（惰性创建，不渲染成图，只判能不能解析）
_mt_parser = None


def _get_mt_parser():
    global _mt_parser
    if _mt_parser is None:
        _mt_parser = mathtext.MathTextParser('agg')
    return _mt_parser

# 项目根目录
# ★ 与 db.py 同理：打包后本文件在 <exe目录>\_internal\app\formula.py，
#   若用 dirname(dirname(__file__)) 会把 ROOT 算成 exe 目录，
#   在 exe 旁建出 data\formula_cache\ → 触发便携模式误判。
#   所以：launcher 指定了 PHYSICS_BANK_HOME 就优先用它。
_forced = os.environ.get('PHYSICS_BANK_HOME')
if _forced:
    ROOT = os.path.abspath(_forced)
else:
    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(ROOT, 'data', 'formula_cache')
os.makedirs(CACHE_DIR, exist_ok=True)

# 缓存版本号：改渲染参数（字体等）时递增，避免命中旧缓存里的错字图。
_CACHE_VER = 'v2-cjkfont'

# 缓存内存索引
_mem = {}


# ========================= 错误诊断（人话提示） =========================
# matplotlib mathtext 的报错又长又英文，直接甩给用户等于没说。
# 这里把它翻成「哪错了、怎么改」，并尽量定位到出错字符位置。

# 常见命令拼写相近表：写错了提示「是不是想写 X」
_NEAR_CMDS = {
    'alpha': ['alpha', 'a'], 'apha': ['alpha'], 'alfa': ['alpha'],
    'beta': ['beta', 'B'], 'bata': ['beta'],
    'gamma': ['gamma'], 'gama': ['gamma'],
    'delta': ['Delta', 'delta', 'd'], 'Delta': ['Delta'],
    'theta': ['theta'], 'tetha': ['theta'],
    'lambda': ['lambda'], 'lamda': ['lambda'],
    'omega': ['omega', 'Omega'], 'omga': ['omega'],
    'omega': ['omega'], 'rho': ['rho', 'p'], 'roh': ['rho'],
    'mu': ['mu'], 'nu': ['nu'], 'pi': ['pi'], 'phi': ['phi', 'varphi'],
    'frac': ['frac'], 'frace': ['frac'], 'dfrac': ['frac'],
    'sqrt': ['sqrt'], 'sqr': ['sqrt'], 'squrt': ['sqrt'],
    'vec': ['vec'], 'vect': ['vec'],
    'times': ['times'], 'cdot': ['cdot'], 'div': ['div'],
    'sum': ['sum'], 'int': ['int'], 'pm': ['pm'], 'mp': ['mp'],
    'approx': ['approx'], 'propto': ['propto'], 'infty': ['infty'],
    'text': ['text'], 'mathrm': ['mathrm'], 'circ': ['circ'],
    'Delta': ['Delta'], 'Omega': ['Omega'], 'Gamma': ['Gamma'],
}

# 用户输入 -> 替代写法的直接映射（不认识的 Unicode 数学符号）
_UNICODE_HINT = {
    'α': r'\alpha', 'β': r'\beta', 'γ': r'\gamma', 'δ': r'\delta',
    'ε': r'\epsilon', 'θ': r'\theta', 'λ': r'\lambda', 'μ': r'\mu',
    'π': r'\pi', 'ρ': r'\rho', 'σ': r'\sigma', 'τ': r'\tau',
    'φ': r'\phi', 'ω': r'\omega', 'Δ': r'\Delta', 'Ω': r'\Omega',
    '×': r'\times', '÷': r'\div', '≈': r'\approx', '≠': r'\neq',
    '≤': r'\leq', '≥': r'\geq', '∝': r'\propto', '∞': r'\infty',
    '∑': r'\sum', '∫': r'\int', '√': r'\sqrt{}', '°': r'^{\circ}',
    '·': r'\cdot', '±': r'\pm', '∂': r'\partial', '∇': r'\nabla',
}


def _brace_report(latex):
    """扫括号配对，返回 (是否配平, 人话描述, 出错下标)。

    只在明显不配平时才给提示 —— 不干扰正常公式。
    """
    stack = []
    i = 0
    n = len(latex)
    while i < n:
        ch = latex[i]
        if ch == '\\':          # 反斜杠转义，跳过下一个字符
            i += 2
            continue
        if ch in '{[':
            stack.append((ch, i))
        elif ch in '}]':
            want = '{' if ch == '}' else '['
            if not stack:
                return False, f'第 {i + 1} 个字符多了一个「{ch}」，前面没有对应的「{want}」', i
            top, pos = stack.pop()
            if top != want:
                return False, (f'第 {pos + 1} 个「{top}」和第 {i + 1} 个「{ch}」不配对'
                               f'（应为「{want}」）'), i
        i += 1
    if stack:
        top, pos = stack[-1]
        return False, f'第 {pos + 1} 个「{top}」没有闭合，缺一个「{"}" if top == "{" else "]"}」', pos
    return True, '', -1


def _cmd_report(latex):
    """找出不认识的 \\命令，并给就近拼写建议。返回 (列表, 首个出错下标)。

    ★ matplotlib 没有一张「所有命令」的表：
      latex_to_bakoma(285) 只有符号，_function_names(32) 只有函数名，
      frac/sqrt/text/int/sin 这些一个都不在里面。
      所以判断「命令认不认识」只能靠试渲染 —— 这是实测结论，别改成查表。
    ★ 试探时必须带占位参数（见 _CMD_PROBE），
      否则 \\frac / \\sqrt 会被误报成「不认识」。
    """
    import re as _re
    bad = []
    first = -1
    for m in _re.finditer(r'\\([A-Za-z]+)', latex):
        name = m.group(1)
        if any(_can_parse(p % name) for p in _CMD_PROBE):
            continue
        if first < 0:
            first = m.start()
        bad.append(name)
    return bad, first


def _can_parse(tex):
    """单条片段能否被 mathtext 解析。不落盘、不进缓存。"""
    try:
        _get_mt_parser().parse('$%s$' % tex, dpi=72, prop=None)
        return True
    except Exception:
        return False


# 试探一个命令「是否认识」时必须喂的占位参数。
# ★ 实测（踩了两次坑，别乱改）：
#   \frac 必须给完整的两个参数才认（\frac{1}{2} 过；\frac{}{} / \frac{a} 都不过），
#   \sqrt 只需一个参数（\sqrt{a} 过，\sqrt{} 不过）。
#   所以统一用「名字 + 两个递进参数 + 尾随 x」，
#   一套形式同时满足单参和双参命令。
_CMD_PROBE = [
    '\\%s{a}{b}',
    '\\%s{a}x',
    '\\%s{a}',
]


def diagnose(latex):
    """诊断一段 LaTeX，返回 (是否可以渲染, 人话原因)。

    可以渲染时 reason 为空字符串。
    """
    latex = (latex or '').strip()
    if not latex:
        return False, '公式还是空的'

    # 1) Unicode 数学符号（mathtext 不认）
    hits = [(i, c) for i, c in enumerate(latex) if c in _UNICODE_HINT]
    if hits:
        i, c = hits[0]
        return False, (f'第 {i + 1} 个字符「{c}」mathtext 不认。'
                       f'请写成 {_UNICODE_HINT[c]}（本面板的符号按钮可以直接点）')

    # 2) 括号配对
    ok, msg, _pos = _brace_report(latex)
    if not ok:
        return False, msg

    # 3) 未知命令
    bad, first = _cmd_report(latex)
    if bad:
        name = bad[0]
        sug = _NEAR_CMDS.get(name.lower())
        if sug:
            tips = '、'.join('\\' + s for s in sug)
            extra = f'是不是想写 {tips}？'
        else:
            extra = '检查一下拼写'
        return False, f'没有「\\{name}」这个命令（第 {first + 1} 个字符起）。{extra}'

    # 4) 真让 matplotlib 渲一次，兜底抓剩余语法错
    if render_formula(latex):
        return True, ''

    try:
        fig = plt.figure(figsize=(0.01, 0.01))
        fig.text(0, 0, f'${latex}$', fontsize=16)
        fig.canvas.draw()
        plt.close(fig)
        return False, 'mathtext 无法解析这段公式，请检查括号、上下标和分式写法'
    except Exception as e:
        raw = str(e).strip().split('\n')[0]
        # 把挤压过的常见英文报错翻成中文
        low = raw.lower()
        if 'expected' in low and "'}'" in raw:
            return False, '有个「{」没闭合 —— 每个 { 都要配一个 }'
        if 'expected' in low and "']'" in raw:
            return False, '有个「[」没闭合'
        if 'unknown symbol' in low:
            return False, '有 matplotlib 不认识的符号或命令，检查一下拼写'
        if 'double superscript' in low:
            return False, '出现了两个连续的「^」（上标）。要写 x^{2a} 请把 2a 用花括号括起来'
        if 'double subscript' in low:
            return False, '出现了两个连续的「_」（下标）。要写 x_{ab} 请把 ab 用花括号括起来'
        return False, f'渲染失败：{raw[:120]}'
    finally:
        try:
            plt.close('all')
        except Exception:
            pass


def _norm_color(c):
    """容错：接受 '#RRGGBB' / 'RRGGBB' / RGBColor / (r,g,b)，统一成 '#RRGGBB'。

    调用方（如 export_pptx）可能顺手把主题的 RGBColor 对象传进来，
    它不是 str，直接拿去渲染会失败。
    """
    if isinstance(c, str):
        s = c.strip()
        if s.startswith('#'):
            return s
        return '#' + s if len(s) == 6 else s
    try:
        return '#%02X%02X%02X' % (int(c[0]), int(c[1]), int(c[2]))
    except Exception:
        return '#111111'


def _key(latex, fontsize, dpi, color):
    h = hashlib.sha256(
        f'{_CACHE_VER}|{latex}|{fontsize}|{dpi}|{color}'.encode('utf-8')
    ).hexdigest()[:20]
    return h


def render_formula(latex, fontsize=16, dpi=140, color='#111111'):
    """
    把 LaTeX 公式渲染成 PNG（透明底），返回文件路径。
    失败返回 None。

    color 会进入缓存键 —— 同一个公式在不同字色下（如答案红、题干黑）
    必须缓存成不同文件，否则会拿错颜色的图。
    """
    latex = (latex or '').strip()
    if not latex:
        return None
    color = _norm_color(color)

    k = _key(latex, fontsize, dpi, color)
    if k in _mem and os.path.exists(_mem[k]):
        return _mem[k]

    outpath = os.path.join(CACHE_DIR, f'{k}.png')
    if os.path.exists(outpath):
        _mem[k] = outpath
        return outpath

    # 包上 $...$（mathtext 需要）
    expr = latex
    if not (expr.startswith('$') and expr.endswith('$')):
        expr = f'${expr}$'

    try:
        fig = plt.figure(figsize=(0.01, 0.01))
        fig.text(0, 0, expr, fontsize=fontsize, color=color)
        fig.savefig(outpath, dpi=dpi, bbox_inches='tight',
                    pad_inches=0.05, transparent=True)
        plt.close(fig)
        if os.path.exists(outpath) and os.path.getsize(outpath) > 0:
            _mem[k] = outpath
            return outpath
        return None
    except Exception:
        try:
            plt.close('all')
        except Exception:
            pass
        return None


def is_renderable(latex):
    """测试公式能否渲染"""
    return render_formula(latex) is not None


# 常用物理公式片段（给录入界面做插入按钮）
COMMON_SNIPPETS = [
    ('分式', r'\frac{a}{b}'),
    ('根号', r'\sqrt{x}'),
    ('平方', r'x^2'),
    ('下标', r'x_0'),
    ('Δ', r'\Delta'),
    ('希腊 α', r'\alpha'),
    ('希腊 β', r'\beta'),
    ('希腊 θ', r'\theta'),
    ('希腊 ω', r'\omega'),
    ('希腊 ρ', r'\rho'),
    ('希腊 λ', r'\lambda'),
    ('积分', r'\int_0^t'),
    ('求和', r'\sum_{i=1}^{n}'),
    ('矢量', r'\vec{F}'),
    ('乘号', r'\times'),
    ('度', r'^{\circ}'),
    ('无穷', r'\infty'),
    ('约等于', r'\approx'),
]


if __name__ == '__main__':
    tests = [
        ('分式', r'x = v_0 t + \frac{1}{2}at^2'),
        ('牛顿定律', r'F = ma'),
        ('库仑', r'F = k\frac{q_1 q_2}{r^2}'),
        ('积分', r'\int_0^t v \, dt = s'),
        ('希腊', r'\rho = \frac{RS}{L}'),
        ('根号', r'v = \sqrt{2gh}'),
        ('角速度', r'\omega = 2\pi f'),
        ('错误公式', r'\thisIsNotValid{'),
    ]
    for name, tex in tests:
        p = render_formula(tex)
        status = 'OK  ' if p else 'FAIL'
        print(f'[{status}] {name:<10} {tex}')
        if p:
            print(f'          -> {os.path.basename(p)}  {os.path.getsize(p)} bytes')
