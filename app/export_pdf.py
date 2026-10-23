"""
export_pdf.py — 试卷导出 PDF

两条路，按可用性自动选：
  1. Word COM（需要装 Word）—— 效果最好，和 .docx 完全一致
  2. LibreOffice headless（soffice）—— 不用装 Office

核心流程：先 export_docx 生成 .docx，再转 PDF。
这样 Word/PDF 两条输出天然一致，不会出现「Word 里公式好好的、
PDF 里变乱码」的分裂。
"""
import os
import sys
import shutil
import subprocess
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db
import export_docx


# ---------------------------------------------------------------- 探测

def find_word():
    """返回可用的 Word.Application，失败返回 None"""
    try:
        import win32com.client
        app = win32com.client.Dispatch('Word.Application')
        return app
    except Exception:
        return None


def find_soffice():
    """找 LibreOffice 可执行文件"""
    cands = [
        r'C:\Program Files\LibreOffice\program\soffice.exe',
        r'C:\Program Files (x86)\LibreOffice\program\soffice.exe',
    ]
    for c in cands:
        if os.path.exists(c):
            return c
    return shutil.which('soffice')


def available_methods():
    """返回可用转换方式列表，供界面提示"""
    out = []
    if find_word():
        out.append('word')
    if find_soffice():
        out.append('libreoffice')
    return out


# ---------------------------------------------------------------- Word COM

def _docx_to_pdf_word(docx_path, pdf_path, app=None):
    own = app is None
    if own:
        app = find_word()
    if app is None:
        raise RuntimeError('Word 不可用')
    doc = None
    try:
        docx_path = os.path.abspath(docx_path)
        pdf_path = os.path.abspath(pdf_path)
        doc = app.Documents.Open(docx_path, ReadOnly=True,
                                 AddToRecentFiles=False)
        # 17 = wdExportFormatPDF, 0 = wdExportAllDocument
        doc.ExportAsFixedFormat(
            OutputFileName=pdf_path,
            ExportFormat=17,
            OpenAfterExport=False,
            OptimizeFor=0,          # wdExportOptimizeForPrint
            CreateBookmarks=1,      # 目录书签（按标题）
            DocStructureTags=True,
            BitmapMissingFonts=True)
        return pdf_path
    finally:
        if doc is not None:
            try:
                doc.Close(SaveChanges=0)
            except Exception:
                pass
        if own:
            try:
                app.Quit()
            except Exception:
                pass


# ---------------------------------------------------------------- LibreOffice

def _docx_to_pdf_lo(docx_path, pdf_path, soffice=None):
    soffice = soffice or find_soffice()
    if not soffice:
        raise RuntimeError('LibreOffice 未安装')
    outdir = os.path.dirname(os.path.abspath(pdf_path))
    os.makedirs(outdir, exist_ok=True)
    # LO 用 --outdir 输出，文件名与 docx 同名
    cmd = [soffice, '--headless', '--norestore', '--convert-to', 'pdf',
           '--outdir', outdir, os.path.abspath(docx_path)]
    r = subprocess.run(cmd, capture_output=True, timeout=300)
    produced = os.path.join(
        outdir, os.path.splitext(os.path.basename(docx_path))[0] + '.pdf')
    if not os.path.exists(produced):
        raise RuntimeError(
            f'LibreOffice 转换失败: {r.stderr.decode("utf-8", "replace")[:300]}')
    if os.path.abspath(produced) != os.path.abspath(pdf_path):
        os.replace(produced, pdf_path)
    return pdf_path


# ---------------------------------------------------------------- 对外

def docx_to_pdf(docx_path, pdf_path, method='auto'):
    """
    docx -> pdf。method: auto | word | libreoffice
    auto 顺序：Word（效果最好）-> LibreOffice
    """
    if not os.path.exists(docx_path):
        raise FileNotFoundError(docx_path)

    if method in ('auto', 'word'):
        app = find_word()
        if app:
            try:
                return _docx_to_pdf_word(docx_path, pdf_path, app)
            except Exception as e:
                if method == 'word':
                    raise
                err_word = e
        elif method == 'word':
            raise RuntimeError('Word 不可用')
        else:
            err_word = RuntimeError('Word 不可用')

    if method in ('auto', 'libreoffice'):
        try:
            return _docx_to_pdf_lo(docx_path, pdf_path)
        except Exception as e:
            raise RuntimeError(f'所有转换方式均失败。Word: {err_word}; '
                              f'LibreOffice: {e}')
    raise ValueError(f'未知 method: {method}')


def export_paper_pdf(pid, out_pdf=None, include_answer=True,
                     include_solution=True, include_figures=True,
                     title=None, order_by_type=True, keep_docx=False,
                     method='auto'):
    """
    导出试卷为 PDF。
    先出 .docx（复用 export_docx），再转 PDF。
    keep_docx=True 时把中间 .docx 也保留在同一目录。
    返回 (pdf_path, stats) —— stats 里含 docx 的统计信息。
    """
    if out_pdf is None:
        paper = db.get_paper(pid)
        if not paper:
            raise ValueError(f'试卷 {pid} 不存在')
        name = (title or paper['title']) or f'试卷{pid}'
        safe = ''.join(c for c in name if c not in r'\/:*?"<>|').strip()
        out_pdf = os.path.join(db.export_dir(), f'{safe}.pdf')

    os.makedirs(os.path.dirname(os.path.abspath(out_pdf)), exist_ok=True)

    if keep_docx:
        docx_path = os.path.splitext(out_pdf)[0] + '.docx'
        tmp_cleanup = False
    else:
        fd, docx_path = tempfile.mkstemp(suffix='.docx', prefix='qb_')
        os.close(fd)
        tmp_cleanup = True

    try:
        _, stats = export_docx.export_paper(
            pid, docx_path,
            include_answer=include_answer,
            include_solution=include_solution,
            include_figures=include_figures,
            title=title,
            order_by_type=order_by_type)
        docx_to_pdf(docx_path, out_pdf, method=method)
    finally:
        if tmp_cleanup and os.path.exists(docx_path):
            try:
                os.remove(docx_path)
            except Exception:
                pass

    stats['pdf'] = out_pdf
    stats['docx'] = docx_path if keep_docx else '(临时，已删除)'
    stats['pdf_bytes'] = os.path.getsize(out_pdf)
    return out_pdf, stats


def count_pages(pdf_path):
    """用 PyMuPDF 数页数"""
    try:
        import fitz
        with fitz.open(pdf_path) as d:
            return d.page_count
    except Exception:
        return None


if __name__ == '__main__':
    # python export_pdf.py <paper_id> [out.pdf]
    a = sys.argv[1:]
    if not a:
        print('用法: export_pdf.py <paper_id> [out.pdf]')
    else:
        pid = int(a[0])
        out = a[1] if len(a) > 1 else None
        p, st = export_paper_pdf(pid, out, keep_docx=True)
        print('PDF :', p)
        print('页数:', count_pages(p))
        print('大小:', f"{st['pdf_bytes']/1024:.1f} KB")
