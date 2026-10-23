# -*- coding: utf-8 -*-
"""答案/解析分离的单元测试（不依赖 GPU）。"""
import io
import os
import sys

sys.path.insert(0, r'F:\E盘备份\openclaw\physics-bank\app')
os.environ['PHYSICS_BANK_HOME'] = r'F:\E盘备份\openclaw\physics-bank\build\_offtest\t_ans'
os.makedirs(os.environ['PHYSICS_BANK_HOME'], exist_ok=True)
import db
db.init_db()
import ocr_import as O

fails = []


def eq(name, got, want):
    if got != want:
        fails.append('%s: got %r want %r' % (name, got, want))
        print('FAIL %-44s got=%r want=%r' % (name, got, want))
    else:
        print('OK   %s' % name)


def run(lines, drop=True):
    ls = [(t, 1.0, 0.0, float(i), 0.0, float(i)) for i, t in enumerate(lines)]
    st = {'section': '', 'last_num': 0, 'pending_junk': [], 'in_notice': False}
    return O.split_questions(ls, state=st, drop_answers=drop)


print('=== 1. 答案行判定 ===')
for t in ['【答案】B', '【解析】x', '答案：B', '解析：y', '[解析] z', '（答案）A',
          '参考答案与解析', '1-5 BADCB', '1.B 2.A 3.C', '【考点】…', '点评：…']:
    eq('is_answer_line %r' % t, O.is_answer_line(t), True)
for t in ['下列说法正确的是', 'A. 甲', '1. 题干', '物体的质量是5kg']:
    eq('is_answer_line %r' % t, O.is_answer_line(t), False)

print()
print('=== 2. 同行拆分 ===')
eq('split 【答案】', O._split_tail_answer('D. 20 m/s【答案】B【解析】由…'),
   ('D. 20 m/s', '【答案】B【解析】由…'))
eq('split 答案：', O._split_tail_answer('D. 20 m/s 答案：B'),
   ('D. 20 m/s', '答案：B'))
eq('split 无答案', O._split_tail_answer('A. 千克'), ('A. 千克', ''))

print()
print('=== 3. drop_answers=True 时答案不进选项 ===')
qs = run([
    '1. 题干一',
    'A. 甲', 'B. 乙', 'C. 丙', 'D. 丁',
    '【答案】B',
    '【解析】第一题解析',
    '2. 题干二',
    'A. 甲', 'B. 乙',
    '答案：A',
])
eq('qs 数', len(qs), 2)
eq('q1 D 干净', qs[0]['options'][3]['content'], '丁')
eq('q1 answer', qs[0].get('answer'), 'B')
eq('q1 analysis', qs[0].get('analysis'), '第一题解析')
eq('q2 answer', qs[1].get('answer'), 'A')

print()
print('=== 4. 答案区整段（参考答案与解析）===')
qs = run([
    '1. 题干一', 'A. 甲', 'B. 乙', 'C. 丙', 'D. 丁',
    '2. 题干二', 'A. 甲', 'B. 乙', 'C. 丙', 'D. 丁',
    '高三物理卷 — 参考答案',
    '1. 答案：C',
    '解析：第一题详解',
    '2. 答案：B',
    '解析：第二题详解',
])
eq('qs 数（无伪题）', len(qs), 2)
eq('q1 answer', qs[0].get('answer'), 'C')
eq('q2 answer', qs[1].get('answer'), 'B')
eq('q1 analysis', qs[0].get('analysis'), '第一题详解')
eq('q2 analysis', qs[1].get('analysis'), '第二题详解')
eq('题干不含参考答案', '参考答案' in qs[1]['stem'], False)

print()
print('=== 5. 非字母答案（填空/计算）===')
qs = run([
    '1. 电阻 R=____Ω',
    '答案：$R=12 \\Omega$ 解析：由欧姆定律得',
])
eq('answer', qs[0].get('answer'), '$R=12 \\Omega$')
eq('analysis', qs[0].get('analysis'), '解析：由欧姆定律得')

print()
print('=== 6. drop_answers=False（OCR 默认）保持旧行为 ===')
qs = run(['1. 题干', 'A. 甲', 'B. 乙', '【答案】B'], drop=False)
eq('选项 B 带着答案', '【答案】B' in qs[0]['options'][1]['content'], True)

print()
print('=== 7. 题干内联答案 ===')
qs = run(['1. 下列单位是基本单位的是（答案：A）', 'A. 千克', 'B. 牛顿'])
eq('stem 无括号', qs[0]['stem'], '下列单位是基本单位的是')
eq('answer', qs[0].get('answer'), 'A')

print()
print('=== 8. 题干尾巴切分节标题 ===')
qs = run(['1. 公式题 $x=vt$ 多选题（共 2 小题，共 8 分）', 'A. 甲'])
eq('stem', qs[0]['stem'], '公式题 $x=vt$')

print()
print('=' * 60)
if fails:
    print('FAILED %d' % len(fails))
    for f in fails:
        print('  -', f)
    sys.exit(1)
print('ALL PASS')
