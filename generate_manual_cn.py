# -*- coding: utf-8 -*-
"""Generates PMS用户手册.docx — the Chinese user manual for the DMS/PMS app.

Run with: python3 generate_manual_cn.py
Screenshots are pulled from manual_assets/ (see manual_assets/README if present).
"""
from pathlib import Path
from docx import Document
from docx.shared import Pt, RGBColor, Inches
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.oxml import OxmlElement
import datetime

HERE = Path(__file__).resolve().parent
ASSETS = HERE / "manual_assets"

doc = Document()
section = doc.sections[0]
section.page_width  = Inches(8.5)
section.page_height = Inches(11)
section.left_margin = section.right_margin = Inches(1.2)
section.top_margin  = section.bottom_margin = Inches(1)

FONT = 'Microsoft YaHei'
BLUE1 = RGBColor(0x1E, 0x40, 0xAF)
BLUE2 = RGBColor(0x1D, 0x4E, 0x89)

def _cn(run):
    run.font.name = FONT
    try:
        run._element.rPr.rFonts.set(qn('w:eastAsia'), FONT)
    except Exception:
        pass

def h1(text):
    p = doc.add_heading(text, level=1)
    for r in p.runs: _cn(r); r.font.color.rgb = BLUE1

def h2(text):
    p = doc.add_heading(text, level=2)
    for r in p.runs: _cn(r); r.font.color.rgb = BLUE2

def para(text):
    p = doc.add_paragraph()
    r = p.add_run(text); _cn(r)
    return p

def bullet(text, level=0):
    p = doc.add_paragraph(style='List Bullet')
    p.paragraph_format.left_indent = Inches(0.25 * (level + 1))
    r = p.add_run(text); _cn(r)

def numbered(text):
    p = doc.add_paragraph(style='List Number')
    r = p.add_run(text); _cn(r)

def tip(text):
    p = doc.add_paragraph()
    p.paragraph_format.left_indent = p.paragraph_format.right_indent = Inches(0.4)
    r1 = p.add_run('提示：'); _cn(r1); r1.bold = True; r1.font.color.rgb = RGBColor(0x05,0x6F,0x00)
    r2 = p.add_run(text); _cn(r2); r2.font.color.rgb = RGBColor(0x33,0x33,0x33)
    shd = OxmlElement('w:shd')
    shd.set(qn('w:val'),'clear'); shd.set(qn('w:color'),'auto'); shd.set(qn('w:fill'),'E6F4EA')
    p._p.get_or_add_pPr().append(shd)

def note(text):
    p = doc.add_paragraph()
    p.paragraph_format.left_indent = p.paragraph_format.right_indent = Inches(0.4)
    r1 = p.add_run('注意：'); _cn(r1); r1.bold = True; r1.font.color.rgb = RGBColor(0x92,0x4E,0x00)
    r2 = p.add_run(text); _cn(r2)
    shd = OxmlElement('w:shd')
    shd.set(qn('w:val'),'clear'); shd.set(qn('w:color'),'auto'); shd.set(qn('w:fill'),'FFF8E1')
    p._p.get_or_add_pPr().append(shd)

def table(headers, rows, widths=None):
    t = doc.add_table(rows=1+len(rows), cols=len(headers))
    t.style = 'Table Grid'
    for i, h in enumerate(headers):
        cell = t.rows[0].cells[i]
        r = cell.paragraphs[0].add_run(h); _cn(r); r.bold = True
        r.font.color.rgb = RGBColor(0xFF,0xFF,0xFF)
        shd = OxmlElement('w:shd')
        shd.set(qn('w:val'),'clear'); shd.set(qn('w:color'),'auto'); shd.set(qn('w:fill'),'1E40AF')
        cell._tc.get_or_add_tcPr().append(shd)
    for ri, row in enumerate(rows):
        for ci, val in enumerate(row):
            cell = t.rows[ri+1].cells[ci]
            r = cell.paragraphs[0].add_run(val); _cn(r)
    if widths:
        for i, w in enumerate(widths):
            for row in t.rows:
                row.cells[i].width = Inches(w)
    doc.add_paragraph()

def codeblock(text):
    p = doc.add_paragraph()
    p.paragraph_format.left_indent = Inches(0.4)
    r = p.add_run(text); r.font.name = 'Courier New'; r.font.size = Pt(9)
    shd = OxmlElement('w:shd')
    shd.set(qn('w:val'),'clear'); shd.set(qn('w:color'),'auto'); shd.set(qn('w:fill'),'F3F4F6')
    p._p.get_or_add_pPr().append(shd)

def figure(filename, caption, width=6.0):
    """Embed a screenshot (from manual_assets/) centered, with a small caption below."""
    path = ASSETS / filename
    if not path.exists():
        note(f'（截图缺失：{filename}）')
        return
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run()
    run.add_picture(str(path), width=Inches(width))
    cap = doc.add_paragraph()
    cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = cap.add_run(caption); _cn(r)
    r.font.size = Pt(9); r.font.color.rgb = RGBColor(0x70,0x70,0x70); r.italic = True
    doc.add_paragraph()

def pb(): doc.add_page_break()


# Cover
p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.CENTER; p.add_run('\n\n\n')
p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
r = p.add_run('照片管理系统'); _cn(r); r.font.size=Pt(32); r.bold=True; r.font.color.rgb=BLUE1
p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
r = p.add_run('Photo / Document Management System  (PMS)'); _cn(r); r.font.size=Pt(16); r.font.color.rgb=RGBColor(0x60,0x7D,0x8B)
doc.add_paragraph()
p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
r = p.add_run('用户手册'); _cn(r); r.font.size=Pt(28); r.bold=True; r.font.color.rgb=BLUE2
doc.add_paragraph(); doc.add_paragraph()
p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
r = p.add_run('版本 2.0  |  ' + datetime.date.today().strftime('%Y年%m月'))
_cn(r); r.font.size=Pt(12); r.font.color.rgb=RGBColor(0x55,0x55,0x55)
pb()

# TOC
h1('目录')
toc = [
    ('1','快速入门'),('1.1','系统要求'),('1.2','下载与安装'),('1.3','首次启动'),
    ('2','界面介绍'),('2.1','整体布局'),('2.2','顶部标签栏'),('2.3','主内容区'),
    ('3','文件夹管理（树形视图）'),('3.1','新建文件夹'),('3.2','重命名与删除文件夹'),
    ('3.3','排序与移动文件夹'),('3.4','序列号（SN）与描述'),
    ('3.5','登录信息（网站账号密码）'),('3.6','文本树预览'),
    ('4','文档操作'),('4.1','上传文件与年月自动归档'),('4.2','上传文件夹'),
    ('4.3','粘贴截图'),('4.4','关联已有文档'),('4.5','查看文档'),
    ('4.6','下载、发送与删除文档'),('4.7','重复文件检测'),
    ('5','OCR 文字识别'),('5.1','OCR 工作原理'),('5.2','执行 OCR'),('5.3','保存 OCR 文本以供搜索'),
    ('6','关键参数提取'),('6.1','什么是关键参数'),('6.2','从文档中提取参数'),('6.3','管理全局关键参数'),
    ('7','搜索文档'),('7.1','基本搜索'),('7.2','使用筛选器'),('7.3','打开并定位搜索结果'),
    ('8','生成数据手册 PDF'),('8.1','选择文档'),('8.2','生成 PDF'),
    ('9','合并文档为单个 PDF'),
    ('10','人脸识别'),('10.1','安装人脸识别组件'),('10.2','标记人脸'),('10.3','按人物浏览与自动匹配'),
    ('11','远程与移动设备上传'),('11.1','发送单个文档到手机'),('11.2','远程上传（二维码批量收件）'),
    ('12','项目管理'),('12.1','导出索引备份（METADATA）'),('12.2','导出完整备份（COMP）'),
    ('12.3','导出选定文件夹备份（PART）'),('12.4','备份文件保留规则'),('12.5','导入项目（.dms）'),
    ('12.6','导出 CSV'),('12.7','批量 ZIP 导入'),('12.8','层级结构导入（含自动去重编号）'),
    ('12.9','在应用内下载本手册'),
    ('13','云同步与邮件'),('13.1','使用 OneDrive 同步存储'),('13.2','邮件发送设置'),
    ('14','密码保护'),('15','键盘快捷键'),('16','常见问题与解决方法'),('','快速参考卡片'),
]
for num, title in toc:
    p = doc.add_paragraph()
    p.paragraph_format.left_indent = Inches(0.4) if '.' in num else Inches(0)
    label = (num + '  ' + title) if num else title
    r = p.add_run(label); _cn(r)
    if '.' not in num and num: r.bold = True
pb()


# 1 快速入门
h1('1  快速入门')
h2('1.1  系统要求')
table(['项目','要求'],[
    ['操作系统','Windows 10 / 11（64 位）或 macOS 12 及以上'],
    ['内存','最低 4 GB，推荐 8 GB（启用人脸识别时推荐 8 GB 以上）'],
    ['磁盘空间','程序本身约 150–250 MB；启用人脸识别后另需下载约 200 MB 组件；另需空间存放您的文档'],
    ['显示器分辨率','1280 x 720 或更高'],
    ['网络连接','日常使用不需要联网（完全离线运行）。仅在查询 GPS 地址、启用人脸识别、发送邮件或使用远程上传时需要联网。'],
],[1.8,4.2])

h2('1.2  下载与安装')
para('程序的安装包名为 DMS（这是内部程序名；界面中显示的产品名称为"照片管理系统 PMS"）。请按以下步骤安装：')
h2('Windows')
numbered('从您的联系人处接收或下载 DMS.zip 文件（例如通过电子邮件、微信或 U 盘）。')
numbered('右键单击 DMS.zip，选择【全部解压缩】。')
numbered('选择目标文件夹，例如 C:\\DMS，然后单击【解压缩】。')
numbered('打开解压后的文件夹，找到 DMS.exe 文件。')
numbered('双击 DMS.exe 启动程序。')
note('首次运行 DMS.exe 时，Windows 可能弹出 SmartScreen 安全警告。请单击【更多信息】然后单击【仍要运行】。此警告是因为该文件未经商业代码签名，程序本身是安全的。')
h2('Mac')
numbered('接收 DMS.app（或包含它的 .zip 压缩包），解压后拖入【应用程序】文件夹（或任意位置）。')
numbered('双击 DMS.app 启动程序。')
numbered('首次打开时 macOS 可能提示"无法验证开发者"，请前往【系统设置】→【隐私与安全性】，找到该提示并点击【仍要打开】。')
tip('无需安装任何其他软件（人脸识别组件除外，见第 10 章）。DMS 已内置 Python 及运行所需的库。')
doc.add_paragraph()

h2('1.3  首次启动')
para('首次启动 DMS 时，请按以下步骤操作：')
numbered('程序会在本机启动一个内置的 Web 服务器（固定使用 8765 端口），随后自动打开默认浏览器访问 http://127.0.0.1:8765。')
numbered('您将看到 PMS 欢迎界面。')
numbered('系统会提示您选择存储文件夹——所有文件和数据都将保存在此处。')
bullet('单击顶部标题栏中的文件夹图标（存储路径按钮）。')
bullet('输入路径，例如 C:\\Users\\用户名\\Documents\\PMS_Data（Windows）或 /Users/用户名/Documents/PMS_Data（Mac），然后确认。')
bullet('如果该文件夹不存在，DMS 会自动创建。')
numbered('设置存储路径后，主界面加载完成，即可开始使用。')
tip('建议将存储路径设置在会被云盘（如 OneDrive）同步的文件夹内，以获得自动异地备份的效果，详见第 13.1 节。')
pb()


# 2 界面介绍
h1('2  界面介绍')
h2('2.1  整体布局')
para('DMS 窗口分为三个主要区域：')
table(['区域','位置','用途'],[
    ['顶部标题栏','页面顶部','显示存储路径、已索引文档总数、【项目】菜单及退出登录按钮。'],
    ['左侧边栏','左侧面板','包含树形、搜索、数据手册、人脸四个标签页。'],
    ['主内容区','右侧/中间区域','显示当前所选文件夹中的文档，或对应标签页的内容。'],
],[1.5,1.8,3.3])
tip('拖动左侧边栏与主内容区之间的分隔线可调整宽度；双击分隔线可恢复默认宽度。')

figure('01_overview.png', '图 2-1　整体布局：左侧树形导航，右侧文件夹详情与操作按钮')

h2('2.2  顶部标签栏')
table(['标签','功能说明'],[
    ['树形','浏览和管理文件夹层级结构，是主要的导航视图。'],
    ['搜索','对所有文档进行全文和元数据搜索。'],
    ['数据手册','勾选文档后，生成带封面和目录的专业合并 PDF。'],
    ['人脸','人脸识别与按人物浏览照片（需先安装组件，见第 10 章）。'],
],[2.0,4.5])

h2('2.3  主内容区')
para('在树形标签中单击某个文件夹后，右侧面板将显示：')
bullet('文件夹名称、序列号（SN）、描述信息，以及登录信息（如已设置）（位于顶部）。')
bullet('操作按钮：标记人脸、链接现有文档、合并文档、粘贴截图、上传文件、不生成年月夹、上传文件夹。')
bullet('该文件夹中每个文档的卡片，显示文件名、大小、类型图标、上传日期及操作图标（SN、链接、发送至手机、发送邮件、删除）。')
pb()

# 3 文件夹管理
h1('3  文件夹管理（树形视图）')
para('DMS 使用树形结构组织文档，类似于文件资源管理器，但额外支持序列号、描述、登录信息和文档计数等功能。')

h2('3.1  新建文件夹')
numbered('在树形视图中单击某个文件夹以选中它（新文件夹将作为其子文件夹创建）。')
numbered('单击边栏工具栏中的【新建文件夹】按钮，或右键单击所选文件夹，选择【新建文件夹】。')
numbered('出现文本输入框后，输入文件夹名称，按 Enter 键确认，或按 Escape 键取消。')
tip('文件夹可以无限层级嵌套，没有深度限制。')

h2('3.2  重命名与删除文件夹')
para('重命名：')
bullet('双击文件夹名称，或右键单击然后选择【重命名】。')
bullet('编辑名称后按 Enter 键保存。')
para('删除：')
bullet('右键单击文件夹，选择【删除】，并确认。')
bullet('该文件夹中的文档不会立即从磁盘删除，而是被移动到一个名为"Not Show in Tree"的隐藏文件夹中暂存（软删除／回收站机制）。')
bullet('如需找回，可在树形视图中找到"Not Show in Tree"节点，将文档重新链接或移动到其他文件夹。')
note('在"Not Show in Tree"文件夹内直接删除文档，才会将文件从磁盘永久清除，此操作无法撤销。这与第 12.4 节中 .dms 备份文件的归档文件夹"Not Shown in The Tree"是两个不同的文件夹，请勿混淆——前者位于文档树内，用于暂存被删除的文档；后者位于存储目录根部，用于存放超出保留数量的旧备份文件。')

h2('3.3  排序与移动文件夹')
para('在同级文件夹间排序：')
bullet('将鼠标悬停在文件夹上，会出现向上和向下箭头按钮。')
bullet('单击向上箭头上移，单击向下箭头下移。')
para('移动到不同的上级文件夹：')
bullet('单击并拖动文件夹，将其拖放到目标父文件夹上（高亮显示放置目标）。')
bullet('也可以将其拖放到另一个文件夹的正上方或正下方，实现前置或后置排列。')

h2('3.4  序列号（SN）与描述')
para('每个文件夹都可以设置可选的序列号（SN）和描述，用于标记组件、批次或项目编号。')
bullet('单击文件夹上的铅笔图标，打开编辑模式。')
bullet('在序列号字段中输入值（例如 INS-2026-001），并可填写描述。')
bullet('SN 将显示在树形视图中该文件夹名称旁边。')
figure('02_folder_with_sn.png', '图 3-1　带序列号的文件夹（房屋保险 · INS-2026-001）')
figure('03_folder_edit_mode.png', '图 3-2　文件夹编辑模式：名称、序列号、描述与登录信息字段')

h2('3.5  登录信息（网站账号密码）')
para('如果某个文件夹对应一个需要登录的网站或在线账户（例如保险公司客户门户、医院患者门户），可以把该网站的登录信息直接保存在这个文件夹上，方便日后快速登录。')
numbered('单击文件夹的铅笔图标进入编辑模式。')
numbered('在【登录信息（可选）】区域填写：网站链接、账号／用户名、密码。')
numbered('单击【完成】保存。')
para('查看模式下会出现两个按钮：')
bullet('【打开网站】——在新标签页打开该网站，并自动将账号复制到剪贴板，方便粘贴到用户名输入框。')
bullet('【复制密码】——将密码复制到剪贴板，方便粘贴到密码输入框。')
figure('04_login_info_view.png', '图 3-3　已保存登录信息后的查看模式')
note('出于安全考虑，密码在服务器端使用加密方式存储（密钥保存在本机用户目录中，不随存储文件夹一起同步），并且只有在您点击【打开网站】或【复制密码】时才会被临时解密，不会随文件夹树的常规读取一并传输。由于密码在没有设置访问密码时同样可以被局域网内的其他访问者读取，建议在使用此功能前先设置第 14 章所述的访问密码。')

h2('3.6  文本树预览')
para('如果需要将整个文件夹结构以纯文本形式复制或导出（例如粘贴到聊天软件或文档中），可以使用文本树功能。')
numbered('在树形标签中单击左上角的【文本树】按钮。')
numbered('弹出的预览窗口以线框图形式显示完整的文件夹层级。')
numbered('勾选【显示序列号】可在每个文件夹名称后附加其 SN 与文档数量。')
numbered('单击【复制】将文本复制到剪贴板，或单击【导出 .txt】保存为文本文件。')
figure('17_text_tree.png', '图 3-4　文本树预览窗口')
pb()


# 4 文档操作
h1('4  文档操作')
h2('4.1  上传文件与年月自动归档')
numbered('在树形标签中选择目标文件夹。')
numbered('单击主内容区中的【上传文件】按钮（默认行为，见下方说明），或【不生成年月夹】按钮。')
numbered('在文件选择对话框中选择一个或多个文件，然后单击【打开】。')
numbered('屏幕顶部出现进度条，显示正在上传的文件数量。')
numbered('上传完成后，新的文档卡片将出现在面板中。')
para('两个上传按钮的区别：')
table(['按钮','行为'],[
    ['上传文件','若识别为照片且能读取到拍摄日期（EXIF），会在当前文件夹下自动创建"年份/月份"两级子文件夹并归档到其中；非照片或无法读取日期的文件直接放入当前文件夹。'],
    ['不生成年月夹','无论是否为照片，一律直接放入当前所选文件夹，不做年月自动归档。'],
],[1.6,4.4])
para('支持的文件类型：')
table(['类别','扩展名'],[
    ['图片','JPEG、PNG、GIF、WebP、BMP、TIFF、HEIC'],
    ['文档','PDF、DOC、DOCX、XLS、XLSX、PPT、PPTX、TXT、CSV、HTML、JSON、XML'],
    ['压缩包','ZIP'],
],[1.5,5.0])
tip('对于照片，DMS 会自动从 EXIF 元数据中读取拍摄日期和 GPS 位置，并与文档一起存储；地址会在联网时自动反查为可读的地名。')

h2('4.2  上传文件夹')
numbered('在树形视图中选择目标文件夹。')
numbered('单击【上传文件夹】按钮。')
numbered('在弹出的对话框中选择电脑上的一个文件夹。')
numbered('DMS 将递归上传所有文件，并在树形视图中创建对应的子文件夹。')

h2('4.3  粘贴截图')
para('无需先保存文件，也可以把刚截取的屏幕截图或从其他地方复制的图片直接粘贴进当前文件夹。')
numbered('在树形标签中选择目标文件夹。')
numbered('截取屏幕截图（或复制一张图片）到系统剪贴板。')
numbered('单击【粘贴截图】按钮，或直接按 Ctrl+V（Windows）／Cmd+V（Mac）。')
numbered('DMS 会把剪贴板中的图片保存为 PDF 并加入当前文件夹。')

h2('4.4  关联已有文档')
para('同一个文档可以关联到多个文件夹，而不会在磁盘上复制文件。')
numbered('选择您想要关联文档的文件夹。')
numbered('单击【链接现有文档】。')
numbered('弹出对话框，列出系统中的所有文档。')
numbered('勾选要关联的文档，然后单击【添加关联】。')
numbered('该文档现在同时出现在原始文件夹和新文件夹中。')

h2('4.5  查看文档')
numbered('单击文档名称或缩略图，打开文档查看器。')
numbered('查看器以弹窗形式打开，主体显示文档预览。')
bullet('图片直接内嵌显示。')
bullet('PDF 在内嵌查看器中打开，支持滚动和缩放。')
bullet('文本文件以等宽字体显示，方便阅读。')
bullet('Office 文件（Word、Excel、PowerPoint）提供下载按钮，可在本地 Office 应用中打开。')
numbered('查看器顶部有【添加描述】【查看地图位置】【提取文字（OCR）】【提取关键参数】【下载】【关闭】等操作按钮。')
numbered('单击【关闭】或按 Escape 键关闭查看器。')
figure('05_doc_viewer_pdf.png', '图 4-1　PDF 文档查看器')
figure('06_doc_viewer_image.png', '图 4-2　图片文档查看器')

h2('4.6  下载、发送与删除文档')
para('每个文档卡片右侧都有一排图标：')
table(['图标','功能'],[
    ['SN','为该文档单独设置序列号（默认继承所在文件夹的 SN）。'],
    ['链接','查看/管理该文档被哪些文件夹关联引用。'],
    ['发送至手机','生成可在手机上打开的下载链接，通过短信／iMessage 发送，或复制链接手动分享（见第 11.1 节）。'],
    ['发送邮件','将该文档作为附件发送邮件（需先完成第 13.2 节的 SMTP 设置；Mac 在未设置 SMTP 时会改为打开系统邮件客户端）。'],
    ['删除','从当前文件夹取消关联并软删除（移入"Not Show in Tree"，见第 3.2 节）。'],
],[1.3,4.7])
para('下载：单击文档卡片上的下载图标，或在查看器内单击【下载】按钮。')

h2('4.7  重复文件检测')
para('上传文件时，如果文件名和大小都与已存在的文档相同，DMS 会弹出提示，避免重复占用空间：')
bullet('跳过——不上传这份重复文件。')
bullet('仅关联——不复制文件，只把已存在的文档关联到当前文件夹（推荐，等同于第 4.4 节的效果）。')
bullet('仍然上传——作为新的独立文档上传，即使内容相同。')
pb()

# 5 OCR
h1('5  OCR 文字识别')
h2('5.1  OCR 工作原理')
para('OCR（光学字符识别）将扫描图片和 PDF 转换为可搜索的文本。DMS 使用 Tesseract OCR 引擎，支持中文（简体）和英文。')
table(['方法','适用情况','速度'],[
    ['文本层提取','已包含数字文本层的 PDF','非常快'],
    ['Tesseract OCR','扫描版 PDF、图片或文本层损坏的文档','中等（取决于文件大小）'],
],[1.8,3.2,1.5])
note('如果本机未安装 Tesseract，则无法对图片进行 OCR。但对于含数字文本层的 PDF，文本层提取功能仍可正常使用。')

h2('5.2  执行 OCR')
numbered('单击文档名称，打开文档查看器。')
numbered('单击【提取文字（OCR）】。')
numbered('DMS 开始提取文本，并显示结果。')
numbered('如果 PDF 包含内嵌文本层，提取几乎是即时完成的。')
numbered('如果需要 OCR（扫描文档或图片），会显示进度指示。')
numbered('完成后，提取的文本显示在可编辑的文本框中，可直接编辑以修正 OCR 错误。')
numbered('面板还会显示：已处理页数、字符数、检测到的语言和使用的提取方法。')
note('如果 PDF 的文本层已损坏或内容不正确，请单击【强制 OCR】按钮，改用 Tesseract 重新提取。')

h2('5.3  保存 OCR 文本以供搜索')
numbered('查看并根据需要编辑提取的文本后，单击【保存并启用搜索】。')
numbered('文本将随文档元数据一起保存。')
numbered('此后，在搜索标签中搜索该文档包含的词语时，即可找到该文档。')
tip('每个文档只需执行一次 OCR。文本保存后，文档将永久可搜索。')
pb()


# 6 关键参数
h1('6  关键参数提取')
h2('6.1  什么是关键参数')
para('关键参数是用户自定义的标签（例如合同编号、压力等级、人物、事件），DMS 可以从文档文本中自动查找并提取对应的值。每个参数存储两个值：')
bullet('设计值——规定或要求的值（例如 3000 psi）。')
bullet('实际值——从文档中找到的实测或认证值。')
para('这样您就可以快速比较数百份文档的规格与实际值。')

h2('6.2  从文档中提取参数')
numbered('打开文档查看器。')
numbered('单击【提取关键参数】。')
numbered('出现参数列表（全局默认参数加上文档专属参数）。')
numbered('勾选您想提取的参数。')
numbered('可选：切换【仅同行匹配】开关，仅在参数名称所在行内匹配值。')
numbered('单击【提取】。')
numbered('DMS 在文档文本中搜索，并在结果表中填入数值：')
table(['列','含义'],[
    ['参数','正在搜索的标签名'],
    ['找到的值','DMS 在文档文本中找到的值'],
    ['状态','已找到，或 NF（未找到）'],
],[1.5,5.0])
numbered('如需修改，可直接编辑任意值。')
numbered('单击【保存参数】，将这些值存储到文档的元数据中。')
tip('元数据是全局共享的——修改后，所有引用此文档的文件夹均会同步更新。')

h2('6.3  管理全局关键参数')
numbered('单击顶部标题栏中的【项目】菜单。')
numbered('选择【全局关键参数】。')
numbered('在弹出的对话框中，可以添加、删除或调整参数顺序。')
numbered('单击【恢复默认值】可重置为默认的参数列表。')
numbered('更改立即生效，适用于后续所有提取操作。')
figure('18_key_parameters.png', '图 6-1　全局关键参数管理对话框')
pb()

# 7 搜索
h1('7  搜索文档')
h2('7.1  基本搜索')
numbered('单击左侧边栏中的【搜索】标签。')
numbered('在搜索框中输入关键词或短语。')
numbered('搜索结果即时以卡片形式显示，每张卡片包含：')
bullet('文档名称和文件类型图标。')
bullet('上传日期、文件大小和文档 ID。')
bullet('所有元数据字段（参数名称 / 设计值 / 实际值）。')
bullet('GPS 定位地址（如有）。')
bullet('引用此文档的文件夹列表。')
note('搜索不区分大小写，支持部分匹配——例如输入【保险】可以找到【房屋保险单】【旅行保险单据】等。')
figure('07_search.png', '图 7-1　搜索结果示例')

h2('7.2  使用筛选器')
table(['筛选器','使用方法'],[
    ['文件类型','勾选图片、PDF、文本或文档，将结果限定为该类型。'],
    ['范围节点','启用【限定范围至所选节点】，仅在当前文件夹及其子文件夹中搜索。'],
    ['元数据','输入参数名称和/或值，查找具有匹配元数据的文档（例如：压力 = 3000 psi）。'],
],[1.5,5.0])

h2('7.3  打开并定位搜索结果')
bullet('单击结果卡片，打开该文档的查看器。')
bullet('双击结果卡片，切换到树形标签，并直接跳转到包含该文档的文件夹。')
pb()

# 8 数据手册
h1('8  生成数据手册 PDF')
para('数据手册功能可将选定的文档合并为一份专业 PDF，包含封面、目录、书签、章节标题和页码。')
h2('8.1  选择文档')
numbered('单击左侧边栏中的【数据手册】标签。')
numbered('树形视图中每个文件夹旁边会出现复选框及"全选"链接。')
numbered('勾选单个文档，或使用以下快捷操作：')
bullet('单击某文件夹旁的【全选】，选中该文件夹（及按需包含子文件夹）中的所有文档。')
bullet('单击【清除】取消所有选择。')
tip('您的选择会自动保存——关闭并重新打开 DMS 后，选择状态仍然保留。')
figure('08_databook_tab.png', '图 8-1　数据手册标签：勾选要合并的文档')

h2('8.2  生成 PDF')
numbered('选好文档后，单击【生成数据手册】。')
numbered('在弹出的对话框中填写标题和副标题。')
bullet('标题——封面上显示的主标题。')
bullet('副标题——辅助说明行（例如项目名称、版本号）。')
numbered('单击【生成并下载】。')
numbered('DMS 在服务器端生成 PDF，您的浏览器将自动下载该文件。')
para('生成的 PDF 包含以下内容：封面（标题、副标题和日期）、带可点击书签的目录、章节标题页、按树形顺序排列的所有选定文档、每页均有页码和页脚。')
figure('09_databook_dialog.png', '图 8-2　生成数据手册对话框')
pb()


# 9 合并文档
h1('9  合并文档为单个 PDF')
para('合并功能可将一个文件夹中的多个文档合并为单个 PDF 文件，并将结果保存回该文件夹。')
numbered('在树形标签中选择目标文件夹。')
numbered('单击主内容区中的【合并文档】按钮。')
numbered('弹出对话框，列出该文件夹中的所有文档。')
numbered('可选：勾选【包含子文件夹中的文档】，同时合并子文件夹中的文档。')
numbered('勾选要合并的文档（或单击【全选】）。')
numbered('在【输出文件名】字段中输入合并后文件的名称。')
numbered('单击【合并 N 份文档并保存至此文件夹】。')
figure('10_combine_dialog.png', '图 9-1　合并文档对话框')
para('支持的原始文件格式：')
table(['类型','处理方式'],[
    ['PDF','页面直接原样并入合并结果。'],
    ['图片（JPEG/PNG/GIF/BMP/TIFF/WebP/HEIC）','每张图片转换为单独一页，并附文件名作为说明文字。'],
    ['其他格式（Word、Excel、PowerPoint、纯文本等）','不会被转换，合并结果中该位置会插入一页提示"Unsupported type"的占位页。'],
],[2.2,4.3])
note('合并后，参与合并的原始文档会从当前文件夹中移除（取消关联），但文件本身仍保留在文件库中——如果这些文档还关联着其他文件夹，仍可在那些文件夹或搜索标签中找到。若需要保留 Word/Excel/PPT 等文件的内容，请先在对应软件中"另存为 PDF"，再上传后合并。')
pb()


# 10 人脸识别
h1('10  人脸识别')
para('DMS 内置人脸识别功能，可以从照片中检测人脸、标记姓名，并按人物快速浏览家庭照片。该功能基于开源库 DeepFace，首次使用前需要下载安装。')

h2('10.1  安装人脸识别组件')
para('单击左侧边栏中的【人脸】标签。若组件尚未安装，会显示安装提示：')
figure('16_face_tab.png', '图 10-1　"人脸"标签页（此处组件已安装，故显示空状态；未安装时会显示"需要安装 deepface"提示及【自动安装 deepface】按钮）')
numbered('单击【自动安装 deepface】按钮。')
numbered('DMS 会在后台自动下载并安装一个独立的 Python 3.12 环境及 DeepFace 相关库（约 200 MB 起，取决于网速可能需要数分钟到十几分钟）。')
numbered('安装在后台进行，期间按钮会显示"安装中，请稍候…"，您可以继续使用应用的其他功能，无需等待或保持该对话框打开。')
numbered('安装完成后按钮状态会自动更新，即可开始标记人脸。')
note('人脸识别需要 Python 3.12（DMS 主程序运行在更新的 Python 版本上，因人脸识别所依赖的 TensorFlow 库暂不支持该版本，故单独使用一个隔离环境）。如果电脑上没有 Python 3.12，安装过程会提示下载链接，请下载安装后重新点击【自动安装 deepface】。')

h2('10.2  标记人脸')
numbered('打开包含人物照片的文件夹。')
numbered('单击【标记人脸】按钮，进入多选模式，每张照片左上角出现选择框。')
numbered('勾选要处理的照片，顶部按钮会显示"标记选中照片 (N)"。')
figure('21_face_select_mode.png', '图 10-2　多选模式：勾选要检测人脸的照片')
numbered('单击【标记选中照片】，弹出对话框并自动开始检测人脸。')
figure('22_face_tagging_dialog.png', '图 10-3　人脸检测进行中')
numbered('检测完成后，每张检测到人脸的照片会显示裁剪出的人脸缩略图。')
numbered('单击某个人脸缩略图，输入姓名并保存，即可将该人脸标记为指定人物。')
numbered('单击【完成】关闭对话框。')

h2('10.3  按人物浏览与自动匹配')
para('切换到【人脸】标签页后，已标记的人物会以卡片形式列出，并显示照片数量。')
bullet('单击某个人物卡片，打开该人物的照片画廊。')
bullet('单击【识别】，可指定文件夹范围和相似度阈值，让 DMS 在更大范围内自动查找该人物可能出现的其他照片。')
bullet('在照片画廊中单击【创建匹配文件夹】，可将该人物的所有关联照片一次性整理进一个新建的"人脸匹配/姓名"文件夹，方便统一查看或导出。')
bullet('如需删除某个人物标记，可在该人物卡片上选择删除（仅删除人物标签，不影响原始照片文件）。')
pb()


# 11 远程与移动设备上传
h1('11  远程与移动设备上传')
h2('11.1  发送单个文档到手机')
para('如果想把某一份已上传的文档快速传到手机上（例如出示保险单、身份证件），可以使用"发送至手机"功能，无需额外的传输软件。')
numbered('在文档卡片上单击【发送至手机】图标（电话形状）。')
numbered('输入手机号码。')
numbered('DMS 会生成一个下载链接，并尝试通过 iMessage（Mac）或"手机连接"（Windows，Phone Link）直接发送短信；也可以手动复制链接粘贴到微信等应用中发送。')
note('对方点击链接后可直接在手机浏览器中下载该文档，无需安装 App。')

h2('11.2  远程上传（二维码批量收件）')
para('如果想让家人朋友把手机里的多张照片或文件直接上传到您电脑上的 DMS 指定文件夹（而不是您去接收单个文件），可以使用桌面端启动器中的"远程上传"功能。')
numbered('在桌面启动器（程序托盘窗口，非浏览器网页）中找到并单击【远程上传】。')
numbered('程序会自动建立一条通过 cloudflared 的安全隧道连接（首次使用可能需要下载隧道组件，需要联网）。')
numbered('选择接收上传文件的目标文件夹。')
numbered('DMS 生成一个二维码及对应链接。')
numbered('对方使用手机扫描二维码，或通过 iMessage / 短信（Phone Link）/ 微信收到链接后打开，即可从手机相册直接选择照片或文件上传。')
note('该链接默认 7 天内有效，过期后需要重新生成。此功能通过临时隧道将您的电脑短暂暴露到公网，仅在需要接收文件时开启，用完建议关闭远程上传或退出 DMS。')
pb()


# 12 项目管理
h1('12  项目管理')
para('顶部标题栏中的【项目】菜单提供备份、恢复和批量导入数据的工具。')
figure('11_project_menu.png', '图 12-1　"项目"菜单：导出、导入与设置')

h2('12.1  导出索引备份（METADATA）')
para('仅备份文件夹树结构和文档元数据，不包含实体文件本身，因此生成速度快、文件体积小。适合频繁备份，用于恢复文件夹结构或误操作回滚。')
numbered('单击【项目】→【导出索引备份（.dms）】。')
numbered('DMS 直接将备份文件保存到当前存储文件夹内，文件名以 METADATA- 开头（例如 METADATA-项目名-20260719-153000.dms）。')
tip('DMS 在每次正常退出（点击"退出 DMS"或关闭窗口）以及每次启动关闭服务器时，也会自动生成一份同样以 METADATA- 开头的索引备份，无需手动操作。')

h2('12.2  导出完整备份（COMP）')
para('将文件夹树和全部实体文件一起打包，适合作为完整异地备份或迁移到另一台电脑。')
numbered('单击【项目】→【导出完整备份（含文件）】。')
numbered('DMS 会先把备份文件保存到存储文件夹内（文件名以 COMP- 开头），随后浏览器同时下载一份到默认下载文件夹。')
note('若文档数量或体积较大，生成完整备份可能需要一些时间，请耐心等待，不要关闭浏览器标签页。')

h2('12.3  导出选定文件夹备份（PART）')
para('只想备份或分享某几个文件夹（而非整个项目）时使用。')
numbered('单击【项目】→【导出选定文件夹（含文件）】。')
numbered('在弹出的对话框中勾选要导出的文件夹。')
numbered('单击导出。文件同样先保存到存储文件夹内（文件名以 PART- 开头），再额外下载一份到浏览器默认下载文件夹。')

h2('12.4  备份文件保留规则')
para('为避免存储文件夹被大量历史备份占满，DMS 会自动整理 .dms 备份文件：')
bullet('存储文件夹根目录下最多保留 10 个最近的 .dms 文件（按修改时间排序，含 METADATA-、COMP-、PART- 三类，合计计算）。')
bullet('超出数量的旧备份会被自动移动到存储文件夹下的一个名为"Not Shown in The Tree"的子文件夹中归档，不会被删除。')
note('这个归档文件夹与第 3.2 节提到的文档回收站文件夹"Not Show in Tree"名称非常相似但并不是同一个：前者（"Not Shown in The Tree"）只存放多余的 .dms 备份文件，位于存储目录根部；后者（"Not Show in Tree"）是文档树内的一个节点，存放被软删除的文档。')

h2('12.5  导入项目（.dms）')
para('在任何运行 DMS 的电脑上还原之前导出的项目备份（METADATA / COMP / PART 三种备份文件均可导入）。')
numbered('单击【项目】→【导入项目（.dms）】。')
numbered('选择 .dms 文件。')
numbered('在对话框中选择电脑上的目标文件夹（建议选择新的空文件夹）。')
numbered('单击【导入】。DMS 解压所有文件并重建树形结构。')
note('如果导入到已有数据的文件夹，系统会提示您确认是否覆盖。')

h2('12.6  导出 CSV')
para('将所有文档的元数据导出为 CSV 电子表格。')
numbered('单击【项目】→【导出 CSV】。浏览器下载 CSV 文件。')
para('CSV 文件每行对应一个文档，列信息包括：文档 ID、名称、文件大小、上传日期及所有元数据参数字段。')
tip('在 Microsoft Excel 或 Google 表格中打开 CSV，可进行数据分析、报告生成或与同事共享。')

h2('12.7  批量 ZIP 导入')
para('通过命名规范快速上传大量文件，并自动分配到对应文件夹。')
numbered('准备一个 ZIP 文件，按以下格式命名每个文件：文件夹名#说明.pdf（例如：房屋保险#2026年保单.pdf）。')
numbered('单击【项目】→【从 ZIP 批量导入文档】，选择您的 ZIP 文件。')
numbered('DMS 读取每个文件名，将 # 号前的部分与树形视图中的文件夹名称匹配，并将文档放入对应文件夹。')
note('导入前，文件夹必须已在树形视图中存在。文件夹名称无法匹配的文档将进入未关联状态。')

h2('12.8  层级结构导入（含自动去重编号）')
para('通过纯文本／CSV 文件快速创建文件夹树结构。适合从其他系统（如物料清单、组织架构表）批量导出文件夹层级后直接生成对应目录。')
numbered('准备一个文本文件，每行 2–3 列（CSV 格式）：文件夹名, 父文件夹名, 说明（可选）。第一行是根文件夹，省略父文件夹列即视为根节点的子节点。')
para('示例：')
codeblock('总装配\n泵壳组件,总装配\n转子组件,总装配\n叶轮,转子组件\n主轴,转子组件')
numbered('单击【项目】→【从层级文件创建文件夹树】。')
numbered('上传您的文本文件（或直接粘贴文本内容）。')
figure('12_hierarchy_import_empty.png', '图 12-2　层级导入对话框（初始状态）')
numbered('单击【验证】。如果同一文件夹名称在文件中重复出现，默认会报错并列出冲突所在行。')
figure('14_hierarchy_import_error.png', '图 12-3　未勾选自动编号时，重复文件夹名被视为错误')
numbered('若您的数据本来就存在同名文件夹（例如多个总成下各有一个同名的"螺栓"子件），可以勾选【自动为重复文件夹名添加编号（01、02、03…）】后重新验证——所有重名文件夹会自动依次编号为"螺栓 01"、"螺栓 02"……全部创建成功，不再报错。')
figure('15_hierarchy_import_dedupe_ok.png', '图 12-4　勾选自动编号后，重复名称被编号并通过验证')
numbered('验证通过后会显示树形预览及将创建的节点数量。')
numbered('单击【创建节点树】，所有文件夹立即出现在树形视图中。')

h2('12.9  在应用内下载本手册')
para('本手册也内置在程序中，无需另外索取文件。')
numbered('单击【项目】→【下载用户手册 (.docx)】。')
numbered('浏览器会下载最新版的本手册（Word 格式），方便离线查阅或打印。')
pb()


# 13 云同步与邮件
h1('13  云同步与邮件')
h2('13.1  使用 OneDrive 同步存储')
para('DMS 本身不直接连接 OneDrive 账户，而是通过"把存储文件夹设置在 OneDrive 同步目录内"的方式，借助 OneDrive 客户端自带的同步能力实现多设备访问和云端备份。')
numbered('在电脑上安装并登录 Microsoft OneDrive 客户端，确认本地同步文件夹位置（一般在"OneDrive"文件夹内）。')
numbered('在该同步文件夹内新建一个子文件夹，例如 DMS_Storage。')
numbered('在 DMS 中将存储路径改为指向这个新建的子文件夹（首次设置见 1.3 节；之后可在设置中更改）。')
numbered('之后对 DMS 数据的所有修改都会被 OneDrive 自动同步到云端及您的其他设备。')
para('项目菜单的"文档下载"分组中提供了详细的图文设置指南（中英文各一份），可下载后按步骤操作：')
bullet('OneDrive Setup Guide (.txt) — English')
bullet('OneDrive 设置指南 (.txt) — 中文')
note('存储路径迁移到 OneDrive 文件夹后，请确保该文件夹已完成同步（云图标显示为已同步状态），再关闭电脑或断网，以免数据只留在本地未及时上传。')

h2('13.2  邮件发送设置')
para('配置好 SMTP 邮件服务器后，就可以直接从文档卡片一键把文件作为附件发送邮件，无需打开邮箱网页手动上传。')
numbered('单击【项目】→【邮件发送设置】。')
numbered('单击顶部的邮箱服务商快捷按钮（Gmail / Yahoo / Outlook / iCloud / QQ邮箱 / 网易163）可自动填入对应的服务器地址和端口。')
numbered('填写用户名（邮箱地址）和密码。')
bullet('使用 Gmail 时需要在 Google 账户设置中生成"应用专用密码"，而不是登录密码。')
numbered('如需使用 SSL/TLS（端口 465）而非默认的 STARTTLS（端口 587），请勾选对应选项。')
numbered('可选填写发件人地址（默认与用户名相同）。')
numbered('单击【发送测试邮件】确认配置无误，再单击【保存设置】。')
figure('20_email_settings.png', '图 13-1　邮件发送设置（SMTP）对话框')
note('未配置 SMTP 时，在 Mac 上单击文档的"发送邮件"按钮会退而改为打开系统自带的"邮件"应用并附加该文件；在 Windows 上则需要先完成 SMTP 配置才能发送。')
pb()


# 14 密码
h1('14  密码保护')
para('您可以为 DMS 设置密码，确保只有授权用户才能访问数据，尤其是在启用了局域网访问、远程上传或保存了网站登录信息（第 3.5 节）之后，强烈建议设置密码。')
para('设置密码：')
numbered('单击【项目】→【设置密码】。')
numbered('输入并确认您选择的密码，然后单击【设置】。')
numbered('下次启动 DMS 时，在主界面加载前会显示登录界面。')
figure('19_password_dialog.png', '图 14-1　设置／移除密码对话框')
para('取消密码：')
numbered('单击【项目】→【设置密码】（已设密码时显示为此项）。')
numbered('输入当前密码以确认身份，然后将新密码字段留空并提交，即可取消密码保护。')
para('退出登录：单击标题栏中的【退出 DMS】按钮。DMS 返回登录界面；服务器继续运行，仅浏览器会话结束。')
note('密码以加盐哈希方式存储在服务器端（SHA-256，100,000 次迭代），不以明文保存。文件夹的网站登录密码（第 3.5 节）则使用另一套独立的可逆加密机制存储，二者互不影响。')
pb()

# 15 快捷键
h1('15  键盘快捷键')
table(['操作','快捷键'],[
    ['确认文件夹重命名 / 新文件夹名称','Enter'],
    ['取消重命名 / 新建文件夹','Escape'],
    ['开始重命名文件夹','双击文件夹名称'],
    ['打开文件夹右键菜单','右键单击文件夹'],
    ['关闭文档查看器','Escape 或单击【关闭】'],
    ['粘贴剪贴板图片到当前文件夹','Ctrl + V（Windows）／ Cmd + V（Mac）'],
    ['复制选中文本','Ctrl + C'],
    ['全选文本框内容','Ctrl + A'],
    ['撤销（文本框内）','Ctrl + Z'],
],[3.5,3.0])
pb()

# 16 故障排除
h1('16  常见问题与解决方法')
table(['问题','解决方法'],[
    ['Windows SmartScreen 阻止 DMS.exe 运行',
     '单击【更多信息】，然后单击【仍要运行】。此提示是因为文件未经商业签名，程序本身是安全的。'],
    ['macOS 提示"无法验证开发者"',
     '前往【系统设置】→【隐私与安全性】，找到相关提示并点击【仍要打开】。'],
    ['浏览器未自动打开',
     '手动打开浏览器，访问 http://127.0.0.1:8765。'],
    ['浏览器显示无法访问此网站 / 点击操作后提示"Failed to fetch"',
     '请确认后台的 DMS 程序仍在运行（Windows 下为控制台窗口或托盘图标；Mac 下为菜单栏/程序坞图标）。如已退出，请重新启动程序。若刚刚点击过"退出 DMS"，说明服务器已正常关闭，这是预期行为。'],
    ['点击【自动安装 deepface】后长时间显示"安装中"',
     '这是正常现象——安装在后台进行，下载 TensorFlow 等组件在网络较慢时可能需要十几分钟。可以继续使用应用其他功能，安装完成后按钮会自动恢复正常状态，无需刷新页面。'],
    ['OCR 按钮显示为灰色或提示 Tesseract 不可用',
     'Tesseract OCR 是可选组件，需单独安装。Windows 请从 UB-Mannheim 提供的安装包安装；Mac 可执行 brew install tesseract，安装后重启 DMS。'],
    ['中文文字未能正确识别',
     '请安装 Tesseract 的简体中文语言包（chi_sim.traineddata），详见 Tesseract 文档。'],
    ['GPS 位置显示为坐标而非城市名称',
     '地址反查服务需要网络连接，请检查您的网络状态。'],
    ['上传的文件未出现在文档列表中',
     '请刷新浏览器页面（F5）。文件可能已成功上传，但页面未自动更新。'],
    ['找不到之前上传或已删除的文档',
     '请使用【搜索】标签按文件名或内容搜索；已删除的文档可能在"Not Show in Tree"回收站文件夹中（见 3.2 节）。'],
    ['DMS 启动后立即关闭',
     '请确保电脑上的 8765 端口未被其他程序占用，重启电脑后再试。'],
    ['生成数据手册 / 合并文档 PDF 失败或出现"Unsupported type"占位页',
     '合并功能仅支持 PDF 和图片格式；Word/Excel/PPT 等其他格式请先另存为 PDF 再上传合并（见第 9 章）。'],
    ['存储文件夹里出现很多 .dms 文件',
     '这是正常的自动备份行为，见第 12.4 节；超过 10 个后旧文件会自动归档到"Not Shown in The Tree"子文件夹，不会无限增长。'],
],[2.5,4.0])
doc.add_paragraph()
h2('获取帮助')
para('如遇到上述列表中未包含的问题，请联系应用开发者并提供以下信息：')
bullet('问题发生时您正在执行的操作描述。')
bullet('屏幕上显示的任何错误信息。')
bullet('您的操作系统版本（例如 Windows 11 或 macOS 15）。')
pb()

# 快速参考卡片
h1('快速参考卡片')
table(['任务','操作方法'],[
    ['新建文件夹','选择父文件夹 → 单击【新建文件夹】→ 输入名称 → Enter'],
    ['上传文档','选择文件夹 → 【上传文件】或【不生成年月夹】→ 选择文件'],
    ['粘贴截图','选择文件夹 → Ctrl+V / Cmd+V，或单击【粘贴截图】'],
    ['查看文档','单击文档名称'],
    ['对文档执行 OCR','打开查看器 → 提取文字（OCR）→ 保存并启用搜索'],
    ['搜索所有文档','单击【搜索】标签 → 输入关键词'],
    ['保存文件夹的网站登录信息','铅笔图标 → 登录信息 → 填写网站/账号/密码 → 完成'],
    ['生成合并 PDF（数据手册）','【数据手册】标签 → 勾选文档 → 生成数据手册'],
    ['合并文件为单个 PDF','选择文件夹 → 合并文档 → 选择文件 → 合并'],
    ['标记照片中的人脸','标记人脸 → 勾选照片 → 标记选中照片 → 命名'],
    ['发送文档到手机','文档卡片 → 发送至手机图标 → 输入号码'],
    ['远程接收他人上传的文件','桌面启动器 → 远程上传 → 对方扫码'],
    ['移动文件夹','拖放文件夹到树形视图中的新位置'],
    ['设置序列号','单击文件夹铅笔图标 → 输入 SN → 完成'],
    ['导出索引/完整/选定备份','项目菜单 → 导出索引/完整/选定备份'],
    ['导出元数据为 CSV','项目菜单 → 导出 CSV'],
    ['设置访问密码','项目菜单 → 设置密码'],
    ['配置邮件发送','项目菜单 → 邮件发送设置'],
],[2.5,4.5])

out = str(HERE / 'PMS用户手册_更新版.docx')
doc.save(out)
print('Saved to', out)
