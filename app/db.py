"""
db.py — 数据层
SQLite 建表 / 连接管理 / 基础 CRUD
"""
import os
import re
import sqlite3
import json
from datetime import datetime

# 项目根目录
# ★ 兼容打包：打包后本文件在 <exe目录>\_internal\app\db.py，
#   若直接用 dirname(dirname(__file__)) 会把 ROOT 算成 exe 目录，
#   然后在 exe 旁建出 data\ → 下次启动被判成「便携模式」→ 用户以为题丢了。
#   所以：若 launcher 已经指定了 PHYSICS_BANK_HOME，优先用它。
_forced = os.environ.get('PHYSICS_BANK_HOME')
if _forced:
    ROOT = os.path.abspath(_forced)
else:
    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, 'data')
ATTACH_DIR = os.path.join(DATA_DIR, 'attachments')
DB_PATH = os.path.join(DATA_DIR, 'bank.db')

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(ATTACH_DIR, exist_ok=True)


def export_dir():
    """导出目录。源码运行时是 <项目根>\\exports；
    打包后 launcher 会把 ROOT 改到用户可写目录，这里自动跟随。"""
    d = os.path.join(ROOT, 'exports')
    os.makedirs(d, exist_ok=True)
    return d


# ---------------- 选项行解析（选项已并入题干文本） ----------------
# 约定：选项直接写在题干里，每行形如 "A. xxx" / "A、xxx" / "A）xxx" / "(A) xxx"
# 该模块只负责「认出哪些行是选项」以及「答案字段里的 A/B/C/D」，
# 供 列表展示 / Word / PDF / PPT 四处复用，避免各写一份。

import re as _re

# A. / A、/ A）/ A) / （A）/ (A) / A： / A:   （半角句点要求后接空格或非空内容）
# 带括号的形式后不要求分隔符：(A) xxx；也用（A）xxx
_OPT_RE = _re.compile(
    r'^\s*(?:\(\s*([A-Ha-h])\s*\)|（\s*([A-Ha-h])\s*）|([A-Ha-h])\s*[\.、．:：，,])\s*(\S.*)?$'
)


def is_option_line(line):
    """判断一行是否是选项行。返回 (label, content) 或 None。

    只认「选项字母 + 分隔符」开头的行，避免把 'a=3m/s^2' 这类误判。
    """
    if not line:
        return None
    m = _OPT_RE.match(line)
    if not m:
        return None
    label = (m.group(1) or m.group(2) or m.group(3)).upper()
    content = (m.group(4) or '').strip()
    return label, content


def split_stem_options(stem):
    """把题干按选项行切成 (lead, [(label, content), ...])。

    - lead：选项之前的题干正文（无选项则等于原文）
    - options：识别出的选项列表
    连续多行内容会并入前一个选项（选项内容换行的情形）。
    """
    lines = (stem or '').splitlines()
    lead, opts = [], []
    for ln in lines:
        hit = is_option_line(ln)
        if hit:
            opts.append([hit[0], hit[1]])
        elif opts and ln.strip():
            # 选了选项之后又出现的普通行 —— 视为上一个选项的续行
            opts[-1][1] = (opts[-1][1] + ' ' + ln.strip()).strip()
        else:
            lead.append(ln)
    return '\n'.join(lead).strip(), [(a, b) for a, b in opts]


def parse_answer_letters(answer, labels=None):
    """从答案字段里解析出正确选项字母集合。

    answer 可能是 'A' / 'AB' / 'A、C' / 'A,C' / 'ACD' / '答案是 B'。
    labels 给出「本次实际存在的选项」，用于排除答案里的无关字母。
    返回 set，如 {'A','C'}。
    """
    if not answer:
        return set()
    pool = {str(x).upper() for x in labels} if labels else None
    out = set()
    # 优先认「选项字母成组出现」的片段
    for m in _re.finditer(r'[A-Ha-h]+', answer):
        seg = m.group(0).upper()
        if len(seg) == 1:
            out.add(seg)
        else:
            out.update(seg)
    if pool is not None:
        out &= pool
    return out


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS questions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    stem            TEXT NOT NULL,
    answer          TEXT DEFAULT '',
    solution        TEXT DEFAULT '',
    question_type   TEXT NOT NULL DEFAULT 'choice_single',
    difficulty      INTEGER DEFAULT 3,
    source          TEXT DEFAULT '',
    source_page     INTEGER,
    grade           TEXT DEFAULT '',
    note            TEXT DEFAULT '',
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS options (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id     INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
    label           TEXT NOT NULL,
    content         TEXT NOT NULL DEFAULT '',
    is_correct      INTEGER DEFAULT 0,
    sort_order      INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS knowledge_points (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE,
    parent_id       INTEGER REFERENCES knowledge_points(id) ON DELETE SET NULL,
    sort_order      INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS question_kp (
    question_id     INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
    kp_id           INTEGER NOT NULL REFERENCES knowledge_points(id) ON DELETE CASCADE,
    PRIMARY KEY (question_id, kp_id)
);

CREATE TABLE IF NOT EXISTS attachments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id     INTEGER REFERENCES questions(id) ON DELETE CASCADE,
    rel_path        TEXT NOT NULL,
    file_type       TEXT DEFAULT 'image',
    role            TEXT DEFAULT 'stem_figure',
    sort_order      INTEGER DEFAULT 0,
    sha256          TEXT DEFAULT '',
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
/*
 * 图片锚点列（2026-09-29 新增，由 _migrate_attachments 补给老库）：
 *   field         'stem' | 'solution' | 'answer'   图属于哪个文本框
 *   anchor_offset 该字段纯文本里的字符下标（0=开头）；
 *                 -1 = 无锚点，跟在全文末尾（旧数据的语义）
 *   width_pct     图片占正文栏宽百分比，0 = 自动按原尺寸等比缩
 *
 * 为什么不写进上面 CREATE TABLE：老库表已存在，
 * CREATE TABLE IF NOT EXISTS 不会补列，必须走 ALTER TABLE。
 */

CREATE TABLE IF NOT EXISTS tags (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE,
    color           TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS question_tag (
    question_id     INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
    tag_id          INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (question_id, tag_id)
);

CREATE TABLE IF NOT EXISTS papers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    title           TEXT NOT NULL,
    description     TEXT DEFAULT '',
    config_json     TEXT DEFAULT '{}',
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS paper_questions (
    paper_id        INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    question_id     INTEGER NOT NULL REFERENCES questions(id),
    sort_order      INTEGER DEFAULT 0,
    score           REAL DEFAULT 0,
    PRIMARY KEY (paper_id, question_id)
);

-- 全文检索（自建按字索引，中文友好）
-- 不用 FTS5 分词器：unicode61 拿中文连写没辙，trigram 又要求 3 字以上
CREATE TABLE IF NOT EXISTS search_index (
    question_id     INTEGER PRIMARY KEY REFERENCES questions(id) ON DELETE CASCADE,
    chars           TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_search_chars ON search_index(chars);

-- 简单的键值设置表（当前主题名、上次导出目录等）
CREATE TABLE IF NOT EXISTS settings (
    key             TEXT PRIMARY KEY,
    value           TEXT
);

-- 触发器保持索引同步
-- 注意：索引内容由 rebuild_search_index / _index_one 统一生成，
-- 涵盖 题干+答案+解析+来源+备注+**所有选项文本**+**知识点名**。
-- （早期版本只索引 questions 表字段，导致按选项或知识点搜不到）
CREATE TRIGGER IF NOT EXISTS questions_si AFTER INSERT ON questions BEGIN
    INSERT OR REPLACE INTO search_index(question_id, chars)
    VALUES (new.id, COALESCE(new.stem,'') || ' ' || COALESCE(new.answer,'')
            || ' ' || COALESCE(new.solution,'')
            || ' ' || COALESCE(new.source,'') || ' ' || COALESCE(new.note,''));
END;

CREATE TRIGGER IF NOT EXISTS questions_sd AFTER DELETE ON questions BEGIN
    DELETE FROM search_index WHERE question_id = old.id;
END;

CREATE TRIGGER IF NOT EXISTS questions_su AFTER UPDATE ON questions BEGIN
    INSERT OR REPLACE INTO search_index(question_id, chars)
    VALUES (new.id, COALESCE(new.stem,'') || ' ' || COALESCE(new.answer,'')
            || ' ' || COALESCE(new.solution,'')
            || ' ' || COALESCE(new.source,'') || ' ' || COALESCE(new.note,''));
END;

-- 注意：选项/知识点变动后由应用层 _refresh_index(qid) 重算，
-- 不靠触发器（触发器在 INSERT 中途会因 questions 行尚未可见而算出 NULL）。

CREATE INDEX IF NOT EXISTS idx_q_type ON questions(question_type);
CREATE INDEX IF NOT EXISTS idx_q_diff ON questions(difficulty);
CREATE INDEX IF NOT EXISTS idx_q_source ON questions(source);
CREATE INDEX IF NOT EXISTS idx_att_q ON attachments(question_id);
CREATE INDEX IF NOT EXISTS idx_opt_q ON options(question_id);
"""

# 预置知识点（高中物理主干）
SEED_KP = [
    ('运动的描述', None),
    ('匀变速直线运动', None),
    ('相互作用——力', None),
    ('牛顿运动定律', None),
    ('曲线运动', None),
    ('万有引力与航天', None),
    ('机械能守恒', None),
    ('动量守恒', None),
    ('静电场', None),
    ('恒定电流', None),
    ('磁场', None),
    ('电磁感应', None),
    ('交变电流', None),
    ('机械振动与机械波', None),
    ('光学', None),
    ('热学', None),
    ('近代物理', None),
]


def get_conn():
    # timeout：另一个连接持有写锁时，最多等 10s 再失败（而不是立即报 locked）
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    # journal_mode 是库级持久设置，只在首次写一次，之后无需每次重设
    # （每次重设会开一个写事务，WAL 下易与主连接抢锁）
    if not os.path.exists(DB_PATH + '-wal'):
        conn.execute('PRAGMA journal_mode = WAL')
    conn.execute('PRAGMA busy_timeout = 10000')
    return conn


def get_setting(key, default=None):
    """读一条设置；不存在返回 default。"""
    conn = get_conn()
    try:
        r = conn.execute('SELECT value FROM settings WHERE key=?',
                         (key,)).fetchone()
        return r['value'] if r else default
    except sqlite3.OperationalError:
        return default        # 老库未建表
    finally:
        conn.close()


def set_setting(key, value):
    """写一条设置（upsert）。"""
    conn = get_conn()
    try:
        conn.execute(
            'INSERT INTO settings(key, value) VALUES (?,?)\n'
            'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
            (key, '' if value is None else str(value)))
        conn.commit()
    finally:
        conn.close()


# ======================= 常用公式库 =======================
# 存在 settings 表里（key='formula_library'），值是一个 JSON 数组：
#   [{'name': '牛顿第二定律', 'latex': 'F = ma'}, ...]
# 用 settings 而不是单开一张表：条目数少、无查询需求，简单够用。

FORMULA_LIB_KEY = 'formula_library'


def list_formula_lib():
    """读常用公式库。返回 [{'name','latex'}...]，损坏时返回空表。"""
    import json
    raw = get_setting(FORMULA_LIB_KEY, '[]')
    try:
        data = json.loads(raw) if raw else []
    except (ValueError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    out = []
    for it in data:
        if isinstance(it, dict) and it.get('latex'):
            out.append({'name': (it.get('name') or '').strip(),
                        'latex': it['latex']})
    return out


def save_formula_lib(items):
    """整表写回。items = [{'name','latex'}...]，跳过空 latex。"""
    import json
    clean = [{'name': (it.get('name') or '').strip(), 'latex': (it.get('latex') or '').strip()}
             for it in (items or []) if (it.get('latex') or '').strip()]
    set_setting(FORMULA_LIB_KEY, json.dumps(clean, ensure_ascii=False))
    return clean


def add_formula_lib(name, latex):
    """追加一条（同 latex 已存在则不重复加）。返回 True=新增，False=已存在。"""
    latex = (latex or '').strip()
    if not latex:
        return False
    items = list_formula_lib()
    if any(it['latex'] == latex for it in items):
        return False
    items.append({'name': (name or '').strip(), 'latex': latex})
    save_formula_lib(items)
    return True


def delete_formula_lib(latex):
    """按 latex 删除一条。返回是否删到。"""
    items = list_formula_lib()
    kept = [it for it in items if it['latex'] != (latex or '').strip()]
    if len(kept) == len(items):
        return False
    save_formula_lib(kept)
    return True


def rename_formula_lib(latex, new_name):
    """改某条公式的名字（按 latex 定位）。latex 不存在返回 False。

    重名允许 —— 只当标签用，latex 才是唯一键。
    """
    latex = (latex or '').strip()
    items = list_formula_lib()
    hit = False
    for it in items:
        if it['latex'] == latex:
            it['name'] = (new_name or '').strip()
            hit = True
    if not hit:
        return False
    save_formula_lib(items)
    return True


def get_formula_lib_names():
    """已用过的公式名集合（去重，非空），给界面做补全提示用。"""
    return sorted({it['name'] for it in list_formula_lib() if it['name']})


# ======================= 判断题答案 =======================
# 库里统一存 '√' / '×'。用户习惯写法很多，一律归一化。
_JUDG_T = {'√', '✓', '✔', '对', '正确', '是', 't', 'true', 'y', 'yes', '1'}
_JUDG_F = {'×', '✗', '✘', 'x', '错', '错误', '否', 'f', 'false', 'n', 'no', '0'}


def normalize_judgment_answer(s):
    """把常见写法归一到 '√' / '×'。无法识别返回 None。

    认得：√ ✓ ✔ 对 正确 是 T/True/是 -> '√'
          × ✗ ✘ X 错 错误 否 F/False  -> '×'
    注意：小写 x 当“叉”，但只有在不是字母选项语境下才到这里（判断题専用）。
    """
    if s is None:
        return None
    k = str(s).strip().lower()
    if not k:
        return None
    if k in _JUDG_T:
        return '√'
    if k in _JUDG_F:
        return '×'
    return None


def init_db():
    conn = get_conn()
    conn.executescript(SCHEMA)
    _migrate_attachments(conn)
    # 预置知识点
    cur = conn.execute('SELECT COUNT(*) AS c FROM knowledge_points')
    if cur.fetchone()['c'] == 0:
        for i, (name, parent) in enumerate(SEED_KP):
            conn.execute(
                'INSERT INTO knowledge_points(name, parent_id, sort_order) VALUES (?,?,?)',
                (name, parent, i)
            )
    conn.commit()
    conn.close()
    # 修复/补齐索引（涵盖选项 + 知识点）
    try:
        rebuild_search_index()
    except Exception:
        pass
    return True


def _migrate_attachments(conn):
    """给老库的 attachments 补上图片锚点列。

    幂等：先 PRAGMA 看列在不在，在就跳过。
    ★ 为了能真正跑在「老库升级」场景，这里绝不 DROP / 重建表。
    """
    have = {r[1] for r in conn.execute('PRAGMA table_info(attachments)')}
    if not have:
        return                                   # 表还不存在
    added = []
    for col, ddl in (
        ('field', "ALTER TABLE attachments ADD COLUMN field TEXT DEFAULT 'stem'"),
        ('anchor_offset', 'ALTER TABLE attachments ADD COLUMN anchor_offset INTEGER DEFAULT -1'),
        ('width_pct', 'ALTER TABLE attachments ADD COLUMN width_pct INTEGER DEFAULT 0'),
    ):
        if col not in have:
            try:
                conn.execute(ddl)
                added.append(col)
            except sqlite3.OperationalError:
                pass
    if added:
        conn.commit()
    return added


def rebuild_search_index(verbose=False):
    """
    全量重建中文检索索引。
    覆盖：题干 / 答案 / 解析 / 来源 / 备注 / 所有选项内容 / 知识点名。
    单条 SQL，避免 N+1。返回重建的题数。
    """
    conn = get_conn()
    conn.execute('DELETE FROM search_index')
    conn.execute("""
        INSERT INTO search_index(question_id, chars)
        SELECT q.id,
               q.stem || ' ' || COALESCE(q.answer,'') || ' '
               || COALESCE(q.solution,'') || ' ' || COALESCE(q.source,'') || ' '
               || COALESCE(q.note,'') || ' '
               || COALESCE((SELECT GROUP_CONCAT(o.content, ' ') FROM options o
                            WHERE o.question_id = q.id), '') || ' '
               || COALESCE((SELECT GROUP_CONCAT(k.name, ' ')
                            FROM knowledge_points k
                            JOIN question_kp qk ON k.id = qk.kp_id
                            WHERE qk.question_id = q.id), '')
        FROM questions q
    """)
    conn.commit()
    n = conn.execute('SELECT COUNT(*) AS c FROM search_index').fetchone()['c']
    conn.close()
    if verbose:
        print(f'search_index rebuilt: {n} rows')
    return n


def _refresh_index(qid):
    """重算某一道题的索引（选项/知识点变动后调用）"""
    conn = get_conn()
    conn.execute("""
        INSERT OR REPLACE INTO search_index(question_id, chars)
        SELECT q.id,
               COALESCE(q.stem,'') || ' ' || COALESCE(q.answer,'') || ' '
               || COALESCE(q.solution,'') || ' ' || COALESCE(q.source,'') || ' '
               || COALESCE(q.note,'') || ' '
               || COALESCE((SELECT GROUP_CONCAT(o.content, ' ') FROM options o
                            WHERE o.question_id = q.id), '') || ' '
               || COALESCE((SELECT GROUP_CONCAT(k.name, ' ')
                            FROM knowledge_points k
                            JOIN question_kp qk ON k.id = qk.kp_id
                            WHERE qk.question_id = q.id), '')
        FROM questions q WHERE q.id = ?
    """, (qid,))
    conn.commit()
    conn.close()


# ---------------- Questions ----------------

def add_question(stem, answer='', solution='', question_type='choice_single',
                 difficulty=3, source='', source_page=None, grade='', note='',
                 options=None, kp_ids=None):
    """options: [{'label':'A','content':'...','is_correct':0/1}, ...]"""
    conn = get_conn()
    cur = conn.execute(
        """INSERT INTO questions(stem, answer, solution, question_type,
             difficulty, source, source_page, grade, note)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (stem, answer, solution, question_type, difficulty,
         source, source_page, grade, note)
    )
    qid = cur.lastrowid
    if options:
        for i, o in enumerate(options):
            conn.execute(
                """INSERT INTO options(question_id, label, content, is_correct, sort_order)
                   VALUES (?,?,?,?,?)""",
                (qid, o.get('label', ''), o.get('content', ''),
                 int(o.get('is_correct', 0)), i)
            )
    if kp_ids:
        for kp in kp_ids:
            conn.execute(
                'INSERT OR IGNORE INTO question_kp(question_id, kp_id) VALUES (?,?)',
                (qid, kp)
            )
    conn.commit()
    conn.close()
    if options or kp_ids:
        _refresh_index(qid)   # 把选项/知识点文本纳入检索
    return qid


def update_question(qid, **fields):
    if not fields:
        return
    fields['updated_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    cols = ', '.join(f'{k}=?' for k in fields)
    conn = get_conn()
    try:
        with conn:      # 成功自动 commit；异常自动 rollback
            conn.execute(f'UPDATE questions SET {cols} WHERE id=?',
                         list(fields.values()) + [qid])
    finally:
        conn.close()


def question_paper_refs(qid):
    """这道题被哪些试卷引用。删题前提示用。"""
    conn = get_conn()
    try:
        return [dict(r) for r in conn.execute(
            """SELECT p.id AS paper_id, p.title FROM paper_questions pq
               JOIN papers p ON p.id=pq.paper_id
               WHERE pq.question_id=? ORDER BY p.id""", (qid,))]
    finally:
        conn.close()


def delete_question(qid, unlink_from_papers=True):
    """删除一道题。

    🔴 坑：paper_questions.question_id 是 ON DELETE NO ACTION，
    题目只要被任何试卷引用，直接 DELETE 会抛 IntegrityError
    （以前异常被 Qt 槽吃掉，界面看着没报错，但题目根本没删掉）。
    所以先把卷-题关联解掉，再删题。

    unlink_from_papers=False 时保留关联、由调用方自行处理（仅测试用）。
    返回 (ok, msg)。
    """
    conn = get_conn()
    try:
        if unlink_from_papers:
            conn.execute(
                'DELETE FROM paper_questions WHERE question_id=?', (qid,))
        cur = conn.execute('DELETE FROM questions WHERE id=?', (qid,))
        n = cur.rowcount
        # 手工清关联表：search_index/options/attachments/question_kp
        # 有 CASCADE，但显式清一遍更保险（旧库可能没建外键）
        conn.execute('DELETE FROM search_index WHERE question_id=?', (qid,))
        conn.execute('DELETE FROM options WHERE question_id=?', (qid,))
        conn.execute('DELETE FROM attachments WHERE question_id=?', (qid,))
        conn.execute('DELETE FROM question_kp WHERE question_id=?', (qid,))
        conn.commit()
        if n == 0:
            return False, f'库里没有 id={qid} 的题目'
        return True, ''
    except sqlite3.Error as e:
        conn.rollback()
        return False, f'{type(e).__name__}: {e}'
    finally:
        conn.close()


def get_question(qid):
    conn = get_conn()
    q = conn.execute('SELECT * FROM questions WHERE id=?', (qid,)).fetchone()
    if not q:
        conn.close()
        return None
    q = dict(q)
    q['options'] = [dict(r) for r in conn.execute(
        'SELECT * FROM options WHERE question_id=? ORDER BY sort_order', (qid,))]
    q['attachments'] = [dict(r) for r in conn.execute(
        'SELECT * FROM attachments WHERE question_id=? ORDER BY sort_order, id', (qid,))]
    q['kp'] = [dict(r) for r in conn.execute(
        """SELECT k.* FROM knowledge_points k
           JOIN question_kp qk ON qk.kp_id=k.id WHERE qk.question_id=?""", (qid,))]
    conn.close()
    return q


def list_questions(question_type=None, difficulty=None, source=None,
                   kp_id=None, keyword=None, limit=500, offset=0):
    """分类筛选：题型 / 难度 / 来源 / 知识点 / 关键词"""
    conn = get_conn()
    where, params = [], []
    if question_type:
        where.append('q.question_type=?')
        params.append(question_type)
    if difficulty:
        where.append('q.difficulty=?')
        params.append(difficulty)
    if source:
        where.append('q.source LIKE ?')
        params.append(f'%{source}%')
    if kp_id:
        where.append('EXISTS (SELECT 1 FROM question_kp qk WHERE qk.question_id=q.id AND qk.kp_id=?)')
        params.append(kp_id)
    if keyword:
        # 中文友好检索：把关键词按字符判分，逐个匹配（AND 语义）
        chars = [c for c in keyword if not c.isspace()]
        if chars:
            for c in chars:
                where.append('EXISTS (SELECT 1 FROM search_index si '
                             'WHERE si.question_id=q.id AND si.chars LIKE ?)')
                params.append(f'%{c}%')

    sql = 'SELECT q.* FROM questions q'
    if where:
        sql += ' WHERE ' + ' AND '.join(where)
    sql += ' ORDER BY q.id DESC LIMIT ? OFFSET ?'
    params += [limit, offset]

    rows = [dict(r) for r in conn.execute(sql, params)]
    conn.close()
    return rows


def count_questions(**kw):
    conn = get_conn()
    n = conn.execute('SELECT COUNT(*) AS c FROM questions').fetchone()['c']
    conn.close()
    return n


def count_questions_filtered(question_type=None, difficulty=None, source=None,
                             kp_id=None, keyword=None):
    """按筛选条件计数（和 list_questions 的 WHERE 完全一致）。

    分页要显示「第 X/Y 页 · 共 N 题」，N 必须是**筛选后**的题数，
    不能用 count_questions() 的全库总数，否则筛完还是显示「共 8 题」。
    """
    conn = get_conn()
    where, params = [], []
    if question_type:
        where.append('q.question_type=?')
        params.append(question_type)
    if difficulty:
        where.append('q.difficulty=?')
        params.append(difficulty)
    if source:
        where.append('q.source LIKE ?')
        params.append(f'%{source}%')
    if kp_id:
        where.append('EXISTS (SELECT 1 FROM question_kp qk '
                     'WHERE qk.question_id=q.id AND qk.kp_id=?)')
        params.append(kp_id)
    if keyword:
        chars = [c for c in keyword if not c.isspace()]
        for c in chars:
            where.append('EXISTS (SELECT 1 FROM search_index si '
                         'WHERE si.question_id=q.id AND si.chars LIKE ?)')
            params.append(f'%{c}%')
    sql = 'SELECT COUNT(*) AS c FROM questions q'
    if where:
        sql += ' WHERE ' + ' AND '.join(where)
    n = conn.execute(sql, params).fetchone()['c']
    conn.close()
    return n


# ---------------- 题目查重 ----------------

# 查重用的字符归一化：先干掉 LaTeX 命令/花括号/空白/标点，
# 再只保留汉字、拉丁字母、数字。
# 题干里换个逗号、加个空格不算新题；$20\,\mathrm{m/s}$ 和 20m/s 也要视为同一道。
_DEDUP_DROP = re.compile(r'\\[a-zA-Z]+|[{}$\\]')
_DEDUP_KEEP = re.compile(r'[\u4e00-\u9fff0-9A-Za-z]')


def dedup_key(text):
    """把题干压成只含实义字符的指纹串，用于精确比对。"""
    t = _DEDUP_DROP.sub('', text or '')
    return ''.join(_DEDUP_KEEP.findall(t)).lower()


def _shingles(s, n=4):
    """字符 n-gram 集合。中文按字滑窗，比按词切更鲁棒（无需分词器）。

    文本短于 n 时退化为整个串，保证短题干也能比。
    """
    if not s:
        return set()
    if len(s) <= n:
        return {s}
    return {s[i:i + n] for i in range(len(s) - n + 1)}


def _jaccard(a, b):
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if not inter:
        return 0.0
    return inter / len(a | b)


def find_similar_questions(stem, exclude_id=None, threshold=0.72, limit=8):
    """查重：新录题干和已有题比对，返回可能的重复/相似题。

    返回 [{'id','stem','question_type','answer','source','score','kind'}...]，
    按 score 降序。kind: 'exact'=完全一样，'similar'=相似。

    threshold: Jaccard 相似度阈值。0.72 实测能把
      「同一道题的 OCR 两次识别差异」和「真正不同的题」分开，
      既不会满屏误报，也不会漏掉改了几个字的重复题。
    """
    target = dedup_key(stem)
    if not target:
        return []
    ts = _shingles(target)

    conn = get_conn()
    try:
        rows = conn.execute(
            'SELECT id, stem, question_type, answer, source FROM questions'
        ).fetchall()
    finally:
        conn.close()

    out = []
    for r in rows:
        if exclude_id is not None and r['id'] == exclude_id:
            continue
        other = dedup_key(r['stem'])
        if not other:
            continue
        if other == target:
            score, kind = 1.0, 'exact'
        else:
            # 长度差太多直接跳过，省掉大量无意义的比对
            lo, hi = sorted((len(target), len(other)))
            if lo and hi / lo > 2.2:
                continue
            score = _jaccard(ts, _shingles(other))
            kind = 'similar'
        if score >= threshold:
            out.append({
                'id': r['id'], 'stem': r['stem'] or '',
                'question_type': r['question_type'], 'answer': r['answer'] or '',
                'source': r['source'] or '', 'score': score, 'kind': kind,
            })

    out.sort(key=lambda x: (-x['score'], x['id']))
    return out[:limit]


# ---------------- Options ----------------

def replace_options(qid, options):
    conn = get_conn()
    conn.execute('DELETE FROM options WHERE question_id=?', (qid,))
    for i, o in enumerate(options):
        conn.execute(
            """INSERT INTO options(question_id, label, content, is_correct, sort_order)
               VALUES (?,?,?,?,?)""",
            (qid, o.get('label', ''), o.get('content', ''),
             int(o.get('is_correct', 0)), i)
        )
    conn.commit()
    conn.close()
    _refresh_index(qid)


# ---------------- 知识点 ----------------

def list_kp():
    conn = get_conn()
    rows = [dict(r) for r in conn.execute(
        'SELECT * FROM knowledge_points ORDER BY sort_order, id')]
    conn.close()
    return rows


def add_kp(name, parent_id=None):
    """新建知识点。重名返回 None（表上有 UNIQUE 约束）。"""
    name = (name or '').strip()
    if not name:
        return None
    conn = get_conn()
    try:
        cur = conn.execute(
            'INSERT INTO knowledge_points(name, parent_id) VALUES (?,?)',
            (name, parent_id))
        conn.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError:
        return None
    finally:
        conn.close()


def rename_kp(kp_id, new_name):
    """重命名知识点。返回 (ok, msg)。重名/空名返回 (False, 原因)。"""
    new_name = (new_name or '').strip()
    if not new_name:
        return False, '名称不能为空'
    conn = get_conn()
    try:
        dup = conn.execute(
            'SELECT id FROM knowledge_points WHERE name=? AND id<>?',
            (new_name, kp_id)).fetchone()
        if dup:
            return False, f'已存在同名知识点「{new_name}」'
        conn.execute('UPDATE knowledge_points SET name=? WHERE id=?',
                     (new_name, kp_id))
        conn.commit()
        # 知识点名进了检索索引，改名后要重建受影响题目的索引
        _reindex_by_kp(conn, kp_id)
        conn.commit()
        return True, ''
    except sqlite3.IntegrityError:
        return False, f'已存在同名知识点「{new_name}」'
    finally:
        conn.close()


def delete_kp(kp_id):
    """删除知识点。引用它的题干/索引自动清理（CASCADE + 重建索引）。
    返回被影响（原本挂了该知识点）的题目数。"""
    conn = get_conn()
    qids = [r[0] for r in conn.execute(
        'SELECT question_id FROM question_kp WHERE kp_id=?', (kp_id,))]
    conn.execute('DELETE FROM knowledge_points WHERE id=?', (kp_id,))
    conn.commit()
    for qid in qids:
        _refresh_index_conn(conn, qid)
    conn.commit()
    conn.close()
    return len(qids)


def kp_usage(kp_id):
    """该知识点被多少道题引用。"""
    conn = get_conn()
    n = conn.execute('SELECT COUNT(*) FROM question_kp WHERE kp_id=?',
                     (kp_id,)).fetchone()[0]
    conn.close()
    return n


def _reindex_by_kp(conn, kp_id):
    """知识点改名后，重建所有引用了它的题目的检索索引。"""
    qids = [r[0] for r in conn.execute(
        'SELECT question_id FROM question_kp WHERE kp_id=?', (kp_id,))]
    for qid in qids:
        _refresh_index_conn(conn, qid)


def _refresh_index_conn(conn, qid):
    """在已有连接上重算单题索引（供批量场景复用，避免反复开关连接）。
    与 _refresh_index 同 SQL，只是不自己开关连接/提交。"""
    conn.execute("""
        INSERT OR REPLACE INTO search_index(question_id, chars)
        SELECT q.id,
               COALESCE(q.stem,'') || ' ' || COALESCE(q.answer,'') || ' '
               || COALESCE(q.solution,'') || ' ' || COALESCE(q.source,'') || ' '
               || COALESCE(q.note,'') || ' '
               || COALESCE((SELECT GROUP_CONCAT(o.content, ' ') FROM options o
                            WHERE o.question_id = q.id), '') || ' '
               || COALESCE((SELECT GROUP_CONCAT(k.name, ' ')
                            FROM knowledge_points k
                            JOIN question_kp qk ON k.id = qk.kp_id
                            WHERE qk.question_id = q.id), '')
        FROM questions q WHERE q.id = ?
    """, (qid,))


# ---------------- 附件 ----------------

def add_attachment(qid, src_path, role='stem_figure', field='stem',
                   anchor_offset=-1, width_pct=0, sort_order=None):
    """把文件复制进 data/attachments/q_<id>/ 并登记。

    field/anchor_offset 记录图片插在哪个文本框的哪个字符位置，
    导出时按这个位置把图插回正文（而不是统一堆到末尾）。
    sort_order 为 None 时自动排到最后。
    """
    import shutil
    import hashlib
    qdir = os.path.join(ATTACH_DIR, f'q_{qid}')
    os.makedirs(qdir, exist_ok=True)

    with open(src_path, 'rb') as f:
        data = f.read()
    sha = hashlib.sha256(data).hexdigest()

    ext = os.path.splitext(src_path)[1].lower() or '.png'
    fname = f'{sha[:16]}{ext}'
    dst = os.path.join(qdir, fname)
    if not os.path.exists(dst):
        with open(dst, 'wb') as f:
            f.write(data)

    rel = os.path.relpath(dst, DATA_DIR).replace('\\', '/')
    conn = get_conn()
    if sort_order is None:
        r = conn.execute('SELECT COALESCE(MAX(sort_order), -1) + 1 AS n '
                         'FROM attachments WHERE question_id=?', (qid,)).fetchone()
        sort_order = r['n'] if r else 0
    cur = conn.execute(
        'INSERT INTO attachments(question_id, rel_path, file_type, role,'
        ' sort_order, sha256, field, anchor_offset, width_pct)'
        ' VALUES (?,?,?,?,?,?,?,?,?)',
        (qid, rel, 'image', role, sort_order, sha, field,
         int(anchor_offset), int(width_pct)))
    conn.commit()
    conn.close()
    return cur.lastrowid


def list_attachments(qid):
    conn = get_conn()
    rows = [dict(r) for r in conn.execute(
        'SELECT * FROM attachments WHERE question_id=? ORDER BY sort_order, id', (qid,))]
    conn.close()
    return rows


def update_attachment_position(aid, field=None, anchor_offset=None,
                               width_pct=None, sort_order=None):
    """改某张图的锚点/宽度/排序（只改传入的字段）。"""
    sets, vals = [], []
    for col, v in (('field', field), ('anchor_offset', anchor_offset),
                   ('width_pct', width_pct), ('sort_order', sort_order)):
        if v is not None:
            sets.append(f'{col}=?')
            vals.append(v)
    if not sets:
        return 0
    vals.append(aid)
    conn = get_conn()
    cur = conn.execute('UPDATE attachments SET %s WHERE id=?' % ', '.join(sets), vals)
    conn.commit()
    n = cur.rowcount
    conn.close()
    return n


def reorder_attachments(qid, ordered_ids):
    """按给定 id 顺序重排某题的图（sort_order 从 0 连编）。"""
    conn = get_conn()
    for i, aid in enumerate(ordered_ids):
        conn.execute('UPDATE attachments SET sort_order=? WHERE id=? AND question_id=?',
                     (i, aid, qid))
    conn.commit()
    conn.close()
    return len(ordered_ids)


def delete_attachment(aid):
    """删一条附件记录（不动磁盘文件；被别的题共用时也只删本条）。"""
    conn = get_conn()
    cur = conn.execute('DELETE FROM attachments WHERE id=?', (aid,))
    conn.commit()
    n = cur.rowcount
    conn.close()
    return n


def abs_attach_path(rel_path):
    return os.path.join(DATA_DIR, rel_path.replace('/', os.sep))


def split_text_by_anchors(text, attachments, field='stem'):
    """把一段文本按图片锚点切成 [('text', s) | ('img', att), ...]。

    规则：
      * 只取 attachments 里 field 匹配、且 anchor_offset >= 0 的图；
      * offset 超出文本长度就夹到末尾；
      * 同一 offset 多张图按 sort_order 保持顺序；
      * 无锚点的图（-1）不在这里出现，由调用方决定怎么办。

    导出侧用它把图插回正文原位置——这就是「所见即所得」。
    """
    text = text or ''
    marks = []
    for a in attachments:
        if (a.get('field') or 'stem') != field:
            continue
        off = a.get('anchor_offset')
        if off is None or off < 0:
            continue
        off = max(0, min(int(off), len(text)))
        marks.append((off, a.get('sort_order') or 0, a.get('id') or 0, a))
    marks.sort(key=lambda t: (t[0], t[1], t[2]))

    out = []
    pos = 0
    for off, _so, _aid, a in marks:
        if off > pos:
            out.append(('text', text[pos:off]))
        out.append(('img', a))
        pos = off
    if pos < len(text):
        out.append(('text', text[pos:]))
    return out


# ---------------- 试卷 ----------------

def add_paper(title, description='', config=None):
    conn = get_conn()
    cur = conn.execute(
        'INSERT INTO papers(title, description, config_json) VALUES (?,?,?)',
        (title, description, json.dumps(config or {}, ensure_ascii=False)))
    pid = cur.lastrowid
    conn.commit()
    conn.close()
    return pid


def add_paper_question(pid, qid, sort_order=0, score=0):
    conn = get_conn()
    conn.execute(
        'INSERT OR REPLACE INTO paper_questions(paper_id, question_id, sort_order, score) VALUES (?,?,?,?)',
        (pid, qid, sort_order, score))
    conn.commit()
    conn.close()


def list_papers():
    conn = get_conn()
    rows = [dict(r) for r in conn.execute('SELECT * FROM papers ORDER BY id DESC')]
    conn.close()
    return rows


def get_paper(pid):
    conn = get_conn()
    p = conn.execute('SELECT * FROM papers WHERE id=?', (pid,)).fetchone()
    if not p:
        conn.close()
        return None
    p = dict(p)
    p['questions'] = [dict(r) for r in conn.execute(
        """SELECT q.*, pq.sort_order AS pq_order, pq.score AS pq_score
           FROM paper_questions pq JOIN questions q ON q.id=pq.question_id
           WHERE pq.paper_id=? ORDER BY pq.sort_order""", (pid,))]
    conn.close()
    return p


if __name__ == '__main__':
    init_db()
    print(f'DB ready: {DB_PATH}')
    print(f'知识点: {len(list_kp())} 个')
    print(f'题目数: {count_questions()}')
