"""
excel_import.py — Excel/CSV 批量导入

支持两种格式：
  A. 宽表（每题一行）—— 推荐给手工整理的数据
  B. 长表（选项一行一个）—— 从其他系统导出的数据

宽表列（表头名大小写/空格不敏感，可用中英文）：
  题干 / stem             必填，支持 $LaTeX$
  选项A / A / option_a     选填，选择题用
  选项B / B / option_b
  选项C / C / option_c
  选项D / D / option_d
  答案 / answer           选填；选择题可写 "B" 或 "B、C"（多选）
  解析 / solution         选填
  题型 / type             choice/fill/calculation/experiment 或 中文
  难度 / difficulty       1-5，默认 3
  知识点 / kp             多个用逗号/顿号/分号分隔，不存在则自动新建
  来源 / source           选填
  页码 / page             选填
  年级 / grade            选填
  备注 / note             选填

另提供模版导出，用户填完再导入。
"""
import os
import re
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db

# ---------------- 表头别名 ----------------
ALIASES = {
    'stem': ['题干', '题目', '问题', 'stem', 'question', '内容'],
    'answer': ['答案', 'answer', 'ans'],
    'solution': ['解析', '解答', '详解', 'solution', 'analysis'],
    'question_type': ['题型', '类型', 'type', 'question_type'],
    'difficulty': ['难度', 'difficulty', 'diff', '星级'],
    'kp': ['知识点', '考点', 'kp', 'knowledge', 'knowledge_point'],
    'source': ['来源', '出处', 'source', '试卷'],
    'source_page': ['页码', '页', 'page', 'source_page'],
    'grade': ['年级', 'grade'],
    'note': ['备注', '说明', 'note', 'remark'],
    'A': ['选项a', 'a', 'option_a', '选项A', '选项1'],
    'B': ['选项b', 'b', 'option_b', '选项B', '选项2'],
    'C': ['选项c', 'c', 'option_c', '选项C', '选项3'],
    'D': ['选项d', 'd', 'option_d', '选项D', '选项4'],
    'E': ['选项e', 'e', 'option_e', '选项E', '选项5'],
    'stem_fig': ['配图', '图', 'figure', 'stem_fig', '图片'],
}

TYPE_MAP = {
    'choice': 'choice_single',
    '单选': 'choice_single', '单项选择题': 'choice_single',
    '单项选择': 'choice_single', 'choice_single': 'choice_single',
    '多选': 'choice_multi', '多项选择': 'choice_multi',
    '多选题': 'choice_multi', 'choice_multi': 'choice_multi',
    'fill': 'fill', '填空': 'fill', '填空题': 'fill',
    'judgment': 'judgment', '判断': 'judgment', '判断题': 'judgment',
    '正误': 'judgment', '对错': 'judgment',
    'calculation': 'calculation', '计算': 'calculation',
    '计算题': 'calculation', '解答': 'calculation', '解答题': 'calculation',
    'experiment': 'experiment', '实验': 'experiment', '实验题': 'experiment',
}

# “选择”/“选择题”太模糊（不知单/多选），不放进 TYPE_MAP，
# 交给 _parse_type 按答案字母数判断。


def _norm(s):
    """表头归一化：去空格、全角转半角、小写"""
    if s is None:
        return ''
    s = str(s).strip().lower()
    s = s.replace('　', ' ')
    s = s.replace('（', '(').replace('）', ')')
    return re.sub(r'[\s_\-]', '', s)


def _build_headmap(headers):
    """把实际表头映射到标准字段名"""
    m = {}
    normed = {_norm(h): i for i, h in enumerate(headers) if h is not None}
    for field, names in ALIASES.items():
        for n in names:
            k = _norm(n)
            if k in normed:
                m[field] = normed[k]
                break
    return m


def _get(row, hm, field, default=''):
    i = hm.get(field)
    if i is None or i >= len(row):
        return default
    v = row[i]
    if v is None:
        return default
    return str(v).strip()


def _parse_type(s, answer=''):
    if not s:
        s = ''
    k = _norm(s)
    for pat, v in TYPE_MAP.items():
        if _norm(pat) == k:
            return v
    # 模糊：包含关键词（先看多选，再看选择）
    if '多选' in s or '多项' in s:
        return 'choice_multi'
    if '单选' in s or '单项' in s:
        return 'choice_single'
    if '填空' in s:
        return 'fill'
    if '判断' in s or '正误' in s or '对错' in s:
        return 'judgment'
    if '实验' in s:
        return 'experiment'
    if '计算' in s or '解答' in s:
        return 'calculation'
    if '选择' in s:
        # 只写“选择题”时，用答案字母数猜单选/多选
        n = len(re.findall(r'[A-Ha-h]', answer or ''))
        return 'choice_multi' if n >= 2 else 'choice_single'
    # 完全没写题型：看有没有选项
    return 'choice_single'


def _parse_diff(s):
    if not s:
        return 3
    m = re.search(r'[1-5]', str(s))
    if m:
        return int(m.group(0))
    # 中文星
    n = str(s).count('★') + str(s).count('星')
    return n if n else 3


def _split_kp(s):
    if not s:
        return []
    parts = re.split(r'[,，、;；/|]+', s)
    return [p.strip() for p in parts if p.strip()]


def _parse_answer_labels(ans):
    """从答案文本里抽出选项字母，如 'B' / 'BC' / 'B、C' → ['B','C']"""
    if not ans:
        return []
    up = ans.upper()
    letters = re.findall(r'[A-E]', up)
    # 去掉明显是英文单词误伤的：只保留出现在答案开头的连续字母块
    m = re.match(r'\s*([A-E](?:\s*[、,，/]?\s*[A-E])*)', up)
    if m:
        return re.findall(r'[A-E]', m.group(1))
    return letters[:4]


def _ensure_kp(name):
    """按名字拿知识点 id，没有就建"""
    for kp in db.list_kp():
        if kp['name'] == name:
            return kp['id']
    return db.add_kp(name)


# ---------------- 读表格 ----------------

def read_table(path):
    """返回 (headers, rows)。支持 .xlsx/.xls/.csv"""
    ext = os.path.splitext(path)[1].lower()
    if ext == '.csv':
        import csv
        for enc in ('utf-8-sig', 'utf-8', 'gbk', 'gb18030'):
            try:
                with open(path, 'r', encoding=enc, newline='') as f:
                    sample = f.read(4096)
                    f.seek(0)
                    try:
                        dialect = csv.Sniffer().sniff(sample, ',;\t')
                    except Exception:
                        dialect = csv.excel
                    rd = list(csv.reader(f, dialect))
                if not rd:
                    return [], []
                return rd[0], rd[1:]
            except UnicodeDecodeError:
                continue
        raise RuntimeError('CSV 编码无法识别（试过 utf-8/gbk/gb18030）')

    # Excel
    try:
        import openpyxl
    except ImportError:
        raise RuntimeError('缺少 openpyxl，请先安装：pip install openpyxl')
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.active
    rows = [[c for c in r] for r in ws.iter_rows(values_only=True)]
    # 去掉全空行
    rows = [r for r in rows if any(c is not None and str(c).strip() for c in r)]
    if not rows:
        return [], []
    return rows[0], rows[1:]


# ---------------- 主流程 ----------------

def preview(path, max_rows=200):
    """试读前若干行，返回解析预览（不入库）"""
    headers, rows = read_table(path)
    hm = _build_headmap(headers)
    out = {'headers': [str(h) if h is not None else '' for h in headers],
           'mapped': {}, 'rows': [], 'problems': []}
    for f, i in hm.items():
        out['mapped'][f] = out['headers'][i]

    if 'stem' not in hm:
        out['problems'].append(
            '找不到「题干」列。可用的表头别名：题干/题目/问题/stem/question/内容')
        return out

    for ri, row in enumerate(rows[:max_rows]):
        stem = _get(row, hm, 'stem')
        if not stem:
            continue
        ans = _get(row, hm, 'answer')
        qtype = _parse_type(_get(row, hm, 'question_type'), ans)
        opts = []
        for L in 'ABCDE':
            c = _get(row, hm, L)
            if c:
                opts.append({'label': L, 'content': c})
        if qtype in ('choice_single', 'choice_multi') and opts and ans:
            correct = _parse_answer_labels(ans)
            for o in opts:
                o['is_correct'] = 1 if o['label'] in correct else 0
        rec = {
            'excel_row': ri + 2,
            'stem': stem,
            'question_type': qtype,
            'difficulty': _parse_diff(_get(row, hm, 'difficulty')),
            'options': opts,
            'answer': ans,
            'solution': _get(row, hm, 'solution'),
            'kp': _split_kp(_get(row, hm, 'kp')),
            'source': _get(row, hm, 'source'),
            'grade': _get(row, hm, 'grade'),
            'note': _get(row, hm, 'note'),
        }
        # 缺答案的警告
        if qtype in ('choice_single', 'choice_multi') and opts and not ans:
            out['problems'].append(f'第 {ri+2} 行：选择题但没有答案')
        out['rows'].append(rec)
    return out


def do_import(path, default_source='', progress=None):
    """正式导入。返回 {'ok':n, 'failed':[(row, err)], 'new_kp':[...]}"""
    headers, rows = read_table(path)
    hm = _build_headmap(headers)
    if 'stem' not in hm:
        raise RuntimeError('找不到「题干」列')

    ok = 0
    failed = []
    new_kp = []
    known_kp = {kp['name'] for kp in db.list_kp()}

    if not default_source:
        default_source = os.path.splitext(os.path.basename(path))[0]

    for ri, row in enumerate(rows):
        excel_row = ri + 2
        stem = _get(row, hm, 'stem')
        if not stem:
            continue
        try:
            ans = _get(row, hm, 'answer')
            qtype = _parse_type(_get(row, hm, 'question_type'), ans)
            opts = []
            for L in 'ABCDE':
                c = _get(row, hm, L)
                if c:
                    opts.append({'label': L, 'content': c})
            if qtype in ('choice_single', 'choice_multi') and opts and ans:
                correct = _parse_answer_labels(ans)
                for o in opts:
                    o['is_correct'] = 1 if o['label'] in correct else 0

            kp_ids = []
            for name in _split_kp(_get(row, hm, 'kp')):
                kp_ids.append(_ensure_kp(name))
                if name not in known_kp:
                    new_kp.append(name)
                    known_kp.add(name)

            page = _get(row, hm, 'source_page')
            try:
                page = int(page) if page else None
            except ValueError:
                page = None

            # 选项并入题干（新数据模型：选项就是题干里的若干行）
            if opts:
                stem_full = stem + '\n' + '\n'.join(
                    f"{o['label']}. {o['content']}".rstrip() for o in opts)
            else:
                stem_full = stem

            db.add_question(
                stem=stem_full,
                answer=ans,
                solution=_get(row, hm, 'solution'),
                question_type=qtype,
                difficulty=_parse_diff(_get(row, hm, 'difficulty')),
                source=_get(row, hm, 'source') or default_source,
                source_page=page,
                grade=_get(row, hm, 'grade'),
                note=_get(row, hm, 'note'),
                options=None,
                kp_ids=kp_ids or None)
            ok += 1
        except Exception as e:
            failed.append((excel_row, str(e)))
        if progress and (ri + 1) % 10 == 0:
            progress(f'已处理 {ri+1}/{len(rows)} 行...')

    return {'ok': ok, 'failed': failed, 'new_kp': new_kp}


# ---------------- 模版 ----------------

TEMPLATE_HEADERS = ['题干', '选项A', '选项B', '选项C', '选项D', '答案',
                    '解析', '题型', '难度', '知识点', '来源', '页码', '年级', '备注']

TEMPLATE_SAMPLE = [
    ['一物体做匀加速直线运动，初速度 $v_0=2\\,\\mathrm{m/s}$，加速度 '
     '$a=3\\,\\mathrm{m/s^2}$，求 $2\\,\\mathrm{s}$ 后的速度。',
     '', '', '', '', '8 m/s', '由 $v=v_0+at$ 得 $v=2+3\\times2=8\\,\\mathrm{m/s}$',
     '计算题', 3, '匀变速直线运动', '示例模版', '', '', ''],
    ['下列说法正确的是（　）',
     '速度是矢量', '速度是标量', '速度方向与位移方向相反',
     '速度大小叫速率', 'ABD',
     '速度是矢量，A 对；C 错；D 对。', '选择题', 2, '运动学基本概念', '示例模版', '', '', ''],
    ['质量为 $m$ 的物体从高处自由下落，$t=3\\,\\mathrm{s}$ 时速度为____。',
     '', '', '', '', '30 m/s', '取 $g=10\\,\\mathrm{m/s^2}$，$v=gt=30\\,\\mathrm{m/s}$',
     '填空题', 2, '自由落体运动', '示例模版', '', '', ''],
    ['匀速圆周运动的物体所受合外力为零。',
     '', '', '', '', '×',
     '匀速圆周运动有向心加速度，合外力提供向心力，不为零。',
     '判断题', 2, '曲线运动', '示例模版', '', '', ''],
]


def write_template(path):
    """导出可填写的模版（xlsx 或 csv）"""
    ext = os.path.splitext(path)[1].lower()
    if ext == '.csv':
        import csv
        with open(path, 'w', encoding='utf-8-sig', newline='') as f:
            w = csv.writer(f)
            w.writerow(TEMPLATE_HEADERS)
            for r in TEMPLATE_SAMPLE:
                w.writerow(r)
        return path
    try:
        import openpyxl
    except ImportError:
        raise RuntimeError('缺少 openpyxl')
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = '题库导入'
    ws.append(TEMPLATE_HEADERS)
    for r in TEMPLATE_SAMPLE:
        ws.append(r)
    # 列宽 + 表头加粗
    from openpyxl.styles import Font, Alignment, PatternFill
    for i, h in enumerate(TEMPLATE_HEADERS, 1):
        c = ws.cell(row=1, column=i)
        c.font = Font(bold=True, color='FFFFFF')
        c.fill = PatternFill('solid', fgColor='4A6FA5')
        c.alignment = Alignment(horizontal='center')
        ws.column_dimensions[c.column_letter].width = 18
    ws.freeze_panes = 'A2'
    for i, h in enumerate(TEMPLATE_HEADERS, 1):
        if h in ('题干', '解析'):
            ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = 50
    wb.save(path)
    return path


if __name__ == '__main__':
    # CLI: python excel_import.py preview <file>
    #      python excel_import.py template <out>
    #      python excel_import.py import <file> [source]
    a = sys.argv[1:]
    if not a:
        print('用法: preview <file> | template <out> | import <file> [source]')
    elif a[0] == 'preview':
        print(json.dumps(preview(a[1]), ensure_ascii=False, indent=2))
    elif a[0] == 'template':
        print('已生成:', write_template(a[1]))
    elif a[0] == 'import':
        print(do_import(a[1], a[2] if len(a) > 2 else ''))
