# -*- coding: utf-8 -*-
"""OCR 增强回归测试（不需要 GPU/模型，纯逻辑）。"""
import io
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, r'F:\E盘备份\openclaw\physics-bank\app')

import ocr_import as O
import layout_enhance as LE

fails = []


def check(name, got, want):
    ok = got == want
    if not ok:
        fails.append('%s: got %r want %r' % (name, got, want))
    print('%-48s %s' % (name, 'OK' if ok else 'FAIL got=%r want=%r' % (got, want)))


print('=== 1. 单位识别（纯单位不是公式）===')
for t in ['m/s²', 'm/s2', 'N/kg', 'kg', 'm/s', 'kg·m/s²', '(m/s²)', 'm/s2。']:
    check('is_pure_unit %r' % t, LE.is_pure_unit(t), True)
for t in ['3v', 'v0', 'x=10t', 'n2-1', '19.6m', 'ab']:
    check('is_pure_unit %r' % t, LE.is_pure_unit(t), False)

print()
print('=== 2. 纯式识别（可整行替换）===')
for t in ['3v', 'n2-1', 'g=9.8m/s2', 'x=10t-1/4', '1.5vt']:
    check('is_pure_expr %r' % t, LE.is_pure_expr(t), True)
for t in ['前时间通过的位移', 'D. 平均速度大', 'A. a 的原长比 b 的长']:
    check('is_pure_expr %r' % t, LE.is_pure_expr(t), False)

print()
print('=== 3. looks_like_math 不能把单位/中文当选 ===')
for t in ['m/s²', 'm/s2', 'N/kg']:
    check('looks_like_math %r' % t, LE.looks_like_math(t), False)

print()
print('=== 4. 碎片判定 ===')
frag_true = ['1', '6', '2', 'n', 't', 'n2-1', 'E', 'B', 'D', '10', 'x/m', 'v0', '51', '4-3', 'vB', '2n', 'U2']
frag_false = ['m', 'm/s²', 'm/s2', 'N/kg', 'A.', 'D.', 'kg', 'm/s', 'kg·m/s²',
              '8.65', 'cm;', '甲', '下列说法', '图甲']
for t in frag_true:
    check('_is_frag %r' % t, O._is_frag(t), True)
for t in frag_false:
    check('_is_frag %r' % t, O._is_frag(t), False)

print()
print('=== 5. 碎片重建 ===')
cases = [
    ('A. 1 6m/s²', 'A. $\\frac{1}{6}$ m/s²'),
    ('A. 1 6 m/s²', 'A. $\\frac{1}{6}$ m/s²'),
    ('B. 3 1m/s²', 'B. $\\frac{3}{1}$ m/s²'),
    ('C. 2 3m/s²', 'C. $\\frac{2}{3}$ m/s²'),
    ('C. 2 3 t', 'C. $\\frac{2}{3}$ t'),
]
for src, want in cases:
    check('rebuild %r' % src, O.rebuild_frag_text(src), want)
# 不能误合
for src in ['C. 19.6m', 'A. 平均速度大', 'D. n2-1 n 2',
            'C. 前 2时间通过的位移比后 2 t时间通过的位移少 1.5vt',
            'A. 3(n+2)m', 'B. 4.9(2n+1)m', 'D. 4-3m/s²']:
    check('rebuild keep %r' % src, O.rebuild_frag_text(src), src)

print()
print('=== 6. 页眉页脚 ===')
for t in ['高一物理试题', '第2页', '共5页', '第 3 页 共 5 页', 'Page 4', '青岛二中 高一物理试题']:
    check('_is_junk %r' % t, O._is_junk(t), True)
for t in ['下列说法正确的是', '物体的质量是5kg', '下列关于加速度的说法正确的是']:
    check('_is_junk %r' % t, O._is_junk(t), False)

print()
print('=== 7. _to_plain_math ===')
check('n2-1', O._to_plain_math('n2-1'), 'n_{2}-1')
check('v0', O._to_plain_math('v0'), 'v_{0}')
check('m/s2', O._to_plain_math('m/s2'), 'm/s^{2}')
check('n2-1 sup', O._to_plain_math('n2-1', True), 'n^{2}-1')

print()
print('=' * 60)
if fails:
    print('FAILED %d:' % len(fails))
    for f in fails:
        print('  -', f)
    sys.exit(1)
print('ALL PASS')
