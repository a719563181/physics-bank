============================================================
  高中物理题库管理（离线版）—— OCR 引擎包 使用说明
  v1.2.0（2026-10-22）
============================================================

【这是什么】

安装包默认【不含】OCR 引擎。没有它，程序其余功能（题库、录入、
组卷、导出 Word/PDF/PPT、Excel/CSV 导入）全都正常，只是
「批量导入 Word/OCR」里的 OCR 部分（PDF / 扫描件 / 照片）用不了。

这个包就是补上 OCR 能力的：内含一套自包含的 Python 运行时 +
PaddleOCR + paddlepaddle(CUDA) + **全部模型**，解压即用，无需安装，
**完全离线，不联网也能用**。

本版新增：
  · 公式识别模型（PP-FormulaNet_plus-M，595 MB）—— 公式转 LaTeX
  · 表格识别模型（SLANet）—— 实验数据表转结构化文本
  · 版面分析模型（PP-DocLayout_plus-L）—— 配图自动切出
  以上三个已预置在包内，首次 OCR 不再下载。


【怎么用】（三步）

1. 把本包解压，得到一个文件夹  runtime-ocr
   解压后的目录结构应当像这样：

      runtime-ocr\
        python.exe
        python312.dll
        DLLs\
        Lib\
        Scripts\
        official_models\        <-- 预置模型（5 个，860 MB）
          PP-OCRv6_medium_det\
          PP-OCRv6_medium_rec\
          PP-DocLayout_plus-L\
          PP-FormulaNet_plus-M\
          SLANet\

2. 把整个 runtime-ocr 文件夹，放到「物理题库」的【程序目录旁边】，
   与 PhysicsBank.exe 同一层。即最终变成：

      C:\Program Files\物理题库\
        PhysicsBank.exe
        _internal\
        runtime-ocr\          <-- 放这里

3. 打开程序 → 「批量导入 Word/OCR」→ 选 PDF/图片 → 就能用了。
   首次识别会加载模型，约需 15-30 秒（三个模型依次加载），之后很快。


【程序是从哪些地方找 OCR 引擎的】

程序按以下顺序查找带 OCR 的 python.exe，任一个命中即可：

  1. 环境变量 PHYSICS_BANK_OCR_PYTHON 指向的 python.exe
  2. <数据目录>\venv-ocr\Scripts\python.exe
  3. <程序目录>\runtime-ocr\python.exe          <-- 推荐放这里
  4. <程序目录>\runtime-ocr\Scripts\python.exe
  5. <程序目录>\ocr\python.exe
  6. <程序目录>\venv-ocr\Scripts\python.exe
  7. 以上目录各自上溯两级的同名子目录

所以「放到程序目录旁的 runtime-ocr」，就会命中第 3 条。


【模型从哪里找】

程序按以下顺序找模型目录，命中就不联网：

  1. 环境变量 PHYSICS_BANK_MODEL_DIR
  2. <程序目录>\runtime-ocr\official_models\     <-- 本包已带
  3. <程序目录>\official_models\

都没有才会走 paddlex 默认缓存 + 自动下载（需要联网）。


【★ 中文目录能用吗】

能。但要说清楚一件事：

Paddle 推理引擎本身**不支持含中文的路径**（会报
`json parse error ... attempting to parse an empty input`）。
这是 Paddle 的已知限制，不是本程序的 bug。

程序已内置规避：检测到模型所在路径含中文时，会自动把模型镜像到
纯英文路径再加载，位置是：

    C:\Users\<你>\AppData\Local\PhysicsBank\model_cache\<哈希>\

镜像只需做一次（约 860 MB，第一次启动慢 10-20 秒），之后直接复用。
所以你**可以**把程序装在「D:\我的题库」这种中文目录里，无需改名。


【没有 GPU 行不行】

可以。引擎包自带 CUDA 运行库，显卡为 NVIDIA 且驱动正常时会自动
用 GPU 加速（快很多）；没有合适的显卡时 paddle 会回退到 CPU，
仍可用，只是慢一些。

实测（NVIDIA P106-100）：5 页真实试卷 65-78 秒，
含公式/表格/配图/多栏全部增强。


【怎么确认引擎生效了】

点「批量导入」→ 选文件 → 若弹出的是识别进度（而不是
「没有找到 OCR 引擎」的提示），就说明引擎已就位。

若仍提示找不到引擎：
  · 确认 runtime-ocr 确实在 PhysicsBank.exe 同一层
  · 确认 runtime-ocr\python.exe 这个文件真的存在
  · 重启一次程序


【体积说明】

引擎包压缩后 2.07 GB，解压后 5.6 GB。
  · Python 运行时 + 标准库       约 0.1 GB
  · paddlepaddle-gpu + CUDA 库   约 4.0 GB   <- 大头
  · 5 个模型                     约 0.86 GB
  · 其他依赖                     约 0.6 GB

这是「完全离线、装完即用」的代价。解压后请勿移动单个文件，
若要挪动请整体挪。


【校验】

文件：PhysicsBank_OCR引擎包_v1.2.0.7z
大小：2.07 GB
MD5 ：3B4DD2A73047DB4305334154A4FF4365
文件数：34597

可用 7-Zip 右键「测试压缩包」自查完整性。
