"""Generate PMS User Manual as a Word document.

Run with: python3 generate_manual.py
Screenshots are pulled from manual_assets/. The app's interface shown in the
screenshots is Chinese (DMS ships with a 中文/EN toggle in the top bar), but
the manual text and captions below are English — see section 2.1.
"""
from pathlib import Path
from docx import Document
from docx.shared import Pt, RGBColor, Inches, Cm
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.style import WD_STYLE_TYPE
from docx.oxml.ns import qn
from docx.oxml import OxmlElement
import datetime

HERE = Path(__file__).resolve().parent
ASSETS = HERE / "manual_assets"

doc = Document()

# ── Page margins ──────────────────────────────────────────────────────────────
section = doc.sections[0]
section.page_width  = Inches(8.5)
section.page_height = Inches(11)
section.left_margin = section.right_margin = Inches(1.2)
section.top_margin  = section.bottom_margin = Inches(1)

# ── Helpers ───────────────────────────────────────────────────────────────────
def h1(text):
    p = doc.add_heading(text, level=1)
    p.runs[0].font.color.rgb = RGBColor(0x1E, 0x40, 0xAF)   # blue-700

def h2(text):
    p = doc.add_heading(text, level=2)
    p.runs[0].font.color.rgb = RGBColor(0x1D, 0x4E, 0x89)

def h3(text):
    p = doc.add_heading(text, level=3)
    p.runs[0].font.color.rgb = RGBColor(0x2E, 0x86, 0xAB)

def para(text, bold=False, italic=False):
    p = doc.add_paragraph(text)
    if bold or italic:
        for run in p.runs:
            run.bold  = bold
            run.italic = italic
    return p

def bullet(text, level=0):
    p = doc.add_paragraph(text, style='List Bullet')
    p.paragraph_format.left_indent = Inches(0.25 * (level + 1))
    return p

def numbered(text, level=0):
    p = doc.add_paragraph(text, style='List Number')
    p.paragraph_format.left_indent = Inches(0.25 * (level + 1))
    return p

def tip(text):
    p = doc.add_paragraph()
    p.paragraph_format.left_indent  = Inches(0.4)
    p.paragraph_format.right_indent = Inches(0.4)
    run = p.add_run("Tip: ")
    run.bold = True
    run.font.color.rgb = RGBColor(0x05, 0x6F, 0x00)
    r2 = p.add_run(text)
    r2.font.color.rgb = RGBColor(0x33, 0x33, 0x33)
    # light green shading
    pPr = p._p.get_or_add_pPr()
    shd = OxmlElement('w:shd')
    shd.set(qn('w:val'), 'clear')
    shd.set(qn('w:color'), 'auto')
    shd.set(qn('w:fill'), 'E6F4EA')
    pPr.append(shd)

def note(text):
    p = doc.add_paragraph()
    p.paragraph_format.left_indent  = Inches(0.4)
    p.paragraph_format.right_indent = Inches(0.4)
    run = p.add_run("Note: ")
    run.bold = True
    run.font.color.rgb = RGBColor(0x92, 0x4E, 0x00)
    r2 = p.add_run(text)
    pPr = p._p.get_or_add_pPr()
    shd = OxmlElement('w:shd')
    shd.set(qn('w:val'), 'clear')
    shd.set(qn('w:color'), 'auto')
    shd.set(qn('w:fill'), 'FFF8E1')
    pPr.append(shd)

def codeblock(text):
    p = doc.add_paragraph()
    p.paragraph_format.left_indent = Inches(0.4)
    run = p.add_run(text)
    run.font.name = 'Courier New'
    run.font.size = Pt(9)
    pPr = p._p.get_or_add_pPr()
    shd = OxmlElement('w:shd')
    shd.set(qn('w:val'), 'clear')
    shd.set(qn('w:color'), 'auto')
    shd.set(qn('w:fill'), 'F3F4F6')
    pPr.append(shd)

def add_table(headers, rows, col_widths=None):
    t = doc.add_table(rows=1 + len(rows), cols=len(headers))
    t.style = 'Table Grid'
    # header row
    for i, h in enumerate(headers):
        cell = t.rows[0].cells[i]
        cell.text = h
        run = cell.paragraphs[0].runs[0]
        run.bold = True
        run.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
        # blue fill
        tcPr = cell._tc.get_or_add_tcPr()
        shd = OxmlElement('w:shd')
        shd.set(qn('w:val'), 'clear')
        shd.set(qn('w:color'), 'auto')
        shd.set(qn('w:fill'), '1E40AF')
        tcPr.append(shd)
    # data rows
    for ri, row in enumerate(rows):
        for ci, val in enumerate(row):
            t.rows[ri + 1].cells[ci].text = val
    if col_widths:
        for i, w in enumerate(col_widths):
            for row in t.rows:
                row.cells[i].width = Inches(w)
    doc.add_paragraph()

def figure(filename, caption, width=6.0):
    """Embed a screenshot (from manual_assets/) centered, with a small caption below."""
    path = ASSETS / filename
    if not path.exists():
        note(f'(screenshot missing: {filename})')
        return
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run()
    run.add_picture(str(path), width=Inches(width))
    cap = doc.add_paragraph()
    cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = cap.add_run(caption)
    r.font.size = Pt(9); r.font.color.rgb = RGBColor(0x70, 0x70, 0x70); r.italic = True
    doc.add_paragraph()

def page_break():
    doc.add_page_break()

# ══════════════════════════════════════════════════════════════════════════════
# COVER PAGE
# ══════════════════════════════════════════════════════════════════════════════
p = doc.add_paragraph()
p.alignment = WD_ALIGN_PARAGRAPH.CENTER
run = p.add_run("\n\n\n")

p = doc.add_paragraph()
p.alignment = WD_ALIGN_PARAGRAPH.CENTER
run = p.add_run("Photo / Document Management System")
run.font.size = Pt(32)
run.font.bold = True
run.font.color.rgb = RGBColor(0x1E, 0x40, 0xAF)

p = doc.add_paragraph()
p.alignment = WD_ALIGN_PARAGRAPH.CENTER
run = p.add_run("PMS  (app name: DMS)")
run.font.size = Pt(16)
run.font.color.rgb = RGBColor(0x60, 0x7D, 0x8B)

doc.add_paragraph()
p = doc.add_paragraph()
p.alignment = WD_ALIGN_PARAGRAPH.CENTER
run = p.add_run("User Manual")
run.font.size = Pt(26)
run.font.bold = True
run.font.color.rgb = RGBColor(0x1D, 0x4E, 0x89)

doc.add_paragraph()
doc.add_paragraph()
p = doc.add_paragraph()
p.alignment = WD_ALIGN_PARAGRAPH.CENTER
run = p.add_run(f"Version 2.0  |  {datetime.date.today().strftime('%B %Y')}")
run.font.size = Pt(12)
run.font.color.rgb = RGBColor(0x55, 0x55, 0x55)

page_break()

# ══════════════════════════════════════════════════════════════════════════════
# TABLE OF CONTENTS
# ══════════════════════════════════════════════════════════════════════════════
h1("Table of Contents")
toc_entries = [
    ("1", "Getting Started"), ("1.1", "System Requirements"), ("1.2", "Download & Install"),
    ("1.3", "First Launch: Choosing a Project"), ("1.4", "Windows Blocked This App? Unblock It"),
    ("2", "Understanding the Interface"), ("2.1", "Main Layout Overview"),
    ("2.2", "Sidebar Tabs"), ("2.3", "Main Content Panel"),
    ("3", "Managing Folders (Tree)"), ("3.1", "Creating Folders"),
    ("3.2", "Renaming and Deleting Folders"), ("3.3", "Reordering and Moving Folders"),
    ("3.4", "Serial Numbers (SN)"),
    ("3.5", "Description: Voice Input, AI Cleanup, and PDF Export"),
    ("3.6", "Login Credentials (can save multiple sets)"), ("3.7", "Text Tree Preview"),
    ("3.8", "Batch Folder Operations (Multi-select)"), ("3.9", "Setting a Photo Root Folder"),
    ("4", "Working with Documents"), ("4.1", "Uploading Files"), ("4.2", "Uploading a Folder"),
    ("4.3", "Pasting Screenshots"), ("4.4", "Linking Existing Documents"),
    ("4.5", "Viewing a Document"), ("4.6", "Downloading, Sending, and Deleting Documents"),
    ("4.7", "Duplicate Detection"), ("4.8", "Barcode Scanning"),
    ("4.9", "Ask AI About a Document, or One-Click Polish"), ("4.10", "Convert to PDF (Batch)"),
    ("4.11", "On-Disk Filenames and Recovering Deletions"),
    ("4.12", "Batch Document Actions (Multi-select: Download, Batch Rename, Find Near-Duplicates)"),
    ("5", "OCR - Extract Text from Documents"), ("5.1", "How OCR Works"),
    ("5.2", "Running OCR"), ("5.3", "Saving OCR Text for Search"),
    ("6", "Key Parameter Extraction"), ("6.1", "What Are Key Parameters?"),
    ("6.2", "Extracting Parameters from a Document"), ("6.3", "Managing Global Key Parameters"),
    ("6.4", "Configuring an AI Provider (Gemini / DeepSeek)"),
    ("6.5", "Parameter Trend Charts and Snapshots"),
    ("6.6", "Batch AI Parameter Extraction (Multi-select Documents)"),
    ("7", "Searching Documents"), ("7.1", "Basic Search"), ("7.2", "Using Filters"),
    ("7.3", "Opening and Jumping to Results"),
    ("8", "Generating a Databook (PDF / Word)"), ("8.1", "Selecting Documents (PDF mode)"),
    ("8.2", "Generating the PDF"), ("8.3", "Auto-Generating a Word Databook from a Folder"),
    ("9", "Merging Documents into a Single PDF"),
    ("10", "Face Recognition"), ("10.1", "Installing the Face Recognition Component"),
    ("10.2", "Tagging Faces"), ("10.3", "Browsing by Person / Auto-Matching"),
    ("11", "Remote and Mobile Upload"), ("11.1", "Sending a Single Document to a Phone"),
    ("11.2", "Remote Upload (QR-Code Batch Intake)"),
    ("12", "Project Management"), ("12.1", "Export Index Backup (METADATA)"),
    ("12.2", "Export Full Backup (COMP)"), ("12.3", "Export Selected Folders (PART)"),
    ("12.4", "Backup Retention Rules"), ("12.5", "Import a Project (.dms)"),
    ("12.6", "CSV Export"), ("12.7", "Batch ZIP Import"),
    ("12.8", "Hierarchy Import (with auto de-duplication)"),
    ("12.9", "Downloading This Manual from Inside the App"),
    ("13", "Cloud Sync and Email"), ("13.1", "Using OneDrive to Sync Storage"),
    ("13.2", "Email Sending Setup"),
    ("14", "Password Protection"), ("15", "Keyboard Shortcuts"),
    ("16", "Working with Multiple Projects at Once"),
    ("17", "Combining Another Project In"),
    ("18", "Folder-Level \u201cAsk AI\u201d: Batch Document Q&A and PDF Export"),
    ("19", "Icon View: Pop-Out Window and Batch Actions"),
    ("20", "Troubleshooting"),
    ("", "Quick Reference Card"),
]
for num, title in toc_entries:
    p = doc.add_paragraph()
    p.paragraph_format.left_indent = Inches(0.4) if "." in num else Inches(0)
    label = (f"{num}  {title}") if num else title
    run = p.add_run(label)
    if "." not in num and num:
        run.bold = True

page_break()

# ══════════════════════════════════════════════════════════════════════════════
# 1  GETTING STARTED
# ══════════════════════════════════════════════════════════════════════════════
h1("1  Getting Started")

h2("1.1  System Requirements")
add_table(
    ["Item", "Requirement"],
    [
        ["Operating System", "Windows 10 / 11 (64-bit) or macOS 12 or later"],
        ["RAM", "4 GB minimum, 8 GB recommended (8 GB+ recommended if you enable face recognition)"],
        ["Disk Space", "About 150-250 MB for the app itself; roughly another 200 MB if you install the optional face-recognition component; plus space for your documents"],
        ["Display", "1280 x 720 or larger"],
        ["Internet", "Not required for day-to-day use (runs fully offline). Only needed for GPS address lookup, face recognition setup, sending email, or remote upload."],
    ],
    col_widths=[1.8, 4.2]
)

h2("1.2  Download & Install")
para("The installer is named DMS (the app's internal name; the product name shown in the interface is \u201cPhoto Management System / PMS\u201d). Follow the steps for your platform.")
h2("Windows")
numbered("Receive or download the file DMS.zip from your contact (e.g. email, WeChat, USB drive).")
numbered("Right-click DMS.zip and choose Extract All...")
numbered("Choose a destination folder, for example C:\\DMS, and click Extract.")
numbered("Open the extracted folder and find the file DMS.exe.")
numbered("Double-click DMS.exe to launch the application.")
note("Windows may show a SmartScreen warning the first time you run DMS.exe. Click More info, then Run anyway. This appears because the file is not commercially code-signed; the program itself is safe. If instead the app doesn't respond at all when you double-click it, see section 1.4 - the file is most likely blocked, not broken.")
h2("Mac")
numbered("Receive DMS.app (or a .zip file containing it), unzip it, and drag it into your Applications folder (or anywhere you like).")
numbered("Double-click DMS.app to launch it.")
numbered("The first time you open it, macOS may say \u201ccannot verify the developer.\u201d Go to System Settings -> Privacy & Security, find the notice there, and click Open Anyway.")
tip("No other software needs to be installed (except the optional face-recognition component, see section 10). DMS bundles its own copy of Python and every library it needs.")
doc.add_paragraph()

h2("1.3  First Launch: Choosing a Project")
para("Double-clicking DMS.app (Mac) or DMS.exe (Windows) first shows a small \u201cChoose a project\u201d window, before the main interface loads:")
numbered("Continue with last project - reopens whichever project you were using the last time you quit normally (DMS remembers the path automatically on every normal quit). This button is greyed out the first time you use DMS, or if that project can no longer be found.")
numbered("Select a project... - opens a system folder picker so you can choose any existing DMS project folder on your computer.")
numbered("New project... - creates a brand-new, empty project folder and switches to it immediately (unlike older versions, DMS no longer opens with pre-loaded demo data).")
para("Once a project is chosen, DMS starts its built-in web server on your computer (searching for a free port starting at 8765) and opens your default browser to that address, loading the main PMS interface.")
tip("Need to work on two different projects at the same time? You don't have to close the first one - see chapter 16, \u201cWorking with Multiple Projects at Once.\u201d")
page_break()

h2("1.4  Windows Blocked This App? Unblock It")
para("A DMS.zip / DMS.exe you received over the network (email, WeChat, a cloud-drive link, etc.) gets automatically tagged by Windows as \u201cdownloaded from another computer,\u201d which can cause repeated security prompts, or simply no response at all when you double-click the file. The first time you use a given copy of the app, unblock it as follows (this is a one-time step per download - a newly downloaded version may need it again):")
numbered("If a security warning appears when you launch DMS.exe (or double-clicking it does nothing for a long time)...")
numbered("Right-click DMS.exe.")
numbered("Choose Properties from the right-click menu.")
numbered("At the bottom of the General tab, under Security, check the Unblock checkbox.")
numbered("Click Apply.")
numbered("Click OK to close the window.")
figure("25_windows_unblock.png", "Figure 1-1  DMS.exe Properties window: check \u201cUnblock,\u201d then click \u201cApply,\u201d then \u201cOK\u201d")
note("This is the same underlying Windows security mechanism as the SmartScreen \u201cRun anyway\u201d prompt in section 1.2, showing up at a different point - unblocking the file here usually makes both go away. If \u201cOpen in native app\u201d (section 4.5) reports a document as locked/read-only, it's the same cause; DMS automatically tries to unlock the file first, so you normally won't need to handle this by hand.")

page_break()

# ══════════════════════════════════════════════════════════════════════════════
# 2  INTERFACE
# ══════════════════════════════════════════════════════════════════════════════
h1("2  Understanding the Interface")

h2("2.1  Main Layout Overview")
para("The DMS window is divided into three main areas:")
add_table(
    ["Area", "Location", "Purpose"],
    [
        ["Header Bar", "Top of the page", "Storage path, total indexed document count, the Project menu, and the logout button."],
        ["Sidebar", "Left panel", "Four tabs: Tree, Search, Databook, and Faces."],
        ["Main Content Panel", "Right / centre area", "Shows the documents in the currently selected folder, or the content of whichever sidebar tab is active."],
    ],
    col_widths=[1.5, 1.8, 3.3]
)
tip("Drag the vertical divider between the sidebar and the content panel to resize either side. Double-click the divider to reset to the default width.")
tip("The storage path shown in the middle of the header bar is itself a button - click it anytime to open a Change storage location dialog and point DMS at a different folder on your computer, no restart and no need to go back through the section 1.3 startup screen (see section 1.3 for the first-time setup).")

figure("01_overview.png", "Figure 2-1  Overall layout: folder tree on the left, folder details and action buttons on the right")

tip("Almost every text field in the app (folder/document names, serial numbers, login credentials, descriptions, the search box, the Ask-AI question box, and more) has a small microphone icon next to it - click it, speak, and the recognized text is appended at the cursor; click again to stop. You can switch the recognition language between Chinese and English, and it needs no AI key at all - it runs on your browser's own built-in speech recognition (Chrome / Edge recommended).")

h2("2.2  Sidebar Tabs")
add_table(
    ["Tab", "Chinese Label", "What It Does"],
    [
        ["Tree", "\u6811\u5f62", "Browse and manage your folder hierarchy. This is the primary navigation view."],
        ["Search", "\u641c\u7d22", "Full-text and metadata search across all documents."],
        ["Databook", "\u6570\u636e\u624b\u518c", "Select documents and generate a professional combined PDF or Word document (see chapter 8)."],
        ["Faces", "\u4eba\u8138", "Face recognition and browsing photos by person (requires an optional component - chapter 10)."],
    ],
    col_widths=[1.0, 1.2, 4.4]
)

h2("2.3  Main Content Panel")
para("When you click a folder in the Tree tab, the right panel shows:")
bullet("Folder name, serial number (SN), description, and saved login credentials (if any) at the top.")
bullet("Action buttons: Tag Faces, Link Existing Document, Merge Documents, Paste Screenshot, Upload Files, Upload (no year/month folders), Upload Folder.")
bullet("A card for each document in the folder, showing file name, size, type icon, upload date, and action icons (SN, link, send to phone, email, delete).")

page_break()

# ══════════════════════════════════════════════════════════════════════════════
# 3  FOLDERS
# ══════════════════════════════════════════════════════════════════════════════
h1("3  Managing Folders (Tree)")
para("DMS organizes your documents into a tree of folders, similar to a file explorer, with extra features: serial numbers, descriptions, login credentials, and document counts.")

h2("3.1  Creating Folders")
numbered("Click a folder in the tree to select it (the new folder will be created as a child of the selected folder).")
numbered("Click New Folder in the sidebar toolbar, or right-click the selected folder and choose New Folder.")
numbered("A text field appears. Type the folder name and press Enter to confirm, or press Escape to cancel.")
tip("Folders can be nested to any depth - there is no limit.")

h2("3.2  Renaming and Deleting Folders")
para("Rename:")
bullet("Double-click the folder name, or right-click and choose Rename.")
bullet("Edit the name and press Enter to save.")
para("Delete:")
bullet("Right-click the folder, choose Delete, and confirm.")
bullet("Documents inside are not removed from disk immediately - they move into a hidden holding folder named \u201cNot Show in Tree\u201d (a soft-delete / recycle-bin mechanism).")
bullet("To recover them, find the \u201cNot Show in Tree\u201d node in the tree and re-link or move the documents elsewhere.")
note("Only deleting a document directly inside the \u201cNot Show in Tree\u201d folder permanently removes the file from disk - that action cannot be undone. This is a different folder from the \u201cNot Shown in The Tree\u201d archive folder mentioned in section 12.4 for old .dms backups: the former lives inside the document tree and holds soft-deleted documents; the latter lives at the root of the storage folder and holds backup files beyond the retention limit.")

h2("3.3  Reordering and Moving Folders")
para("Reorder siblings (folders at the same level):")
bullet("Hover over a folder to reveal up/down arrow buttons.")
bullet("Click the up arrow to move it up, the down arrow to move it down.")
para("Move to a different parent:")
bullet("Click and drag the folder, dropping it onto the target parent folder (highlighted while dragging).")
bullet("You can also drop it directly above or below another folder to place it before or after that folder.")

h2("3.4  Serial Numbers (SN)")
para("Every folder can have an optional Serial Number (SN) used to tag assemblies, batches, or project identifiers.")
bullet("Click the pencil icon on a folder to enter edit mode.")
bullet("Enter a value in the Serial Number field (e.g. INS-2026-001).")
bullet("The SN is shown next to the folder name in the tree.")
figure("02_folder_with_sn.png", "Figure 3-1  A folder with a serial number (Homeowner's Insurance - INS-2026-001)")
figure("03_folder_edit_mode.png", "Figure 3-2  Folder edit mode: name, serial number, and login-credential fields")

h2("3.5  Description: Voice Input, AI Cleanup, and PDF Export")
para("Every folder (and every document) has its own Description area - not just a one-line note, but a full writing and organizing toolkit: voice dictation, transcribing an uploaded recording, AI cleanup/polish, a Chinese typo checker, and finally one-click export to a PDF that keeps updating in place. What started as a plain text box for folders has been upgraded to the exact same full-featured panel single documents already had.")
bullet("Voice Input - click it and speak into your microphone; the recognized text is appended to the end of the description automatically. Switch between Chinese and English recognition. This runs in the browser and needs no AI configured.")
bullet("Transcribe an uploaded recording - pick an audio file (e.g. a voice memo from your phone: .m4a, also .mp3/.wav/.aac, etc.), and AI (Gemini) transcribes it and appends the text - a second way to get text in, alongside live dictation, especially useful when you recorded on your phone earlier and want to process it later. Requires a Gemini API key the first time (see below).")
bullet("AI Cleanup - choose from \u201cOrganize / Polish wording / Make concise,\u201d then click Clean up with AI; the AI turns spoken or rough notes into readable prose while preserving all the information. The result is shown as a preview first - it only replaces your text once you click Apply, never silently.")
bullet("Check for Typos - has AI check the description for typos, punctuation, and misused idioms (Chinese text); apply fixes one at a time or all at once.")
bullet("Save & Make Searchable - saves the current description into the index so it can be found later by search.")
bullet("Save as New Document / Update Description Document - saves the description as a .txt document in the same folder; on later saves the button becomes \u201cUpdate Description Document\u201d and overwrites the same file instead of piling up copies.")
bullet("Save as PDF / Update PDF - turns the description into a properly formatted PDF, also stored in the same folder; editing the description and clicking again updates that same PDF (same filename) rather than creating a new one each time - handy as a document you can print or share at any point.")
figure("23_folder_description_panel.png", "Figure 3-3  A folder's description panel (example: a \u201cBaby's Birth\u201d folder)")
tip("Scenario 1 - a baby-book folder: create a \u201cBaby's Birth\u201d folder and write the whole story in the description - what time you rushed to the hospital, the hospital and doctor's names, how it felt holding the baby for the first time... then add the birth certificate, footprint card, and ultrasound scans as documents in the same folder. That one narrative turns a handful of scattered documents and photos into a complete keepsake.")
tip("Scenario 2 - voice-logging routine measurements: after taking a blood-pressure or blood-sugar reading, just say the result out loud into a voice-memo app on your phone - record as many times as you measure, saved on the phone for now. When convenient, transfer those recordings to your computer (AirDrop, WeChat file transfer, a cable, whatever works), open the description panel for the relevant folder or document, and click Transcribe Recording once per file; they're transcribed and appended in turn. Finish with one click of Clean up with AI (choose \u201cOrganize\u201d) to turn several loose readings into one properly organized record, then Save as PDF to keep it.")
note("Transcribing a recording, AI cleanup, and the typo checker all need a Gemini API key configured first: until then, a \u201cConfigure AI (Gemini)\u201d link appears in their place - click it, paste your key when prompted, and save (the key is stored only on this machine, encrypted). Voice Input (live microphone recognition) needs no key at all and works immediately.")

h2("3.6  Login Credentials (can save multiple sets)")
para("If a folder corresponds to a website or online account that requires signing in (an insurance company's customer portal, a hospital patient portal, and so on), you can save that site's login details directly on the folder for quick access later. A folder is no longer limited to one set of credentials - add as many sets as you need, for example a \u201cdealer portal\u201d and a separate \u201cmanufacturer parts portal\u201d for the same piece of equipment.")
numbered("Click the pencil icon on the folder to enter edit mode.")
numbered("In the Login Credentials (optional, multiple sets allowed) area, fill in: website link, account/username, and password.")
numbered("Need to save another set? Click Add Login Credentials to add another group of the same three fields; each group has an X button to delete it.")
numbered("Click Done to save.")
figure("35_multi_login_credentials.png", "Figure 3-4  Edit mode with two login-credential sets added (the microphone icon next to each field lets you dictate it - see section 2.1)")
para("In view mode, each credential set shows its own three buttons, with the account name visible directly so you can tell the sets apart without decrypting anything:")
bullet("Open Website - opens the site in a new tab and automatically copies the account name to the clipboard, ready to paste into the username field.")
bullet("Copy Account - copies the username to the clipboard in one click, instead of selecting text out of a masked field.")
bullet("Copy Password - copies the password to the clipboard, ready to paste into the password field.")
figure("36_multi_login_view.png", "Figure 3-5  View mode with two login-credential sets shown side by side")
note("For security, passwords are stored encrypted on the server (the key lives in your local user directory and is not synced along with the storage folder), and are only decrypted briefly when you click Open Website, Copy Account, or Copy Password - never sent along with ordinary folder-tree reads. Because passwords can otherwise be read by other devices on the same local network when no access password is set, we strongly recommend setting the access password described in chapter 14 before relying on this feature.")

h2("3.7  Text Tree Preview")
para("If you need to copy or export the entire folder structure as plain text (for example, to paste into a chat app or a document), use the text tree feature.")
numbered("Click the Text Tree button at the top-left of the Tree tab.")
numbered("The preview window shows the complete folder hierarchy as an ASCII tree.")
numbered("Check Show Serial Numbers to append each folder's SN and document count next to its name.")
numbered("Click Copy to copy the text to the clipboard, or Export .txt to save it as a text file.")
figure("17_text_tree.png", "Figure 3-6  Text tree preview window")

h2("3.8  Batch Folder Operations (Multi-select)")
para("Need to delete or export several folders at once? You no longer have to do it one at a time.")
numbered("Click Multi-select Folders at the top-left of the Tree tab.")
numbered("The toolbar switches to multi-select mode, and a checkbox appears in front of every folder name.")
numbered("Check the folders you want to act on (you can check folders across different levels and different parents).")
numbered("Click Download to export the checked folders individually, or Delete to soft-delete them all at once (moved into \u201cNot Show in Tree\u201d - see section 3.2).")
numbered("Click Cancel Multi-select to exit the mode.")
figure("27_folder_multiselect.png", "Figure 3-7  Folder multi-select mode: one folder checked, ready to batch-download or batch-delete")

h2("3.9  Setting a Photo Root Folder")
para("Section 4.1 explains that uploading a photo automatically creates “year/month” subfolders under the current folder. If you'd rather have every photo in the whole project archived by year/month under one fixed location - instead of scattered under whichever folder you happened to upload into - you can designate a “photo root” folder:")
numbered("Right-click the folder you want to use as the root, and choose Set as photo root folder.")
numbered("That folder gets a small camera-icon marker in the tree.")
numbered("From then on, uploading a photo anywhere in the tree (as long as you're not using the “current folder” upload mode) creates its year/month subfolders under this designated root, not under whatever folder you uploaded into.")
numbered("To undo it, right-click the marked folder and choose Unset photo root folder.")
figure("39_photo_root_context_menu.png", "Figure 3-8  Folder right-click menu: Set/Unset photo root folder")
tip("Handy for funneling every photo from every device and every folder into one central “Photo Library” folder organized by year/month, while each business folder keeps only the documents relevant to it, unmixed with photos.")
page_break()

# ══════════════════════════════════════════════════════════════════════════════
# 4  DOCUMENTS
# ══════════════════════════════════════════════════════════════════════════════
h1("4  Working with Documents")

h2("4.1  Uploading Files")
numbered("Select the destination folder in the Tree tab.")
numbered("Click Upload Files (default behaviour, see below), or Upload (no year/month folders).")
numbered("In the file picker, select one or more files and click Open.")
numbered("A progress indicator appears at the top of the screen showing how many files are uploading.")
numbered("When complete, the new document cards appear in the panel.")
para("The difference between the two upload buttons:")
add_table(
    ["Button", "Behaviour"],
    [
        ["Upload Files", "If a file is recognized as a photo with a readable capture date (EXIF), DMS automatically creates \u201cYear/Month\u201d sub-folders under the current folder and files it there; non-photos, or files with no readable date, go straight into the current folder."],
        ["Upload (no year/month folders)", "Every file goes straight into the currently selected folder, whether or not it's a photo - no automatic year/month sorting."],
    ],
    col_widths=[1.8, 4.2]
)
para("Supported file types:")
add_table(
    ["Category", "Extensions"],
    [
        ["Images", "JPEG, PNG, GIF, WebP, BMP, TIFF, HEIC"],
        ["Documents", "PDF, DOC, DOCX, XLS, XLSX, PPT, PPTX, TXT, CSV, HTML, JSON, XML"],
        ["Archives", "ZIP"],
    ],
    col_widths=[1.5, 5.0]
)
tip("For photos, DMS automatically reads the capture date and GPS location from EXIF metadata and stores it with the document; the coordinates are reverse-geocoded into a readable place name automatically whenever you're online.")

h2("4.2  Uploading a Folder")
numbered("Select the destination folder in the tree.")
numbered("Click Upload Folder - this opens the Upload Multiple Folders dialog.")
numbered("Click + Add Folder and pick a folder on your computer; repeat to queue up several folders at once, each listed above with an × to remove it.")
numbered("Choose one of three upload modes:")
bullet("Sort into year/month folders (default) - every file in each picked folder (including its subfolders) is routed by capture/archive date into “year/month” folders, created automatically if they don't exist yet. The original subfolder structure is not kept.")
bullet("Upload files and subfolders as the sources - subfolders inside each picked folder are recreated here exactly as they are, with no date-based year/month folders. Good for material that's already organized the way you want to keep it.")
bullet("Recreate the picked folder under the current folder - unlike the other two modes, the picked folder itself also becomes a new subfolder here (not just its contents); every subfolder under it is rebuilt as-is, files land in their matching folders, and matching directories are created on the local drive too.")
numbered("Optionally type something into Add text to filenames (optional) - it's inserted right after the date when there is one (e.g. 2026-09-17-text-original name), or at the very front of the filename when there isn't. The mic button next to it accepts voice dictation too. Leave it blank to add nothing.")
figure("43_upload_folder_modes.png", "Figure 4-2  The Upload Multiple Folders dialog: the three upload modes and the filename text field")
numbered("Click Upload in the bottom-right corner to finish.")
tip("Mix and match across batches as needed: archive a whole phone-camera-roll folder with Sort into year/month, while a drawing folder that's already organized by project uses Upload as the sources or Recreate the picked folder to keep its structure intact.")

h2("4.3  Pasting Screenshots")
para("You don't need to save a file first - a screenshot you just took, or an image copied from elsewhere, can be pasted straight into the current folder.")
numbered("Select the destination folder in the Tree tab.")
numbered("Take a screenshot (or copy an image) to the system clipboard.")
numbered("Click Paste Screenshot, or simply press Ctrl+V (Windows) / Cmd+V (Mac).")
numbered("DMS saves the clipboard image as a PDF and adds it to the current folder.")

h2("4.4  Linking Existing Documents")
para("The same document can be linked into multiple folders without duplicating the file on disk.")
numbered("Select the folder you want to link the document into.")
numbered("Click Link Existing Document.")
numbered("A dialog lists all documents in the system.")
numbered("Check the document(s) you want and click Add Link.")
numbered("The document now appears in both the original folder and the new folder.")
tip("A faster way: just drag a document card and drop it directly onto the target folder in the left-hand tree. It has exactly the same effect as the steps above, without opening the picker dialog first.")

h2("4.5  Viewing a Document")
numbered("Click the document name or thumbnail to open the Document Viewer.")
numbered("The viewer opens as an overlay with the preview as its main content; you can drag its edges to resize it, drag the title bar to move it, or click Pop Out to turn the viewer into its own separate browser window (for example, to drag it to a second monitor). The divider between the preview and the info panel on the right can also be dragged to resize either side.")
bullet("Images are shown inline.")
bullet("PDFs open in an embedded viewer with scrolling and zoom.")
bullet("Text files are shown in a readable monospace view.")
bullet("Office files (Word, Excel, PowerPoint) offer a Download button to open in your local Office app.")
numbered("The viewer's toolbar has buttons for Add Description, View Map Location, Extract Text (OCR), Extract Key Parameters, Scan Barcode (section 4.8), Ask AI (section 4.9, requires an API key - section 6.4), Download, Pop Out, and Close.")
numbered("Click Close or press Escape to close the viewer.")
figure("05_doc_viewer_pdf.png", "Figure 4-3  The PDF document viewer")
figure("06_doc_viewer_image.png", "Figure 4-4  The image document viewer")
figure("28_doc_viewer_popout.png", "Figure 4-5  The viewer's top toolbar: extract key parameters, scan barcode, ask AI, pop out, and more")
tip("A document you've opened stays highlighted in its folder's list, so when you come back you can immediately spot which one you were just looking at, without hunting for it again.")

h2("4.6  Downloading, Sending, and Deleting Documents")
para("Every document card has a row of icons on the right:")
add_table(
    ["Icon", "Function"],
    [
        ["SN", "Set a custom serial number for this document specifically (inherits the folder's SN by default)."],
        ["Link", "View / manage which folders reference this document."],
        ["Send to phone", "Generate a link the document can be opened from on a phone, sent by SMS/iMessage, or copied and shared manually (see section 11.1)."],
        ["Email", "Send the document as an email attachment (requires SMTP setup - section 13.2; on Mac, if SMTP isn't set up, this opens the system Mail app instead)."],
        ["Delete", "Unlink from the current folder and soft-delete (moved to \u201cNot Show in Tree\u201d - see section 3.2)."],
    ],
    col_widths=[1.3, 4.7]
)
para("Download: click the download icon on a document card, or the Download button inside the viewer.")

h2("4.7  Duplicate Detection")
para("When you upload a file whose name and size both match an existing document, DMS prompts you to avoid wasting space:")
bullet("Skip - don't upload this duplicate file.")
bullet("Link only - don't copy the file; just link the existing document into the current folder (recommended - has the same effect as section 4.4).")
bullet("Upload anyway - upload it as a new, independent document even though the content matches.")

h2("4.8  Barcode Scanning")
para("If a photo has a barcode or QR code visible in it (a product label, an equipment nameplate...), DMS can read the code directly from the image - no need to type it in by hand.")
numbered("Open the viewer for that image (or a PDF containing one).")
numbered("Click Scan Barcode at the top.")
numbered("On success, a banner shows the code type and value detected (e.g. \u201cCODE128: 6901234567892\u201d).")
numbered("The result is saved automatically as the document's \u201cBarcode\u201d parameter, viewable/editable in the metadata section below; if a photo has more than one barcode, they're saved in turn as Barcode, Barcode2, Barcode3, and so on.")
figure("24_scan_barcode.png", "Figure 4-6  Scan Barcode: the success banner after a successful read")
tip("The capture date and GPS location need no extra effort - DMS already pulls those from a photo's EXIF data on upload (see section 4.1); \u201cScan Barcode\u201d just adds the code's own content. Put together, a single photo can record when, where, and what code all at once.")
note("This runs entirely offline (built on the open-source zbar library), sends nothing anywhere, and needs no AI key - but it does need a reasonably clean image: the code should be sharp, unobstructed, and have a clear quiet zone (blank margin) around it. QR codes shared as phone screenshots - especially ones with a logo overlaid in the middle, or a background that runs right up to the code's edge, like a WeChat \u201cscan to join group\u201d image - usually can't be read this way; typically only WeChat's own scanner or a phone camera can. That's a known limitation of the underlying library, not a bug.")

h2("4.9  Ask AI About a Document, or One-Click Polish")
para("Besides the folder-wide \u201cAsk AI\u201d described in chapter 18 (which reads several documents at once), opening a single document's viewer lets you ask questions about just that one document:")
numbered("Open the document viewer and click Ask AI at the top.")
numbered("A panel opens on the right; the AI answers based on whatever text (OCR), description, and key parameters have already been extracted for this document.")
numbered("Type your question in the box (or click the microphone icon to dictate it), then click Send, or press Ctrl/Cmd+Enter.")
numbered("The answer appears below, and the question and answer are both appended to a running Q&A record document kept in the same folder, so you can look back at it later.")
figure("30_doc_ask_ai.png", "Figure 4-7  Asking AI about a single document (needs a Gemini or DeepSeek API key - section 6.4)")
para("The description panel's AI Cleanup button works the same way for a single document's description: choose \u201cOrganize / Polish wording / Make concise,\u201d and the AI tidies up rough or dictated text while keeping all the information; the result previews first, and only replaces your text once you click Apply.")

h2("4.10  Convert to PDF (Batch)")
para("Need to convert a batch of photos or existing PDFs into a consistent, cleanly named set? You don't have to save each one individually.")
numbered("Check the images and/or PDFs you want to convert in the folder (use Multi-select, or check them inside the dialog that opens).")
numbered("Click Convert to PDF; the dialog lists the checked documents and their sizes.")
numbered("Click Convert N document(s) to PDF.")
numbered("Each document is converted into its own separate PDF (named with a \u201cYYYY-MM-DD-\u201d prefix whenever a date is known); the originals are moved to \u201cDeleted files\u201d (a soft delete - recoverable, see section 3.2).")
figure("33_convert_to_pdf.png", "Figure 4-8  The Convert to PDF dialog, with one document selected")

h2("4.11  On-Disk Filenames and Recovering Deletions")
para("These are lower-level improvements that need no action from you, but are useful to know about when browsing files in your system's file manager or troubleshooting:")
bullet("Filenames on disk now start with the document's name instead of its internal ID (e.g. Inspection Report+DOC-20260920-xxx.pdf), so browsing the storage folder directly in Finder / Explorer immediately tells you what a file is without opening it first.")
bullet("A failed upload now reports the actual reason (e.g. disk full, no write permission) instead of a bare, unexplained \u201c500 error.\u201d")
bullet("Before any change to a project's folder structure (adding/removing folders, moving things, batch operations), DMS automatically saves a snapshot of the structure at that moment; if a particular change ever leaves the structure in a bad state, that snapshot can be used to roll back. This safety net runs entirely in the background - you won't see any UI for it during normal use.")

h2("4.12  Batch Document Actions (Multi-select: Download, Batch Rename, Find Near-Duplicates)")
para("The document list has a row of batch-action buttons above it that work on every document in the current folder, without needing to pop out the grid-view window first (Section 19 covers the pop-out window's own batch actions - the two can be used together):")
figure("40_multiselect_toolbar.png", "Figure 4-9  Document list toolbar: Select, AI-fill parameter, Batch rename, Find near-duplicates")
bullet("Select - check one or more documents, then either Download them to a folder (defaults to your Downloads folder) or Unlink them from the current folder (the documents themselves are soft-deleted - see section 3.2).")
bullet("AI-fill parameter - multi-select documents and have AI read one parameter's value off each and fill it in for all of them at once; see section 6.6 for the full flow.")
bullet("Batch rename - renames every document in the current folder to “date + location - original name” (the date comes from the document's own capture/archive date, and documents where no date can be resolved are skipped; the location comes from the document's location metadata and is omitted if there isn't one). The actual file on disk is renamed too. Clicking it shows a preview first; nothing changes until you confirm.")
figure("42_batch_rename_preview.png", "Figure 4-10  Batch rename preview: nothing is renamed until you confirm")
bullet("Find near-duplicates - check a few photos (or click Select all photos to grab every photo in the folder), then drag the Similarity threshold slider (50%-100%); DMS compares the selected photos pairwise and flags ones that look highly similar to each other (e.g. several near-identical burst-mode shots), making it easy to spot redundant photos to clean up. This is a “looks similar” fuzzy match, a different mechanism from section 4.7's exact filename-and-size match at upload time.")
page_break()

# ══════════════════════════════════════════════════════════════════════════════
# 5  OCR
# ══════════════════════════════════════════════════════════════════════════════
h1("5  OCR - Extract Text from Documents")

h2("5.1  How OCR Works")
para("OCR (Optical Character Recognition) converts scanned images and PDFs into searchable text. DMS uses the Tesseract OCR engine, which supports both English and Chinese (Simplified).")
para("DMS uses two methods to extract text:")
add_table(
    ["Method", "When Used", "Speed"],
    [
        ["Text Layer", "PDFs that already contain digital text", "Very fast"],
        ["Tesseract OCR", "Scanned PDFs, images, or corrupted text layers", "Moderate (depends on file size)"],
    ],
    col_widths=[1.5, 3.5, 1.5]
)
note("If Tesseract is not installed on the machine, OCR for images will not be available. Text-layer extraction for digital PDFs still works.")

h2("5.2  Running OCR")
numbered("Open the document viewer by clicking the document name.")
numbered("Click Extract Text (OCR) in the toolbar.")
numbered("DMS extracts text and displays it in the text panel.")
numbered("If the document is a PDF with an embedded text layer, extraction is instant.")
numbered("If OCR is needed (a scan or an image), a progress indicator appears.")
numbered("When finished, the extracted text is shown in an editable text area.")
bullet("You can correct OCR errors by editing the text directly.")
bullet("The panel also shows the number of pages processed, character count, detected language, and extraction method used.")
note("If a PDF has a corrupted or incorrect text layer, click Force OCR to re-extract using Tesseract instead.")

h2("5.3  Saving OCR Text for Search")
numbered("After reviewing (and optionally editing) the extracted text, click Save & Make Searchable.")
numbered("The text is saved with the document's metadata.")
numbered("The document now appears in search results whenever you search for words it contains.")
tip("You only need to run OCR once per document. The text is stored and the document stays searchable forever.")

page_break()

# ══════════════════════════════════════════════════════════════════════════════
# 6  KEY PARAMETERS
# ══════════════════════════════════════════════════════════════════════════════
h1("6  Key Parameter Extraction")

h2("6.1  What Are Key Parameters?")
para("Key Parameters are user-defined labels (like \u201cContract Number,\u201d \u201cPressure Rating,\u201d or Chinese labels like \u201c\u4eba\u7269\u201d / \u201c\u4e8b\u4ef6\u201d) that DMS can automatically find and extract from document text.")
para("This lets you quickly compare values across large numbers of documents, and - combined with chapter 6.5 - chart how a given value changes over time.")

h2("6.2  Extracting Parameters from a Document")
numbered("Open the document viewer.")
numbered("Click Extract Key Parameters in the toolbar.")
numbered("A list of parameters appears (global defaults, plus any you add for this document).")
numbered("Check the parameters you want to extract.")
numbered("Optionally toggle \u201cAlso search the next line when the same line has no value\u201d off if you only want same-line matches.")
numbered("Click Extract from Document Text.")
numbered("Review the results and edit any values if needed.")
numbered("Click to save the values into the document's metadata.")
tip("You can also turn on AI-assisted extraction (needs an API key - section 6.4), which reads the document image directly and can be more accurate for handwritten or oddly formatted values, at the cost of being slower.")

h2("6.3  Managing Global Key Parameters")
numbered("Click the Project menu in the header.")
numbered("Choose Global Key Parameters.")
numbered("In the dialog, add, remove, or reorder parameters.")
numbered("Click Restore Defaults to reset to the default parameter list.")
numbered("Changes take effect immediately for all future extractions.")
figure("18_key_parameters.png", "Figure 6-1  The Global Key Parameters dialog")

h2("6.4  Configuring an AI Provider (Gemini / DeepSeek)")
para("AI-assisted key-parameter extraction, Ask AI (both for documents and folders), and the description panel's AI Cleanup and typo-check all share the same API-key setup - configure either provider, or both:")
numbered("Click any \u201cConfigure AI (Gemini)\u201d link wherever it appears (the description panel, an Ask AI panel, the key-parameters panel, and so on all lead to the same dialog) to open the AI Provider Settings dialog.")
para("If you don't have an API key yet, a \u201cDon't have an API key?\u201d link sits right below each provider's input field in that dialog and jumps straight to its free sign-up page in a new browser tab:")
add_table(
    ["Provider", "Free sign-up URL", "Steps"],
    [
        ["Google Gemini", "aistudio.google.com/apikey", "Sign in with a Google account (free to create one if you don't have one) \u2192 click \u201cCreate API key\u201d \u2192 choose a new project or an existing Google Cloud project \u2192 copy the generated key (a string starting with \u201cAIza\u201d)."],
        ["DeepSeek", "platform.deepseek.com/api_keys", "Sign up / log in to the DeepSeek platform \u2192 on the \u201cAPI Keys\u201d page, click to create a new key \u2192 copy the generated key."],
    ],
    col_widths=[1.3, 2.2, 2.5]
)
numbered("Back in DMS's AI Provider Settings dialog, paste the copied key into the matching provider's field and click its Save button.")
numbered("The two providers' keys are stored completely independently; with both configured, each AI feature generally prefers Gemini and falls back to DeepSeek automatically when Gemini isn't available.")
figure("32_ai_provider_settings.png", "Figure 6-2  AI Provider Settings: Gemini and DeepSeek configured separately")
note("These AI features are opt-in and need an internet connection; with no key configured at all, every core DMS feature (folder management, uploads, OCR, barcode scanning, search, and so on) still runs fully offline and is unaffected. Keys are stored only on this machine (a local server-side file) and are never uploaded to any DMS-related or third-party server.")
tip("Both providers offer a free tier - no credit card needed for occasional use. Free tiers are rate-limited (a cap on calls per minute/day); if you process a lot of documents in a short burst and see a \u201ctemporarily over the limit\u201d message, just wait a bit and retry.")
tip("Next to \u201cDetailed Gemini setup steps (including how to get a free/paid key)\u201d in the dialog is a Download setup guide (.docx) link, with screenshots showing how to sign up for a Google account, request a free Gemini API key, and later upgrade to a paid tier if you need higher limits - handy if you've never gotten one before.")

h2("6.5  Parameter Trend Charts and Snapshots")
para("Chart how a key parameter's value changes over time - useful for blood pressure, account balances, inventory levels, or any reading that needs to be logged repeatedly and reviewed for trends.")
numbered("Select a folder and click the Trend Chart button in the toolbar.")
numbered("In the Parameter dropdown, choose which key parameter to plot (only parameters with a numeric value somewhere in this folder or its sub-folders are listed); choose a line or bar chart, pick a field for the horizontal axis (such as document date), and \u201cInclude sub-folders\u201d controls whether documents in child folders count too.")
figure("29_trend_chart.png", "Figure 6-3  The trend chart window (this folder has no numeric parameters yet; picking one shows a line/bar chart)")
numbered("Like the document viewer, the chart window can be dragged, resized by its edges, or popped out into its own window; the axis ranges can also be set manually instead of relying on auto-scaling.")
numbered("Happy with a particular parameter's chart? Click Save Snapshot - DMS records that parameter's current total/latest value into an \u201c[X] History.xlsx\u201d document; save a few snapshots over time and you get a chart of how the value has changed.")
tip("Typical use: click Save Snapshot on a \u201cBank Account\u201d folder's balance parameter at the end of each month; after a few months you'll have a chart of the balance trend. The same pattern works for blood pressure, body weight, or periodic inventory counts.")
numbered("Once there are enough data points, click Fit with Gemini in the chart window (it becomes Re-analyze once you have a result) to have Gemini fit a trend line across the values and comment on it in plain language (e.g. \u201coverall upward trend; the latest reading is slightly above average\u201d). Needs a Gemini API key configured first (section 6.4).")

h2("6.6  Batch AI Parameter Extraction (Multi-select Documents)")
para("Section 6.2 covers opening one document and manually checking parameters to extract them one at a time. To fill in the same parameter across a whole batch of documents at once, use AI-fill parameter in the document list toolbar instead - no need to open each document individually.")
numbered("Above a folder's document list, click AI-fill parameter; the toolbar switches to multi-select mode.")
numbered("Check the documents to process (or click Select all), then click AI-extract parameter on the right.")
numbered("In the dialog, pick an existing key parameter from the dropdown, or type a new parameter name below it.")
figure("41_ai_fill_parameter.png", "Figure 6-4  The AI-extract parameter dialog: choose a parameter, with the document count checked")
numbered("Click Start extracting. DMS reads each selected document's page images to find that parameter's value (it doesn't need to match the document's text verbatim); results are listed for you to review first, and only get written into each document's \u201cActual Value\u201d once you click Apply.")
note("Because this reads the page images themselves, it works just as well on scans and photos with no text layer. Needs a Gemini API key configured first (section 6.4).")

page_break()

# ══════════════════════════════════════════════════════════════════════════════
# 7  SEARCH
# ══════════════════════════════════════════════════════════════════════════════
h1("7  Searching Documents")

h2("7.1  Basic Search")
numbered("Click the Search tab in the sidebar.")
numbered("Type a word or phrase in the search box.")
numbered("Results appear instantly as cards showing:")
bullet("Document name and file type icon.")
bullet("Upload date, file size, document ID.")
bullet("All metadata fields (parameter name / value).")
bullet("GPS-derived location (if available).")
bullet("Which folders reference this document.")
note("Search is case-insensitive and partial-match - typing \u201cpump\u201d will find \u201cPump Assembly,\u201d \u201ccentrifugal pump,\u201d and so on.")
para("When you type more than one word, the \u201cMulti-word match\u201d control next to the search box decides how they combine:")
bullet("All words (AND) - the default. A document must contain every word you typed (in any order) to match.")
bullet("Any word (OR) - a document matches if it contains at least one of the words.")
bullet("Whole sentence - the entire contents of the search box is matched verbatim, as one exact phrase, instead of being split into separate words.")
figure("38_search_match_modes.png", "Figure 7-1a  The Multi-word match control: All words (AND) / Any word (OR) / Whole sentence")
tip("Switch to \u201cAny word (OR)\u201d when you're not sure which of several related terms a document uses (e.g. \u201cinvoice receipt bill\u201d); switch to \u201cWhole sentence\u201d when you want to find one specific phrase and AND/OR are turning up too many unrelated partial matches.")

h2("7.2  Using Filters")
add_table(
    ["Filter", "How to Use"],
    [
        ["File Type", "Check Image, PDF, Text, or Documents to restrict results to that type."],
        ["Scope", "Enable \u201cScope to selected node\u201d to search only within the currently selected folder and its sub-folders."],
        ["Metadata", "Enter a parameter name and/or value to find documents with matching metadata."],
    ],
    col_widths=[1.5, 5.0]
)
figure("07_search.png", "Figure 7-1  Example search results")

h2("7.3  Opening and Jumping to Results")
bullet("Click a result card to open the Document Viewer for that document.")
bullet("Double-click a result card to switch to the Tree tab and jump straight to the folder containing that document.")

page_break()

# ══════════════════════════════════════════════════════════════════════════════
# 8  DATABOOK
# ══════════════════════════════════════════════════════════════════════════════
h1("8  Generating a Databook (PDF / Word)")
para("The Databook feature combines selected documents into a single, professional document with a cover page, table of contents, section headers, and page numbers. Two modes are available: manually checking documents to build a PDF (8.1-8.2), or automatically generating an editable Word document from a folder's structure (8.3).")

h2("8.1  Selecting Documents (PDF mode)")
numbered("Click the Databook tab in the sidebar.")
numbered("The tree is shown with a checkbox next to each folder, plus a Select All link.")
numbered("Check individual documents, or use the shortcuts:")
bullet("Click Select All next to a folder to select every document in that folder (optionally including sub-folders).")
bullet("Click Clear to deselect everything.")
tip("Your selection is saved automatically - close and reopen DMS and it will still be there.")
figure("08_databook_tab.png", "Figure 8-1  The Databook tab: checking documents to include")

h2("8.2  Generating the PDF")
numbered("After selecting documents, click Generate Databook.")
numbered("In the dialog, enter a Title (the main heading on the cover page) and a Subtitle (a secondary line, e.g. project name or revision number).")
numbered("Click Generate and Download.")
numbered("DMS generates the PDF on the server and your browser downloads it automatically.")
para("The generated PDF includes: a cover page (title, subtitle, date), a table of contents with clickable bookmarks, section-header pages, all selected documents in tree order, and page numbers/footers throughout.")
figure("09_databook_dialog.png", "Figure 8-2  The Generate Databook dialog")

h2("8.3  Auto-Generating a Word Databook from a Folder")
para("Instead of hand-checking individual documents, this mode only needs you to pick one \u201ctop\u201d folder - DMS automatically turns the folder structure into chapters, producing a .docx file you can keep editing and reformatting in Word - useful when you need to tweak layout before submitting it, or the recipient specifically wants Word rather than PDF.")
numbered("Click the Databook tab in the sidebar, then switch to Auto-Generate by Folder (Word).")
numbered("Check one folder - each of its sub-folders (nesting to any depth) becomes a chapter, documents within a chapter keep their existing folder order, and each document is followed by its own description text.")
numbered("Click the generate button; DMS builds the .docx file on the server and your browser downloads it.")
figure("34_databook_docx.png", "Figure 8-3  The Databook tab's two modes: \u201cManual selection (PDF)\u201d and \u201cAuto-generate by folder (Word)\u201d")
note("Both modes create a brand-new file and never modify the original documents in your folders. The Word mode's tables, headings, and body text are native, editable Word content - not PDF pages pasted in as images - so you can freely add, remove, or reformat text afterward.")

page_break()

# ══════════════════════════════════════════════════════════════════════════════
# 9  MERGE DOCUMENTS
# ══════════════════════════════════════════════════════════════════════════════
h1("9  Merging Documents into a Single PDF")
para("Merge combines multiple PDFs and images from a folder into one PDF file, which is then added back into that folder.")
numbered("Select a folder in the Tree tab.")
numbered("Click Merge Documents in the main content panel.")
numbered("A dialog opens listing the documents in the folder.")
numbered("Check the documents to merge (or click Select All).")
numbered("Click Merge.")
numbered("DMS creates the merged PDF and adds it to the current folder.")
note("Merging only supports PDF and image formats; Word/Excel/PowerPoint and other formats should be saved as PDF first, then uploaded and merged. The original documents remain in the folder afterward - unlink them manually if you no longer need the individual copies.")
figure("10_combine_dialog.png", "Figure 9-1  The merge-documents dialog")

page_break()

# ══════════════════════════════════════════════════════════════════════════════
# 10  FACE RECOGNITION
# ══════════════════════════════════════════════════════════════════════════════
h1("10  Face Recognition")
para("The optional face-recognition component lets you tag faces in your photos and later browse them by person, with DMS automatically matching new photos of people it already knows.")

h2("10.1  Installing the Face Recognition Component")
numbered("Click the Faces tab in the sidebar.")
numbered("If the component (deepface) isn't installed yet, a prompt explains this and offers an Auto-install deepface button.")
numbered("Click it and wait - this downloads TensorFlow and related libraries in the background and can take several minutes on a slow connection; you can keep using the rest of the app meanwhile.")
figure("16_face_tab.png", "Figure 10-1  The Faces tab (shown here with the component already installed, hence the empty state; before installing, you'd instead see a prompt with an Auto-install deepface button)")
tip("If auto-install keeps failing (more common on Windows), download the Face Recognition Install Guide (.txt) - Windows from the Project menu's Downloads group for manual, step-by-step instructions for installing Python 3.12 and deepface.")

h2("10.2  Tagging Faces")
numbered("From a folder, click Tag Faces to enter selection mode.")
numbered("Check the photos you want DMS to scan for faces.")
figure("21_face_select_mode.png", "Figure 10-2  Selection mode: photos checked for face detection")
numbered("Click to run detection; DMS finds faces in the checked photos.")
figure("22_face_tagging_dialog.png", "Figure 10-3  Face detection in progress")
numbered("For each detected face, type a person's name (or pick an existing one) to tag it.")

h2("10.3  Browsing by Person / Auto-Matching")
para("Once some faces are tagged, the Faces tab lets you browse photos grouped by person. New photos you tag later are automatically compared against known faces and suggested matches, so you don't have to name every single photo of the same person from scratch.")

page_break()

# ══════════════════════════════════════════════════════════════════════════════
# 11  REMOTE / MOBILE UPLOAD
# ══════════════════════════════════════════════════════════════════════════════
h1("11  Remote and Mobile Upload")

h2("11.1  Sending a Single Document to a Phone")
para("Click the \u201csend to phone\u201d icon on any document card to generate a link that opens the document directly on a phone's browser - share it by SMS/iMessage, or copy the link and send it any way you like.")

h2("11.2  Remote Upload (QR-Code Batch Intake)")
para("To receive a batch of photos or files from someone else's phone (or your own) without emailing them one by one, use Remote Upload from the desktop launcher: it shows a QR code that, once scanned, opens an upload page on the phone's browser, and anything uploaded there lands directly in the folder you chose on the desktop.")
note("Remote upload works over your local network by default; sending or receiving across the open internet requires the tunnel/ngrok setup described alongside that feature in the app, which is outside the scope of this manual section.")

page_break()

# ══════════════════════════════════════════════════════════════════════════════
# 12  PROJECT MANAGEMENT
# ══════════════════════════════════════════════════════════════════════════════
h1("12  Project Management")
para("The Project menu (in the header bar) provides tools for backing up, restoring, merging, and importing data.")
note("If you've set an access password (Chapter 14), all three export types (METADATA / COMP / PART) show an “Automatically encrypted with the project password” note in the menu - the exported .dms file is encrypted with that password, and the same password is needed to decrypt it when importing on another machine. Without a password set, exported backups are not encrypted.")

h2("12.1  Export Index Backup (METADATA)")
para("Backs up only the folder-tree structure and metadata (not the actual document files) as a .dms file, named with a METADATA- prefix. Much smaller and faster than a full backup - useful for structural snapshots.")

h2("12.2  Export Full Backup (COMP)")
para("Backs up the entire folder tree and every document file, packaged as a .dms file with a COMP- prefix. This is a complete, self-contained backup of the project.")
tip("Save this file to a USB drive or cloud storage as a regular backup.")

h2("12.3  Export Selected Folders (PART)")
para("Lets you pick specific folders to export (with their files), producing a .dms file with a PART- prefix - useful for sharing or archiving just part of a project.")

h2("12.4  Backup Retention Rules")
para("The project keeps the 10 most recent .dms backups in the project's root folder; older ones are automatically moved into a \u201cNot Shown in The Tree\u201d archive folder at the root of the storage directory (not to be confused with the similarly named \u201cNot Show in Tree\u201d node inside the document tree from section 3.2, which holds soft-deleted documents instead).")

h2("12.5  Import a Project (.dms)")
numbered("Click Project, then Import Project (.dms) and select the file.")
numbered("Choose a destination folder for the import (a new, empty folder is recommended).")
numbered("Click Import and Switch.")
numbered("DMS extracts the files and rebuilds the tree structure, and switches the active project to it.")
note("Importing only restores the folder-tree structure; it never deletes files already on disk.")

h2("12.6  CSV Export")
para("Click Project, then Export Metadata (CSV) to download a spreadsheet with one row per document, including document ID, name, size, upload date, and metadata fields. Open it in Excel or Google Sheets for analysis or sharing.")

h2("12.7  Batch ZIP Import")
para("Upload a large batch of files at once using a naming convention, and have DMS automatically sort each one into its matching folder.")
numbered("Prepare a ZIP file, naming each file inside it like this: FolderName#description.pdf (e.g. Home Insurance#2026 Policy.pdf) - the part before the # is the destination folder name, and everything after the # is the rest of the filename, which can be anything you like.")
numbered("Click Project, then Import Documents from ZIP, and choose your ZIP file.")
numbered("DMS reads every filename, matches the part before the # against folder names in the tree, and places each document into the matching folder.")
note("Folders referenced by a filename must already exist in the tree before importing (unlike Hierarchy Import in section 12.8, this does not create new folders). Documents whose folder name doesn't match anything are left unlinked.")
note("If the same folder name occurs more than once in the tree (e.g. a “Bolt” sub-part under both “Rotor Assembly” and “Housing Assembly”), matching by bare name lands on whichever one comes first, which may not be the one you meant. In that case, use a “/”-separated path before the # instead of a bare name - starting from the root or from any ancestor folder that makes the path unique - e.g. Rotor Assembly/Bolt#List.pdf to land specifically on the “Bolt” folder under “Rotor Assembly” rather than the identically-named one under “Housing Assembly.”")

h2("12.8  Hierarchy Import (with auto de-duplication)")
para("Builds a folder tree quickly from a plain text/CSV file - handy for turning a bill of materials or org-chart export from another system straight into a matching folder structure.")
numbered("Prepare a text file with 2-3 comma-separated columns per line:")
add_table(
    ["Column", "Meaning", "Notes"],
    [
        ["Column 1", "Folder name", "Required. The folder this row creates."],
        ["Column 2", "Parent folder name", "Which already-listed folder this one nests under; leave blank for the root. The file's very first row is the root folder itself, so its own parent column must be blank (or omitted)."],
        ["Column 3", "Description (optional)", "Can be left out of the file entirely. When present, it becomes that folder's initial description text."],
    ],
    col_widths=[1.0, 1.8, 3.7]
)
para("Example:")
codeblock("Top Assembly\nPump Housing,Top Assembly\nRotor Assembly,Top Assembly\nImpeller,Rotor Assembly\nShaft,Rotor Assembly")
note("Level-heading rows exported by BOM tools (lines that just say something like “Level 1”) are skipped automatically - no need to delete them by hand. If the first row is a column-title row (“folder name, parent folder name, description,” in whatever wording), DMS detects that structurally from how the rows reference each other and ignores it automatically too, without you needing to delete it.")
numbered("Click Project, then Create Folder Tree from Hierarchy, and upload (or paste) the text.")
numbered("Click Validate. If the same folder name repeats anywhere in the file, this is flagged as an error by default, listing the conflicting lines.")
figure("12_hierarchy_import_empty.png", "Figure 12-1  The hierarchy import dialog (initial state)")
figure("14_hierarchy_import_error.png", "Figure 12-2  With auto-numbering off, a duplicate folder name is flagged as an error")
numbered("If your data genuinely has same-named folders on purpose (e.g. a “Bolt” sub-part repeated under several assemblies), check Auto-number duplicate folder names (01, 02, 03...) and re-validate - every repeated name is automatically numbered in turn (“Bolt 01,” “Bolt 02,” ...) and the whole file now passes validation instead of erroring.")
figure("15_hierarchy_import_dedupe_ok.png", "Figure 12-3  With auto-numbering on, the duplicate name is numbered and passes validation")
numbered("Once validation passes, a tree preview and the number of nodes to be created are shown.")
numbered("Click Create; all folders appear in the tree immediately.")

h2("12.9  Downloading This Manual from Inside the App")
para("Click Project, then Download User Manual (.docx) under the Downloads section, to get the same manual you're reading now, generated fresh from inside the running app.")
figure("11_project_menu.png", "Figure 12-4  The Project menu: export, import, and settings")

page_break()

# ══════════════════════════════════════════════════════════════════════════════
# 13  CLOUD SYNC & EMAIL
# ══════════════════════════════════════════════════════════════════════════════
h1("13  Cloud Sync and Email")

h2("13.1  Using OneDrive to Sync Storage")
para("Pointing your DMS storage folder at a location synced by OneDrive (or a similar cloud-sync service) gives you automatic off-site backup with no extra steps - every change DMS makes on disk gets picked up and uploaded by the sync client in the background, the same as any other file in that folder.")
note("Very large libraries, or a very slow internet connection, can make cloud sync lag behind your local changes; this is a property of the sync client, not of DMS.")

h2("13.2  Email Sending Setup")
para("To use the \u201cemail\u201d action on a document card, configure an SMTP account first.")
numbered("Click Project, then Email Sending Setup.")
numbered("Enter your SMTP server, port, username, and password (an app-specific password is usually required for Gmail/Outlook accounts with two-factor authentication).")
numbered("Save the settings; DMS uses them to send documents as attachments going forward.")
figure("20_email_settings.png", "Figure 13-1  Email sending (SMTP) settings dialog")
note("On Mac, if SMTP hasn't been configured, clicking the email action instead opens your system Mail app with the document attached, so the feature still works without any setup.")
tip("The Download setup guide (.docx) button in the dialog gives step-by-step, illustrated instructions for enabling SMTP and generating an app-specific password in common providers like Gmail and QQ Mail.")

page_break()

# ══════════════════════════════════════════════════════════════════════════════
# 14  PASSWORD
# ══════════════════════════════════════════════════════════════════════════════
h1("14  Password Protection")
para("You can protect DMS with a password so that only authorized users can access the data - strongly recommended once you've enabled LAN access, remote upload, or saved any website login credentials (section 3.6).")

para("Set a password:")
numbered("Click Project, then Set Password.")
numbered("Enter and confirm your chosen password.")
numbered("Click Set.")
numbered("On the next DMS startup, a login screen appears before the main interface.")

para("Remove the password:")
numbered("Click Project, then Change Password.")
numbered("Enter your current password to confirm, then leave the new password fields blank.")
numbered("Click Set to remove password protection.")

para("Logout:")
bullet("Click Logout in the header bar.")
bullet("DMS returns to the login screen. The server keeps running; only the browser session ends.")

note("The password is stored as a salted hash on the server (SHA-256, 100,000 iterations) - never in plain text. A folder's saved website login password (section 3.6) uses a separate, independent reversible-encryption scheme; the two don't affect each other.")

figure("19_password_dialog.png", "Figure 14-1  The set/remove password dialog")

page_break()

# ══════════════════════════════════════════════════════════════════════════════
# 15  KEYBOARD SHORTCUTS
# ══════════════════════════════════════════════════════════════════════════════
h1("15  Keyboard Shortcuts")
add_table(
    ["Action", "Shortcut"],
    [
        ["Confirm folder rename / new folder name", "Enter"],
        ["Cancel rename / new folder", "Escape"],
        ["Start renaming a folder", "Double-click the folder name"],
        ["Open folder context menu", "Right-click the folder"],
        ["Close the Document Viewer", "Escape, or click Close"],
        ["Paste a clipboard image into the current folder", "Ctrl+V (Windows) / Cmd+V (Mac)"],
        ["Copy selected text", "Ctrl+C"],
        ["Select all text in a text field", "Ctrl+A"],
        ["Undo (in text fields)", "Ctrl+Z"],
        ["Jump to the previous/next document in the viewer", "← / → (left/right arrow)"],
    ],
    col_widths=[3.5, 3.0]
)

page_break()

# ══════════════════════════════════════════════════════════════════════════════
# 16  MULTIPLE PROJECTS AT ONCE
# ══════════════════════════════════════════════════════════════════════════════
h1("16  Working with Multiple Projects at Once")
para("DMS is no longer limited to one project at a time.")
numbered("Click the Project menu in the header bar and choose the \u201copen another project\u201d option (or simply run DMS.app / DMS.exe again and pick a different project in the startup window described in section 1.3).")
numbered("DMS starts a separate window and its own service for the new project, on a different local port, completely independent of the first window - both can be logged in and used at the same time without kicking each other's session out.")
numbered("Opening the same project folder again is detected, and DMS switches to the existing window for it instead of starting a duplicate service.")
tip("Besides switching to an existing project, the Project menu's Settings group also has Create New Blank Project, which starts a brand-new, empty project in a new folder at any time - no restart, and no need to go through the section 1.3 startup screen.")
note("Every normal quit (the red close button, the Quit DMS button, Cmd+Q, Dock right-click quit, and so on) records that project's path, so the next plain double-click launch's Continue with last project (section 1.3) points to it - if several project windows were open, \u201clast\u201d means whichever one you quit most recently.")

page_break()

# ══════════════════════════════════════════════════════════════════════════════
# 17  COMBINE PROJECT
# ══════════════════════════════════════════════════════════════════════════════
h1("17  Combining Another Project In")
para("If the same material was organized separately on two computers (or by two different people), and you now want to merge it into one, you don't have to move files by hand - Combine Project automatically folds another independent project's folder tree and documents into the one you're currently using.")
numbered("Click the Project menu in the header bar.")
numbered("Under Import, choose Combine other sub-project...")
numbered("In the dialog, click Add sub-project folder... and choose another independent DMS project's storage folder (you can add more than one).")
numbered("Click Preview (no changes made) to see what the merge will do before committing, or skip straight to Combine.")
figure("31_combine_project.png", "Figure 17-1  The Combine Other Sub-Project dialog")
para("Merge rules:")
bullet("Folders with the same name are merged into the matching folder in the current project - you won't end up with two side-by-side folders of the same name.")
bullet("Documents that are byte-for-byte identical are automatically recognized as duplicates and skipped, rather than importing twice and wasting space.")
bullet("Files and folders in the original project are never modified or deleted - this is a one-way, copy-style operation, and the source project can keep being used independently afterward.")
tip("Good fit for: two computers (or two people) who each organized part of the same material for a while and now want one combined project; also useful for folding a small \u201cinspection photos\u201d side-project back into the main one.")

page_break()

# ══════════════════════════════════════════════════════════════════════════════
# 18  FOLDER-LEVEL ASK AI
# ══════════════════════════════════════════════════════════════════════════════
h1("18  Folder-Level \u201cAsk AI\u201d: Batch Document Q&A and PDF Export")
para("Section 4.9 covers asking about a single document. To have AI read several documents in a folder at once to answer a question (\u201chow much do these receipts add up to in total\u201d), use the Ask AI button in the folder toolbar.")
numbered("Select the target folder and click Ask AI in the toolbar.")
numbered("The dialog lists every document in that folder (and its sub-folders) with a checkbox; check the ones to hand to the AI. The view toggle in the top-right switches between List / Small / Large / Extra-large thumbnails to help you see what you're picking - your choice of size is remembered for next time.")
numbered("Type your request or question in the box below (or dictate it), then choose one of two paths:")
bullet("Ask AI Now - the AI (Gemini or DeepSeek) reads the checked documents' content and answers, citing which documents it drew on; turning on \u201cLet AI look at photos/scans\u201d lets it directly \u201cread\u201d photos and scans with no text layer too (Gemini only, slower). This needs an API key configured first (section 6.4).")
bullet("Build PDF (for tools like ChatGPT) - assembles the checked documents (photos annotated with capture date/location/notes, PDFs merged as-is) into one complete PDF, handy for copying or handing to an external AI tool like ChatGPT or Claude for further follow-up questions. This option needs no API key.")
figure("26_folder_ask_ai.png", "Figure 18-1  Asking AI about a folder: picking documents and choosing how to proceed")
numbered("Checking \u201cArchive the answer to the folder's Q&A record\u201d appends the exchange to a running \u201cAI Q&A Record.pdf\u201d in that folder, so it keeps building into a useful reference instead of being lost after you close the dialog.")
tip("Example: check all the receipts, bank statements, and last year's tax return in a \u201cTax Documents\u201d folder and ask \u201cabout how much of a refund am I looking at this year\u201d; or build a PDF and hand it to another AI tool for a second opinion.")

page_break()

# ══════════════════════════════════════════════════════════════════════════════
# 19  ICON VIEW POP-OUT & BATCH ACTIONS
# ══════════════════════════════════════════════════════════════════════════════
h1("19  Icon View: Pop-Out Window and Batch Actions")
para("Besides the default list view, a folder's documents can be switched to an \u201cicon view\u201d (thumbnail browsing similar to a file explorer's grid mode), which can also be popped out into its own separate window.")
numbered("In a folder's document area, click the icon-view toggle (the grid icon next to the list-view icon).")
numbered("Switch between Small and Large thumbnails (an Extra-large size is available in the pop-out window only).")
numbered("Click Pop Out to open the icon view in its own browser window - drag it to a second monitor, resize it freely, and it no longer takes up space in the main window.")
figure("37_popout_grid.png", "Figure 19-1  The pop-out icon-view window: multi-select mode allows batch download, delete, or prefixing")
numbered("Click Multi-select in the top-right of the pop-out window, check some files, and then:")
bullet("Download - downloads the checked files one after another.")
bullet("Add Prefix - adds the same prefix to a batch of filenames at once (for example, appending a note after the date), a one-shot batch rename.")
bullet("Delete - soft-deletes the checked files in bulk (moved to \u201cNot Show in Tree\u201d - section 3.2).")
note("The pop-out window talks to the main window through the browser's own tab-messaging mechanism; if it hasn't communicated with the main window for a while, reopening the folder in the main window may show the pop-out as \u201cexpired\u201d - just click Pop Out again to open a fresh one. No documents are affected either way.")

page_break()

# ══════════════════════════════════════════════════════════════════════════════
# 20  TROUBLESHOOTING
# ══════════════════════════════════════════════════════════════════════════════
h1("20  Troubleshooting")

add_table(
    ["Problem", "Solution"],
    [
        [
            "Windows SmartScreen blocks DMS.exe, or double-clicking does nothing",
            "Click \u2018More info\u2019 then \u2018Run anyway\u2019; if there's no response at all, the file is most likely blocked - see section 1.4 to unblock it."
        ],
        [
            "macOS says \u2018cannot verify the developer\u2019",
            "Go to System Settings -> Privacy & Security, find the notice, and click Open Anyway."
        ],
        [
            "The browser does not open automatically",
            "Open your browser manually and go to http://127.0.0.1:8765 (or whichever port the console/log shows)."
        ],
        [
            "Browser shows \u2018this site can't be reached\u2019, or an action reports \u2018Failed to fetch\u2019",
            "Make sure the DMS background process is still running (a console window or tray icon on Windows; a menu-bar / Dock icon on Mac). Relaunch DMS if it has quit. If you just clicked Quit DMS, the server shutting down is expected."
        ],
        [
            "Clicking \u2018Auto-install deepface\u2019 shows \u2018Installing...\u2019 for a long time",
            "This is normal - installation runs in the background and downloading TensorFlow and related components can take ten-plus minutes on a slow connection. You can keep using the rest of the app; the button returns to normal automatically when done, no page refresh needed."
        ],
        [
            "The OCR button is greyed out or says Tesseract is unavailable",
            "Tesseract OCR is an optional component. On Windows, install it from the UB-Mannheim build; on Mac, run brew install tesseract, then restart DMS."
        ],
        [
            "Chinese text is not extracted correctly",
            "Install Tesseract's Simplified Chinese language pack (chi_sim.traineddata). See the Tesseract documentation."
        ],
        [
            "GPS location shows as coordinates, not a city name",
            "Reverse-geocoding needs an internet connection - check your network."
        ],
        [
            "An uploaded file does not appear in the document list",
            "Refresh the browser page (F5) - the file may have uploaded successfully even though the page didn't auto-update."
        ],
        [
            "Can't find a document uploaded or deleted earlier",
            "Use the Search tab to search by filename or content; a deleted document may be sitting in the \u2018Not Show in Tree\u2019 recycle-bin folder (section 3.2)."
        ],
        [
            "DMS quits immediately after launching",
            "Make sure nothing else on your computer is using the port DMS wants (starting near 8765); restart your computer and try again."
        ],
        [
            "Generating a databook or merged PDF fails, or shows an \u2018Unsupported type\u2019 placeholder page",
            "Merging only supports PDF and image formats; save Word/Excel/PowerPoint files as PDF first, then upload and merge (chapter 9)."
        ],
        [
            "The storage folder has a lot of .dms files piling up",
            "This is normal automatic backup behaviour (section 12.4); once there are more than 10, older files are archived into a \u2018Not Shown in The Tree\u2019 sub-folder automatically - the count doesn't grow forever."
        ],
        [
            "Scan Barcode says \u2018no barcode detected in the image\u2019",
            "Make sure the barcode itself is sharp, unobstructed, and has a blank margin around it. QR codes shared as phone screenshots - especially ones with a logo in the middle or a background that runs to the code's edge, like a WeChat \u2018scan to join group\u2019 image - usually can't be read this way; see section 4.8."
        ],
        [
            "A \u2018Transcribe Recording\u2019 or \u2018AI Cleanup\u2019 button shows \u2018Configure AI (Gemini)\u2019 instead",
            "You haven't configured a Gemini API key yet. Click the link, paste your key when prompted, and save - see section 6.4."
        ],
        [
            "Transcribing a recording produces no change, or reports failure",
            "Make sure the recording actually contains clear speech (music, silence, or background noise alone is reported as \u2018no speech detected\u2019); an \u2018UNAVAILABLE\u2019 or high-demand error usually means the Gemini service is temporarily busy - retry shortly."
        ],
        [
            "\u2018Continue with last project\u2019 is greyed out at startup",
            "There's no recorded normal-quit yet (first run, or the process was force-killed last time) - use Select a project... or New project... instead; see section 1.3."
        ],
        [
            "The pop-out icon-view window shows \u2018this window has expired\u2019",
            "Go back to the main window and click Pop Out again to open a fresh one - this is the normal expiry of a one-time handoff token between the two windows and doesn't affect any documents."
        ],
        [
            "After combining a project, a folder has fewer documents than expected",
            "Byte-for-byte identical documents are recognized as duplicates and skipped by design; to keep both copies, rename or slightly modify the document in the source project before combining - see chapter 17."
        ],
    ],
    col_widths=[2.5, 4.0]
)

doc.add_paragraph()
h2("Getting Help")
para("If you encounter an issue not listed above, please contact the application developer with:")
bullet("A description of what you were doing when the problem occurred.")
bullet("Any error message shown on screen.")
bullet("Your operating system version (e.g. Windows 11 or macOS 15).")

page_break()

# ══════════════════════════════════════════════════════════════════════════════
# QUICK REFERENCE CARD
# ══════════════════════════════════════════════════════════════════════════════
h1("Quick Reference Card")
add_table(
    ["Task", "How"],
    [
        ["Create a new folder", "Select parent -> New Folder -> type name -> Enter"],
        ["Upload documents", "Select folder -> Upload Files / Upload (no year/month folders) -> pick files"],
        ["Paste a screenshot", "Select folder -> Ctrl+V / Cmd+V, or click Paste Screenshot"],
        ["View a document", "Click the document name"],
        ["Run OCR on a document", "Open viewer -> Extract Text (OCR) -> Save & Make Searchable"],
        ["Search all documents", "Click Search tab -> type keyword"],
        ["Save a folder's website login", "Pencil icon -> Login Credentials -> fill in site/account/password -> Done"],
        ["Generate a combined PDF (databook)", "Databook tab -> check documents -> Generate Databook"],
        ["Merge files into one PDF", "Select folder -> Merge Documents -> select files -> Merge"],
        ["Tag faces in photos", "Tag Faces -> check photos -> detect -> name each face"],
        ["Send a document to a phone", "Document card -> send-to-phone icon -> enter number"],
        ["Receive files from someone else remotely", "Desktop launcher -> Remote Upload -> they scan the QR code"],
        ["Move a folder", "Drag and drop the folder to its new place in the tree"],
        ["Set a serial number", "Pencil icon on folder -> enter SN -> Done"],
        ["Export index / full / selected backup", "Project menu -> Export Index / Full / Selected Backup"],
        ["Export metadata as CSV", "Project menu -> Export Metadata (CSV)"],
        ["Set a password", "Project menu -> Set Password"],
        ["Set up email sending", "Project menu -> Email Sending Setup"],
        ["Dictate a description into a folder/document", "Description panel -> Voice Input -> pick Chinese/EN -> speak"],
        ["Transcribe an uploaded recording into a description", "Description panel -> Transcribe Recording -> pick a file"],
        ["Clean up / polish a description with AI", "Description panel -> pick a mode -> Clean up with AI -> Apply"],
        ["Save a description as an updatable PDF", "Description panel -> Save as PDF (click again after editing to update the same file)"],
        ["Scan a barcode/QR code in a photo", "Open image viewer -> Scan Barcode"],
        ["Unblock a Windows \u2018blocked\u2019 file", "Right-click DMS.exe -> Properties -> check Unblock -> Apply -> OK"],
        ["Continue with the last project / start a new blank one", "Double-click DMS.app / DMS.exe -> pick the matching button in the startup window"],
        ["Combine another project in", "Project menu -> Combine other sub-project... -> add folder(s) -> Combine"],
        ["Ask AI about several documents in a folder at once", "Folder toolbar -> Ask AI -> check documents -> Ask AI Now"],
        ["Batch-convert images/PDFs into individual PDFs", "Select documents -> Convert to PDF -> confirm"],
        ["Pop the icon view out into its own window", "Switch to icon view -> Pop Out"],
        ["Multi-select folders to delete/download in bulk", "Tree toolbar -> Multi-select Folders -> check -> Download/Delete"],
        ["Chart a parameter's value over time", "Select folder -> Trend Chart -> pick a parameter -> Save Snapshot"],
        ["Auto-generate a Word databook from a folder", "Databook tab -> Auto-generate by folder (Word) -> check a folder"],
        ["Configure a Gemini / DeepSeek API key", "Any \u2018Configure AI\u2019 link -> paste key -> Save"],
    ],
    col_widths=[2.7, 4.3]
)

# ── Save ──────────────────────────────────────────────────────────────────────
output_path = "/Users/david/PMS/PMS_User_Manual.docx"
doc.save(output_path)
print("Saved to", output_path)
