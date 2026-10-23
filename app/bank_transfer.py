"""
bank_transfer.py — 题库整体导出 / 导入（.pbk 包）

用途：把整库（题目 + 知识点 + 试卷 + 配图 + 公式库 + PPT 主题）
      打包成一个文件，拿到另一台电脑导入，实现题库迁移。

包格式：**ZIP**（后缀 .pbk，本质还是 zip，方便排查）
    manifest.json     版本、时间、统计、源库信息
    bank.db           数据库快照（已做 WAL checkpoint，保证完整）
    attachments/...   题目配图（原始目录结构）
    themes/*.json     自定义 PPT 主题

设计要点：
1. **导出**：先用 SQLite 备份 API 做一致性快照（比直接拷文件安全，
   能正确处理 WAL 未落盘的数据），再连同附件一起 zip。
2. **导入**：不信任包里的 db 表结构，而是**逐条读出内容用本机的
   db API 重建**，这样：① 新旧版本兼容；② 自动重建检索索引；
   ③ 避免把外来库的坏结构带进来。
3. **ID 冲突**：导入时按**名称**匹配知识点（同名复用），题目一律新建，
   试卷一律新建。不做「覆盖同 id」——那会毁掉本机数据。
"""

import os
import io
import json
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime

import db

PACK_EXT = '.pbk'
MANIFEST = 'manifest.json'
PACK_VERSION = 1


# ---------------------------------------------------------------- 导出

def _snapshot_db(dst_path):
    """用 SQLite 在线备份 API 做一致性快照（含 WAL 中未 checkpoint 的数据）。"""
    src = sqlite3.connect(db.DB_PATH)
    try:
        dst = sqlite3.connect(dst_path)
        try:
            src.backup(dst)
            # 备份完把 WAL 合并掉，保证单文件自洽
            dst.execute('PRAGMA journal_mode=DELETE')
            dst.commit()
        finally:
            dst.close()
    finally:
        src.close()


def _read_all(snapshot_path):
    """从快照里把内容全读出来（用只读连接）。"""
    uri = 'file:%s?mode=ro' % snapshot_path.replace('\\', '/')
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        q = [dict(r) for r in conn.execute('SELECT * FROM questions ORDER BY id')]
        kp = [dict(r) for r in conn.execute('SELECT * FROM knowledge_points ORDER BY id')]
        qkp = [dict(r) for r in conn.execute('SELECT * FROM question_kp')]
        att = [dict(r) for r in conn.execute('SELECT * FROM attachments ORDER BY id')]
        pap = [dict(r) for r in conn.execute('SELECT * FROM papers ORDER BY id')]
        pq = [dict(r) for r in conn.execute('SELECT * FROM paper_questions')]
        st = {}
        try:
            st = {r['key']: r['value'] for r in conn.execute('SELECT * FROM settings')}
        except Exception:
            pass
        return dict(questions=q, kp=kp, question_kp=qkp, attachments=att,
                    papers=pap, paper_questions=pq, settings=st)
    finally:
        conn.close()


def export_bank(out_path, include_attachments=True, progress=None):
    """把整库导出为 .pbk 包。返回 (out_path, stats)。"""
    def say(msg):
        if progress:
            progress(msg)

    if not os.path.isfile(db.DB_PATH):
        raise RuntimeError('找不到题库文件：%s' % db.DB_PATH)

    tmp = tempfile.mkdtemp(prefix='pbk_exp_')
    try:
        snap = os.path.join(tmp, 'bank.db')
        say('正在生成数据库快照...')
        _snapshot_db(snap)

        data = _read_all(snap)
        nq = len(data['questions'])
        nkp = len(data['kp'])
        npaper = len(data['papers'])
        natt_rows = len(data['attachments'])
        say('题库：%d 题 / %d 知识点 / %d 卷 / %d 张配图记录' % (nq, nkp, npaper, natt_rows))

        # 附件：按 rel_path 收集实际文件
        # 注意：rel_path 是相对 DATA_DIR 的，本身已含 'attachments/' 前缀
        # （由 db.add_attachment 写入），所以包里直接用 rel_path 做路径，
        # 绝不能再拼一层 'attachments/'，否则会变成 attachments/attachments/...
        att_files = []
        if include_attachments:
            base = db.DATA_DIR
            seen = set()
            for a in data['attachments']:
                rel = (a.get('rel_path') or '').replace('\\', '/')
                if not rel or rel in seen:
                    continue
                seen.add(rel)
                p = os.path.join(base, rel.replace('/', os.sep))
                if os.path.isfile(p):
                    att_files.append((p, rel))
            say('收集配图 %d 个文件' % len(att_files))

        # 主题
        theme_files = []
        import ppt_theme as _pt
        tdir = getattr(_pt, 'THEME_DIR', None)
        if tdir and os.path.isdir(tdir):
            for fn in os.listdir(tdir):
                if fn.lower().endswith('.json'):
                    theme_files.append((os.path.join(tdir, fn), 'themes/' + fn))
            say('收集自定义主题 %d 个' % len(theme_files))

        manifest = {
            'format': 'physics-bank',
            'pack_version': PACK_VERSION,
            'created_at': datetime.now().isoformat(timespec='seconds'),
            'app': '高中物理题库管理（离线版）',
            'counts': {
                'questions': nq, 'knowledge_points': nkp, 'papers': npaper,
                'attachments': len(att_files),
                'themes': len(theme_files),
                'formula_lib': len(json.loads(data['settings'].get('formula_library') or '[]'))
                    if data['settings'].get('formula_library') else 0,
            },
            'content': data,
        }

        say('正在写入压缩包...')
        os.makedirs(os.path.dirname(os.path.abspath(out_path)) or '.', exist_ok=True)
        with zipfile.ZipFile(out_path, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            z.write(snap, 'bank.db')
            if include_attachments:
                for src, arc in att_files:
                    try:
                        z.write(src, arc)
                    except Exception:
                        pass
            for src, arc in theme_files:
                try:
                    z.write(src, arc)
                except Exception:
                    pass
            z.writestr(MANIFEST, json.dumps(manifest, ensure_ascii=False, indent=2))

        size = os.path.getsize(out_path)
        say('导出完成：%s' % out_path)
        return out_path, {
            'questions': nq, 'kp': nkp, 'papers': npaper,
            'attachments': len(att_files), 'themes': len(theme_files),
            'size': size,
        }
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 读取包信息

def peek_pack(path):
    """只读包信息，不导入。返回 manifest dict。"""
    with zipfile.ZipFile(path, 'r') as z:
        names = z.namelist()
        if MANIFEST in names:
            return json.loads(z.read(MANIFEST).decode('utf-8'))
        # 老式/裸包：只有 bank.db
        if 'bank.db' in names:
            return {'format': 'physics-bank', 'pack_version': 0,
                    'created_at': '', 'counts': {}, 'content': None}
        raise RuntimeError('不是有效的题库包（缺少 manifest.json 和 bank.db）')


# ---------------------------------------------------------------- 导入

def import_bank(path, mode='merge', progress=None, attach_root=None):
    """从 .pbk 包导入。

    mode:
      'merge'    —— 合并（默认）：知识点同名复用，题目/试卷一律新建
      'replace'  —— 清空本机现有内容后再导入（危险）

    返回 stats。
    """
    def say(msg):
        if progress:
            progress(msg)

    mf = peek_pack(path)
    content = mf.get('content')
    if not content and 'bank.db' in zipfile.ZipFile(path).namelist():
        # 没有 manifest.content 的老包：解开 db 现读
        tmp = tempfile.mkdtemp(prefix='pbk_imp_')
        with zipfile.ZipFile(path) as z:
            z.extract('bank.db', tmp)
        content = _read_all(os.path.join(tmp, 'bank.db'))
        shutil.rmtree(tmp, ignore_errors=True)
    if not content:
        raise RuntimeError('包内容为空或损坏')

    src_q = content.get('questions', [])
    src_kp = content.get('kp', [])
    src_qkp = content.get('question_kp', [])
    src_att = content.get('attachments', [])
    src_pap = content.get('papers', [])
    src_pq = content.get('paper_questions', [])
    src_settings = content.get('settings', {})

    say('包内：%d 题 / %d 知识点 / %d 卷' % (len(src_q), len(src_kp), len(src_pap)))

    if mode == 'replace':
        say('清空本机现有题库内容...')
        _wipe_local()

    # ---- 1. 知识点：按名称匹配，同名复用 ----
    conn = db.get_conn()
    local_kp = {}
    for r in conn.execute('SELECT id, name FROM knowledge_points'):
        local_kp[r['name']] = r['id']
    conn.close()

    kp_map = {}      # 旧 kp_id -> 本机 kp_id
    src_kp_sorted = sorted(src_kp, key=lambda r: (r.get('parent_id') or 0, r.get('id') or 0))
    created_kp = 0
    for r in src_kp_sorted:
        old_id = r.get('id')
        name = (r.get('name') or '').strip()
        if not name:
            continue
        if name in local_kp:
            kp_map[old_id] = local_kp[name]
        else:
            new_id = db.add_kp(name)
            local_kp[name] = new_id
            kp_map[old_id] = new_id
            created_kp += 1
    say('知识点：复用 %d，新建 %d' % (len(kp_map) - created_kp, created_kp))

    # ---- 2. 题目 ----
    kp_by_new = {}   # new_qid -> [new_kp_id]
    for rel in src_qkp:
        oq, okp = rel.get('question_id'), rel.get('kp_id')
        if oq is None or okp is None:
            continue
        nk = kp_map.get(okp)
        if nk:
            kp_by_new.setdefault(oq, []).append(nk)

    q_map = {}       # 旧 qid -> 新 qid
    created_q = 0
    for r in src_q:
        old_id = r.get('id')
        try:
            nq = db.add_question(
                stem=r.get('stem') or '',
                answer=r.get('answer') or '',
                solution=r.get('solution') or '',
                question_type=r.get('question_type') or 'choice_single',
                difficulty=r.get('difficulty') or 3,
                source=r.get('source') or '',
                source_page=r.get('source_page'),
                grade=r.get('grade') or '',
                note=r.get('note') or '',
                kp_ids=kp_by_new.get(old_id, []))
        except Exception as e:
            say('  跳过一题（%s）：%s' % (e, (r.get('stem') or '')[:40]))
            continue
        q_map[old_id] = nq
        created_q += 1
    say('题目：新建 %d' % created_q)

    # 重建检索索引（add_question 已逐步建，这里兜底全量）
    try:
        db.rebuild_search_index()
    except Exception:
        pass

    # ---- 3. 附件 ----
    # 包内路径形如 attachments/q_<旧id>/<文件>（见导出侧说明）。
    n_att = 0
    if attach_root:
        with zipfile.ZipFile(path) as z:
            names = [n for n in z.namelist()
                     if n.startswith('attachments/') and not n.endswith('/')]
            for name in names:
                parts = name.split('/')
                # attachments / q_<id> / 文件名  -> 至少 3 段
                if len(parts) < 3:
                    continue
                # 从后往前找 q_<数字> 那一段，兼容历史上多包一层的旧包
                old_qid = None
                folder_idx = None
                for i, seg in enumerate(parts[:-1]):
                    if seg.startswith('q_') and seg[2:].isdigit():
                        old_qid = int(seg[2:])
                        folder_idx = i
                        break
                if old_qid is None:
                    continue
                new_qid = q_map.get(old_qid)
                if not new_qid:
                    continue
                try:
                    blob = z.read(name)
                except Exception:
                    continue
                qdir = os.path.join(attach_root, 'q_%d' % new_qid)
                os.makedirs(qdir, exist_ok=True)
                dst = os.path.join(qdir, parts[-1])
                try:
                    if not os.path.exists(dst):
                        with open(dst, 'wb') as f:
                            f.write(blob)
                    rel = os.path.relpath(dst, db.DATA_DIR).replace('\\', '/')
                    # 找原始 role（用去掉最外层 attachments/ 后的相对路径比对）
                    inner = '/'.join(parts[folder_idx:])
                    role = 'stem_figure'
                    for a in src_att:
                        if (a.get('rel_path') or '').replace('\\', '/').endswith(inner):
                            role = a.get('role') or role
                            break
                    conn = db.get_conn()
                    conn.execute(
                        'INSERT INTO attachments(question_id, rel_path, file_type, role, sha256) '
                        'VALUES (?,?,?,?,?)',
                        (new_qid, rel, 'image', role, ''))
                    conn.commit()
                    conn.close()
                    n_att += 1
                except Exception:
                    pass
    say('配图：导入 %d 个' % n_att)

    # ---- 4. 试卷 ----
    created_p = 0
    for p in src_pap:
        old_pid = p.get('id')
        try:
            np = db.add_paper(p.get('title') or '导入的试卷',
                              p.get('description') or '',
                              json.loads(p.get('config_json') or '{}'))
        except Exception:
            continue
        created_p += 1
        for rel in sorted([r for r in src_pq if r.get('paper_id') == old_pid],
                          key=lambda r: r.get('sort_order') or 0):
            nq = q_map.get(rel.get('question_id'))
            if nq:
                try:
                    db.add_paper_question(np, nq, rel.get('sort_order') or 0,
                                          rel.get('score') or 0)
                except Exception:
                    pass
    say('试卷：新建 %d' % created_p)

    # ---- 5. 公式库合并 ----
    n_f = 0
    if src_settings.get('formula_library'):
        try:
            incoming = json.loads(src_settings['formula_library']) or []
            cur = db.list_formula_lib()
            have = {i.get('latex') for i in cur}
            for it in incoming:
                if it.get('latex') and it['latex'] not in have:
                    if db.add_formula_lib(it.get('name') or '未命名', it['latex']):
                        have.add(it['latex'])
                        n_f += 1
        except Exception:
            pass
    if n_f:
        say('公式库：新增 %d 条' % n_f)

    # ---- 6. 主题 ----
    n_t = 0
    try:
        import ppt_theme as _pt
        tdir = getattr(_pt, 'THEME_DIR', None)
        if tdir:
            os.makedirs(tdir, exist_ok=True)
            with zipfile.ZipFile(path) as z:
                for name in z.namelist():
                    if name.startswith('themes/') and name.endswith('.json'):
                        fn = os.path.basename(name)
                        dst = os.path.join(tdir, fn)
                        if not os.path.exists(dst):
                            with open(dst, 'wb') as f:
                                f.write(z.read(name))
                            n_t += 1
    except Exception:
        pass
    if n_t:
        say('主题：导入 %d 个' % n_t)

    return {'questions': created_q, 'kp_new': created_kp,
            'papers': created_p, 'attachments': n_att,
            'formulas': n_f, 'themes': n_t}


def _wipe_local():
    """清空本机题目/试卷/知识点（危险，仅供 replace 模式）。"""
    conn = db.get_conn()
    try:
        conn.execute('DELETE FROM paper_questions')
        conn.execute('DELETE FROM papers')
        conn.execute('DELETE FROM question_kp')
        conn.execute('DELETE FROM attachments')
        conn.execute('DELETE FROM search_index')
        conn.execute('DELETE FROM questions')
        conn.execute('DELETE FROM knowledge_points')
        conn.commit()
    finally:
        conn.close()
