# 高中物理题库管理（离线版）

Windows 离线运行的中学物理题库管理工具。个人自用，纯本地、不联网。

当前版本：**v1.0.9**

## 功能

- **题目录入**：题干 / 答案 / 解析三段式，LaTeX 公式（`$...$`）实时预览
- **题型**：单选 `choice_single` / 多选 `choice_multi` / 填空 `blank` / 解答 `solve` / 判断 `judge`
- **选项并入题干**：`A. xxx` 每行一个直接写在题干里，不再单独建表录入
- **配图**：粘贴或选取图片，图随光标定位；缩略图墙可拖拽排序、双击看大图
- **知识点**：可自定义增删改，支持对话框内快增与独立管理窗口
- **批量导入**：OCR（PaddleOCR，GPU 加速）+ Excel/CSV
- **查重**：录入时检索相似题，弹左右对照框确认后才入库
- **组卷导出**：Word（pandoc OMML 原生公式）/ PDF / PPT（公式转 PNG，可自定义模板）
- **题库迁移**：整体导出/导入 `.pbk` 包

## 目录

```
app/                     源码（唯一需要版本化的部分）
  main.py                主窗口、题目列表、分页
  db.py                  SQLite 数据层（含迁移、检索索引、锚点）
  question_edit.py       题目录入/编辑对话框（含配图面板）
  formula.py             LaTeX 渲染与诊断
  formula_edit.py        公式输入面板（55 按钮 / 5 组）
  export_docx.py         Word 导出
  export_pdf.py          PDF 导出
  export_pptx.py         PPT 导出
  ppt_theme.py           主题模型
  ppt_theme_dialog.py    主题编辑
  ocr_import.py          OCR 批量导入
  excel_import.py        Excel/CSV 导入
  bank_transfer.py       .pbk 迁移包
  paper_edit.py          组卷
  kp_manage_dialog.py    知识点管理
  launcher.py            启动器（设置 PHYSICS_BANK_HOME）
  env_check.py           环境自检
build/
  PhysicsBank.iss        Inno Setup 脚本
  seed/bank.db           空模板（0 题 / 23 知识点），打包用
  app.ico, app_512.png   图标
PhysicsBank.spec         PyInstaller 配置
```

`venv*`、`dist/`、`data/`、`backups/`、`imports/`、`exports/` 均在 `.gitignore` 中，
**不入库**（含约 11.7 GB 虚拟环境与真实题库数据）。

## 数据位置

运行期数据与程序目录**分离**：

- 安装版：`%LOCALAPPDATA%\PhysicsBank\data\bank.db`
- 便携版：由 `PHYSICS_BANK_HOME` 环境变量指定

`_internal\data_tpl\bank.db` 是空模板；**绝不**随包铺 `_internal\data\bank.db`。

## OCR 环境（可选，独立解释器）

安装包**不含** OCR 引擎（paddle + CUDA 约 4.5 GB，不便随包分发）。
「扫码件 / 照片 / PDF」要额外准备一个装了 PaddleOCR 的 python 解释器；
其余功能（题库、组卷、导出 Word/PDF/PPT、Excel/CSV 导入）不受影响。

查找顺序（`import_dialog.OcrWorker._find_ocr_python`）：

1. 环境变量 `PHYSICS_BANK_OCR_PYTHON` 指向的 `python.exe`
2. `PHYSICS_BANK_HOME` / 项目根下的 `venv-ocr\Scripts\python.exe`
3. 程序目录上溯两级的 `venv-ocr\Scripts\python.exe`（打包版走这条）
4. `runtime-ocr\python.exe`、`runtime-ocr\Scripts\python.exe`、`ocr\python.exe`

设 `PHYSICS_BANK_NO_OCR=1` 可强制禁用。找不到时会提示上述目录。

**★ `venv-ocr` 必须自带 PyMuPDF**，否则一导 PDF 就
`ModuleNotFoundError: No module named 'fitz'`。
图片 OCR 不需要 fitz，只有 PDF 分支用 —— 缺了平时看不出来。

```
venv-ocr\Scripts\python.exe -m pip install "PyMuPDF==1.28.2"
```

版本与主 venv 对齐（两处都是 1.28.2）。`ocr_import._require_pdf_deps()`
会在跑 PDF 前自检，缺了给出上面的安装命令而不是裸 traceback。

## 构建

```powershell
# 1. 打包（约 3.5–5 分钟）
venv-build\Scripts\python.exe -m PyInstaller --noconfirm --clean PhysicsBank.spec

# 2. 编译安装包（约 1.5–3 分钟）
& "C:\Users\<你>\AppData\Local\Programs\Inno Setup 6\ISCC.exe" build\PhysicsBank.iss
# 产物：build\installer\高中物理题库管理_安装包_v<版本>.exe
```

版本号在 `build\PhysicsBank.iss` 的 `#define MyAppVersion`。

### 打包铁律（每版必查）

1. `_internal\data\bank.db` **不存在**
2. `_internal\data_tpl\bank.db` **存在**（空模板，110592 字节）
3. exe 旁**无** `data\` / `themes\`
4. 全目录**无** `*.db-shm` / `*.db-wal` / `*.db-journal`
5. 装好的 `app\question_edit.py` 与开发版 **MD5 一致**
6. 全目录文件**同一时间戳**（无上一版残留）

⚠ **查库用 `immutable=1`**：
`sqlite3.connect` 哪怕是只读意图，也会在路径旁生成 `-shm`/`-wal`。
验证脚本自己就能把刚验干净的东西弄脏，甚至打进安装包。
用 `file:<path>?immutable=1` 才真正零副作用。

⚠ **第 6 条要排除人工维护的文件**：
`build\README_安装说明.txt` 是手写的，时间戳总是旧的；
不要因为它报「有上一版残留」。

`build\PhysicsBank.iss` 的 `Source:` 一律用**绝对路径**——
ISCC 按 `.iss` 所在目录解析相对路径，会静默打进 `build\dist\` 的旧产物。

## 测试纪律

- 测试**一律用真实库的临时副本**，靠 `PHYSICS_BANK_HOME` 隔离，绝不碰真实 `bank.db`
- 副本先调 `db.init_db()` 跑迁移，否则新字段缺失
- 摸库用只读连接 `file:...?mode=ro`
- 任何 DELETE/UPDATE 脚本必须先 `SELECT COUNT` 前后对比
- 删关联表按依赖顺序：`question_kp` / `paper_questions` / `search_index` / `attachments` → `papers` / `questions`
- GUI 测试前后记录 PID，测完确认窗口标题还在
