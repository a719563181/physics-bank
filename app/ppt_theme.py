# -*- coding: utf-8 -*-
"""
ppt_theme.py —— PPT 讲义的「模板 / 主题」系统。

设计目标
--------
把原先散落在 export_pptx.py 里的**硬编码样式常量**（配色、字体、字号、
页边距、封面文字、页脚）抽成可替换的 **Theme** 对象，从而支持：

  * 内置多套配色（蓝 / 墨绿 / 暗红 / 灰白 / 深色）
  * 用户自建主题（在界面上改，或直接编辑 JSON）
  * 主题存成 JSON 落盘，随导出产物一并可携带

一个 Theme 同时描述「观感」和「版式尺度」。export_pptx 只读 Theme，
不再自己定义颜色。

用法
----
    from ppt_theme import Theme, list_themes, load_theme, save_theme
    t = load_theme('墨绿')          # 内置名或 JSON 路径
    export_paper_pptx(pid, out, theme=t)
"""
import json
import os
import re

THEME_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         '..', 'themes')
THEME_DIR = os.path.abspath(THEME_DIR)


# ---------------------------------------------------------------- 主题对象

# 一个主题里所有「颜色」字段（值为 '#RRGGBB'）
_COLOR_FIELDS = (
    'title', 'body', 'muted', 'answer', 'solution',
    'accent', 'accent_fg', 'cover_bg', 'section_fg',
)

# 一个主题里所有「数值 / 文本」字段及默认值
_SCALAR_DEFAULTS = {
    'name': '默认',
    'font': '微软雅黑',
    'mono_font': 'Consolas',
    # 版式（英寸）
    'margin': 0.6,
    'bar_h': 0.72,
    'body_top': 1.0,
    'body_bottom': 7.1,
    # 字号（磅）
    'size_title': 38,
    'size_section': 34,
    'size_end': 30,
    'size_bar': 18,
    'size_bar_right': 12,
    'size_stem': 17,
    'size_tex': 24,
    'size_option': 16,
    'size_option_tex': 21,
    'size_answer': 21,
    'size_answer_tex': 26,
    'size_solution': 14,
    'size_solution_tex': 19,
    'size_label': 15,
    'size_stem_echo': 11,
    # 文案
    'cover_title': '',            # 空 = 用试卷标题
    'cover_subtitle': '',         # 空 = 用试卷描述
    'cover_footer': '高中物理题库管理 · 离线版',
    'end_text': '讲解完毕',
    'bar_left': '第 {idx} 题 / 共 {total} 题',
    'answer_prefix': '答案：',
    'solution_label': '解析',
}

_COLOR_DEFAULTS = {
    'title': '#1A1A1A',
    'body': '#222222',
    'muted': '#808080',
    'answer': '#C0392B',
    'solution': '#1F6F3C',
    'accent': '#2C6FB8',
    'accent_fg': '#FFFFFF',
    'cover_bg': '#F5F7FA',
    'section_fg': '#FFFFFF',
}

_HEX = re.compile(r'^#?(?:[0-9a-fA-F]{6})$')


def _norm_color(v, default):
    if not isinstance(v, str) or not _HEX.match(v.strip()):
        return default
    v = v.strip().lstrip('#').upper()
    return '#' + v


class Theme:
    """一套 PPT 讲义的观感 + 版式参数。"""

    def __init__(self, **kw):
        data = dict(_SCALAR_DEFAULTS)
        data.update(_COLOR_DEFAULTS)
        for k, v in kw.items():
            if k in _COLOR_FIELDS:
                data[k] = _norm_color(v, data[k])
            elif k in _SCALAR_DEFAULTS:
                data[k] = v
        self._d = data

    # --- 访问
    def __getitem__(self, k):
        return self._d[k]

    def get(self, k, default=None):
        return self._d.get(k, default)

    def keys(self):
        return self._d.keys()

    def to_dict(self):
        return dict(self._d)

    def copy(self, **kw):
        d = dict(self._d)
        d.update(kw)
        return Theme(**d)

    # --- 颜色 -> RGBColor（延迟 import，避免无 pptx 环境下炸）
    def rgb(self, field):
        from pptx.dml.color import RGBColor
        h = self._d[field].lstrip('#')
        return RGBColor(int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))

    def emu(self, inches_field):
        return int(round(float(self._d[inches_field]) * 914400))

    def pt(self, size_field):
        return float(self._d[size_field])

    def __repr__(self):
        return f"<Theme {self._d['name']!r} accent={self._d['accent']}>"


# ---------------------------------------------------------------- 内置主题

BUILTIN = {
    '默认蓝': {},
    '墨绿': {
        'name': '墨绿',
        'accent': '#1F6F3C',
        'cover_bg': '#F2F7F2',
        'answer': '#B03A2E',
        'solution': '#1B5E20',
    },
    '暗红': {
        'name': '暗红',
        'accent': '#A93226',
        'cover_bg': '#FBF3F2',
        'answer': '#922B21',
        'solution': '#1F618D',
    },
    '灰白极简': {
        'name': '灰白极简',
        'accent': '#4A4A4A',
        'cover_bg': '#FAFAFA',
        'title': '#000000',
        'answer': '#C0392B',
        'solution': '#2E7D32',
    },
    '深色': {
        'name': '深色',
        'accent': '#2C6FB8',
        'cover_bg': '#1E1E24',
        'section_fg': '#FFFFFF',
        'title': '#F2F2F2',
        'body': '#E4E4E4',
        'muted': '#9A9A9A',
        'answer': '#FF7B72',
        'solution': '#7EE787',
        'bar_left': '第 {idx} / {total}',
    },
    '大字课堂': {
        'name': '大字课堂',
        'size_stem': 19,
        'size_tex': 27,
        'size_option': 18,
        'size_option_tex': 24,
        'size_answer': 24,
        'size_answer_tex': 30,
        'size_solution': 16,
        'size_solution_tex': 21,
        'body_top': 1.05,
    },
}


def builtin_names():
    return list(BUILTIN.keys())


def list_themes():
    """内置名 + 用户主题文件名（不含扩展名）。返回有顺序的列表。"""
    names = list(BUILTIN.keys())
    if os.path.isdir(THEME_DIR):
        for fn in sorted(os.listdir(THEME_DIR)):
            if fn.lower().endswith('.json'):
                n = fn[:-5]
                if n not in names:
                    names.append(n)
    return names


def _user_path(name):
    return os.path.join(THEME_DIR, name + '.json')


def load_theme(name='默认蓝'):
    """
    name 可以是：
      * None / ''          -> 默认蓝
      * 内置主题名          -> 内置 Theme
      * 用户主题名（无扩展）-> 读 themes/<name>.json
      * 存在的 .json 路径   -> 读该文件
      * Theme 对象          -> 原样返回
    """
    if isinstance(name, Theme):
        return name
    if not name:
        name = '默认蓝'
    if name in BUILTIN:
        return Theme(**BUILTIN[name])
    p = name if name.lower().endswith('.json') else _user_path(name)
    if os.path.exists(p):
        with open(p, 'r', encoding='utf-8') as f:
            return Theme(**json.load(f))
    return Theme(**BUILTIN['默认蓝'])


def save_theme(theme, name=None):
    """把 Theme 落盘到 themes/<name>.json。返回路径。"""
    if isinstance(theme, dict):
        theme = Theme(**theme)
    name = name or theme['name'] or '自定义'
    safe = re.sub(r'[\\/:*?"<>|]', '_', name).strip() or '自定义'
    os.makedirs(THEME_DIR, exist_ok=True)
    p = _user_path(safe)
    with open(p, 'w', encoding='utf-8') as f:
        json.dump(theme.to_dict(), f, ensure_ascii=False, indent=2)
    return p


def delete_theme(name):
    p = _user_path(name)
    if os.path.exists(p):
        os.remove(p)
        return True
    return False


def export_theme(theme, out_path):
    if isinstance(theme, dict):
        theme = Theme(**theme)
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(theme.to_dict(), f, ensure_ascii=False, indent=2)
    return out_path
