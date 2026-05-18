"""
UpdateFromJira.py
=================
Before monthly meeting: pulls task status from Jira and updates
Slide 2 (KPIs) + Slide 3 (fresh table, no editing of existing XML).

Strategy for Slide 3:
  - Removes any existing table entirely
  - Inserts a brand new table with correct data
  - No repair warnings, no blank slides

Run:
    python UpdateFromJira.py --dry-run
    python UpdateFromJira.py
    python UpdateFromJira.py --pptx "My Report.pptx"

Install: pip install requests
"""
import sys
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', line_buffering=True)

import os, re, glob, zipfile, argparse, urllib3
from datetime import datetime

urllib3.disable_warnings()

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
CONFIG = {
    'JIRA_BASE_URL': 'https://atlassiansuite.mservice.com.vn:8443',
    'JIRA_USER':     'nam.dang',
    'JIRA_PAT':      os.environ.get('JIRA_PAT', 'ODU2NDU5NjA1NTM3OjjlU2ev98OM+2vO5YCDpUjj9Cdg'),
    'PROJECT':       'CEO',
    'PARENT_TASK':   'ONLINE',
    'DRY_RUN':       False,
}

STATUS_MAP = {
    'to do':       ('To Do',       '595959'),
    'open':        ('To Do',       '595959'),
    'new':         ('To Do',       '595959'),
    'in progress': ('In Progress', '0D5CDF'),
    'inprogress':  ('In Progress', '0D5CDF'),
    'in review':   ('In Review',   'D97706'),
    'pending':     ('Pending',     'D97706'),
    'done':        ('Done',        '007E6F'),
    'closed':      ('Done',        '007E6F'),
    'resolved':    ('Done',        '007E6F'),
    'blocked':     ('Blocked',     'DC2626'),
    'cancelled':   ('Cancelled',   '888780'),
}

def status_display(s):
    return STATUS_MAP.get(s.lower().strip(), (s, '595959'))


# ─────────────────────────────────────────────
# FILE DETECTION
# ─────────────────────────────────────────────
def find_pptx(hint=None):
    folder = os.path.dirname(os.path.abspath(__file__))
    if hint:
        if os.path.exists(hint):            return hint
        c = os.path.join(folder, hint)
        if os.path.exists(c):               return c
        print(f"  ❌ File not found: {hint}")
        return None
    files = [f for f in glob.glob(os.path.join(folder, '*.pptx'))
             if '_updated_' not in os.path.basename(f)
             and not os.path.basename(f).startswith('~$')]
    if not files:
        files = glob.glob(os.path.join(folder, '*.pptx'))
    if not files:
        print(f"  ❌ No .pptx files found in {folder}")
        return None
    files.sort(key=os.path.getmtime, reverse=True)
    print(f"  📁 Available .pptx files:")
    for i, f in enumerate(files[:5]):
        print(f"     [{i+1}] {os.path.basename(f)}{'  ← will use' if i==0 else ''}")
    return files[0]


# ─────────────────────────────────────────────
# JIRA
# ─────────────────────────────────────────────
def jira_get(path, params=None):
    import requests
    headers = {'Authorization': 'Bearer ' + CONFIG['JIRA_PAT'], 'Accept': 'application/json'}
    return requests.get(f"{CONFIG['JIRA_BASE_URL']}/rest/api/2/{path}",
                        headers=headers, params=params or {}, verify=False, timeout=15)

def test_connection():
    try:
        r = jira_get('myself')
        if r.status_code == 200:
            me = r.json()
            print(f"  ✅ {me.get('displayName')} ({me.get('name')})")
            return True
        print(f"  ❌ Auth failed ({r.status_code})")
        return False
    except Exception as e:
        print(f"  ❌ {e}"); return False

def find_parent_key():
    safe = re.sub(r'[\[\]\(\)\{\}\+\-\!\*\?\^\"\\~]', ' ', CONFIG['PARENT_TASK']).strip()
    try:
        r = jira_get('search', {'jql': f'project="{CONFIG["PROJECT"]}" AND issuetype=Task AND text~"{safe}"',
                                'maxResults': 20, 'fields': 'summary'})
        if r.status_code == 200:
            for issue in r.json().get('issues', []):
                if issue['fields']['summary'].strip() == CONFIG['PARENT_TASK'].strip():
                    return issue['key']
    except: pass
    start = 0
    while True:
        try:
            r = jira_get('search', {'jql': f'project="{CONFIG["PROJECT"]}" AND issuetype=Task ORDER BY created DESC',
                                    'maxResults': 100, 'startAt': start, 'fields': 'summary'})
            if r.status_code != 200: break
            data = r.json()
            for issue in data.get('issues', []):
                if issue['fields']['summary'].strip() == CONFIG['PARENT_TASK'].strip():
                    return issue['key']
            if start + 100 >= data.get('total', 0): break
            start += 100
        except: break
    return None

def fetch_subtasks(parent_key):
    tasks = []
    try:
        r = jira_get('search', {
            'jql': f'parent={parent_key} ORDER BY created ASC',
            'maxResults': 100,
            'fields': 'summary,status,assignee,comment,duedate,description'
        })
        if r.status_code == 200:
            for issue in r.json().get('issues', []):
                f = issue['fields']
                bu = ''
                for c in reversed(f.get('comment', {}).get('comments', [])):
                    body = c.get('body', '')
                    if 'Auto-refreshed' not in body and body.strip():
                        bu = body.strip()[:80].replace('\n', ' ')
                        break
                # Extract "Action:" section from description
                desc = f.get('description', '') or ''
                action = ''
                action_match = re.search(r'\*?Action:\*?\s*\n+(.*?)(?:\n\n|$)', desc, re.DOTALL)
                if action_match:
                    action = action_match.group(1).strip().replace('\n', ' ')
                # Trim "Deadline..." from action text for cleaner display
                action = re.sub(r'\.?\s*Deadline\s+\S+', '', action).strip()
                # No truncation — show full action text

                tasks.append({
                    'key':           issue['key'],
                    'title':         f.get('summary', ''),
                    'status':        f.get('status', {}).get('name', 'To Do'),
                    'assignee':      (f.get('assignee') or {}).get('displayName', ''),
                    'assignee_name': (f.get('assignee') or {}).get('name', ''),
                    'due':           f.get('duedate', '') or '',
                    'bu':            bu,
                    'action':        action,
                })
    except Exception as e:
        print(f"  Warning: {e}")
    return tasks


# ─────────────────────────────────────────────
# TABLE XML BUILDER — creates a full table from scratch
# Matches original template style exactly
# ─────────────────────────────────────────────
PINK   = 'F95396'
BLUE   = '003E85'
WHITE  = 'FFFFFF'
BORDER = '000000'

def esc(t):
    return str(t).replace('&','&amp;').replace('<','&lt;').replace('>','&gt;').replace('"','&quot;')

def tc(text, color='111111', bold=False, align='l', bg=None, sz='1000'):
    """Build a single table cell."""
    b     = ' b="1"' if bold else ''
    fill  = f'<a:solidFill><a:srgbClr val="{bg}"/></a:solidFill>' if bg else '<a:noFill/>'
    tfill = f'<a:solidFill><a:srgbClr val="{color}"/></a:solidFill>'
    border = (
        f'<a:lnL w="9525"><a:solidFill><a:srgbClr val="{BORDER}"/></a:solidFill><a:prstDash val="solid"/></a:lnL>'
        f'<a:lnR w="9525"><a:solidFill><a:srgbClr val="{BORDER}"/></a:solidFill><a:prstDash val="solid"/></a:lnR>'
        f'<a:lnT w="9525"><a:solidFill><a:srgbClr val="{BORDER}"/></a:solidFill><a:prstDash val="solid"/></a:lnT>'
        f'<a:lnB w="9525"><a:solidFill><a:srgbClr val="{BORDER}"/></a:solidFill><a:prstDash val="solid"/></a:lnB>'
    )
    return (
        f'<a:tc>'
        f'<a:txBody><a:bodyPr/><a:lstStyle/>'
        f'<a:p><a:pPr algn="{align}"><a:buNone/></a:pPr>'
        f'<a:r><a:rPr{b} lang="vi" sz="{sz}" dirty="0">'
        f'{tfill}'
        f'<a:latin typeface="Inter Tight"/>'
        f'<a:ea typeface="Inter Tight"/>'
        f'<a:cs typeface="Inter Tight"/>'
        f'</a:rPr>'
        f'<a:t>{esc(text)}</a:t></a:r>'
        f'</a:p></a:txBody>'
        f'<a:tcPr marL="45700" marR="45700" marT="22850" marB="22850">'
        f'{fill}{border}'
        f'</a:tcPr>'
        f'</a:tc>'
    )

def build_table_xml(tasks):
    """
    Build complete <p:graphicFrame> containing the task table.
    Position: x=249450 y=1088175 (matches original template position)
    Width: 8229600 (full width), Height: scales with rows
    """
    # Column widths in EMU (match original template proportions)
    # Total width = 8229600 EMU
    col_w = [2880360, 3456432, 822960, 1069848]  # Task 35%, Chi tiết 42%, Status 10%, P.I.C 13%

    # Header row
    header_cells = (
        tc('Task',       PINK, bold=True, align='l', bg='FFF0F5', sz='1000') +
        tc('Chi tiết',   PINK, bold=True, align='l', bg='FFF0F5', sz='1000') +
        tc('Status',     PINK, bold=True, align='ctr', bg='FFF0F5', sz='1000') +
        tc("P.I.C",      PINK, bold=True, align='l', bg='FFF0F5', sz='1000')
    )
    header_row = f'<a:tr h="270000">{header_cells}</a:tr>'

    # Data rows
    data_rows = ''
    max_display = 13  # show all tasks
    row_h = 143000    # row height in EMU

    for i, task in enumerate(tasks[:max_display]):
        sd, sc = status_display(task['status'])

        # Column 1: "CEO-21 Short task title"
        short_title = task['title']
        # Remove the [Online - MonDD] prefix if present
        short_title = re.sub(r'^\[\w+ - \w+\]\s*', '', short_title).strip()
        # Cut at first ' - ' only
        if ' - ' in short_title:
            short_title = short_title.split(' - ')[0].strip()
        # Format: [CEO-21] title
        task_cell = f"[{task['key']}] {short_title}"

        # Column 2: Action sentence from description
        action = task.get('action', '')
        if not action:
            # Fallback: use BU comment
            action = task.get('bu', '')

        # Column 3: Status (colored)

        # Column 4: P.I.C — use Jira username (e.g. tram.nguyen8)
        pic = task.get('assignee_name', '') or task.get('assignee', '')
        if ' - ' in pic:
            pic = pic.split(' - ')[0].strip()

        bg = 'FFFFFF' if i % 2 == 0 else 'FFF5F8'
        data_rows += (
            f'<a:tr h="{row_h}">'
            + tc(task_cell, '111111', align='l',   bg=bg, sz='800')
            + tc(action,    '595959', align='l',   bg=bg, sz='800')
            + tc(sd,        sc,       bold=True, align='ctr', bg=bg, sz='800')
            + tc(pic,       '0D5CDF', align='l',   bg=bg, sz='800')
            + '</a:tr>'
        )

    # Total table height
    total_h = 270000 + row_h * min(len(tasks), max_display)

    # tblGrid — column widths
    tbl_grid = ''.join(f'<a:gridCol w="{w}"/>' for w in col_w)

    table_xml = (
        f'<a:tbl>'
        f'<a:tblPr firstRow="1" bandRow="0"><a:noFill/></a:tblPr>'
        f'<a:tblGrid>{tbl_grid}</a:tblGrid>'
        f'{header_row}'
        f'{data_rows}'
        f'</a:tbl>'
    )

    # Wrap in graphicFrame at correct position
    graphic_frame = (
        f'<p:graphicFrame>'
        f'<p:nvGraphicFramePr>'
        f'<p:cNvPr id="501" name="TaskTable"/>'
        f'<p:cNvGraphicFramePr><a:graphicFrameLocks noGrp="1"/></p:cNvGraphicFramePr>'
        f'<p:nvPr/>'
        f'</p:nvGraphicFramePr>'
        f'<p:xfrm>'
        f'<a:off x="249450" y="1088175"/>'
        f'<a:ext cx="8229600" cy="{total_h}"/>'
        f'</p:xfrm>'
        f'<a:graphic>'
        f'<a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/table">'
        f'{table_xml}'
        f'</a:graphicData>'
        f'</a:graphic>'
        f'</p:graphicFrame>'
    )
    return graphic_frame


# ─────────────────────────────────────────────
# SLIDE 3 — remove old table, insert fresh one
# ─────────────────────────────────────────────
def update_slide3(xml, tasks):
    # Remove existing graphicFrame (table) if present
    xml = re.sub(r'<p:graphicFrame>.*?</p:graphicFrame>', '', xml, flags=re.DOTALL)

    # Insert fresh table just before </p:spTree>
    new_table = build_table_xml(tasks)
    xml = xml.replace('</p:spTree>', new_table + '</p:spTree>')

    return xml


# ─────────────────────────────────────────────
# SLIDE 2 — update KPI numbers + month label
# ─────────────────────────────────────────────
def replace_in_shape(xml, x_anchor, new_value):
    """Replace first digit-only <a:t> inside the shape at x_anchor position."""
    parts = re.split(r'(?=<p:sp>)', xml)
    result = []
    replaced = False
    for part in parts:
        if not replaced and f'<a:off x="{x_anchor}"' in part:
            part = re.sub(r'(<a:t>)\d+(</a:t>)',
                          lambda m: m.group(1) + str(new_value) + m.group(2),
                          part, count=1)
            replaced = True
        result.append(part)
    return ''.join(result)


def fix_title_style(xml):
    """Make slide 2 title match slide 3: black, not bold/pink."""
    parts = re.split(r'(?=<p:sp>)', xml)
    result = []
    for part in parts:
        if 'Performance BU Online' in part:
            part = re.sub(r'<a:rPr\b[^/]*\bb="1"[^>]*>.*?</a:rPr>',
                          '<a:rPr lang="vi" sz="3600"/>', part, flags=re.DOTALL)
            part = re.sub(r'<a:endParaRPr\b[^/]*\bb="1"[^>]*>.*?</a:endParaRPr>',
                          '<a:endParaRPr lang="vi" sz="3600"/>', part, flags=re.DOTALL)
        result.append(part)
    return ''.join(result)


def find_kpi_positions(xml):
    """
    Auto-detect x positions of KPI shapes by label text.
    Works with any version of the template.
    Returns dict: {name: x_position_string}
    """
    shapes = re.findall(r'<p:sp>.*?</p:sp>', xml, re.DOTALL)
    pos = {}
    number_shapes = []  # shapes that contain only digits (the value shapes)

    for sp in shapes:
        texts = [t for t in re.findall(r'<a:t>([^<]*)</a:t>', sp) if t.strip()]
        x = re.search(r'<a:off x="(\d+)"', sp)
        if not x:
            continue
        xval = x.group(1)
        joined = ' '.join(texts).lower()

        if 'inprogress' in joined or 'in progress' in joined:
            # Find the paired number shape nearby (next shape with only digits)
            continue
        if 'pending' in joined and 'Tỷ' not in ' '.join(texts):
            continue

        # Identify number-only shapes
        nums = re.findall(r'<a:t>(\d+)</a:t>', sp)
        pct_merged = re.findall(r'<a:t>(\d+%)</a:t>', sp)

        if pct_merged:
            pos['pct_merged'] = xval  # "20%" in one node
        elif '%' in joined and nums:
            pos['pct_split'] = xval   # "20" + "%" as separate nodes

    # Simpler approach: find shapes by their template value
    for sp in shapes:
        texts = [t for t in re.findall(r'<a:t>([^<]*)</a:t>', sp) if t.strip()]
        x = re.search(r'<a:off x="(\d+)"', sp)
        if not x:
            continue
        xval = x.group(1)

        if texts == ['100']:
            pos['total'] = xval
        elif texts == ['20'] and 'done' not in pos:
            pos['done'] = xval         # first '20' = done
        elif texts == ['20'] and 'done' in pos:
            pos['pct_split'] = xval    # second '20' = pct
        elif texts == ['30']:
            pos['inprog'] = xval
        elif texts == ['50']:
            pos['pending'] = xval

    return pos


def update_slide2(xml, tasks, month_label):
    total   = len(tasks)
    done    = sum(1 for t in tasks if any(x in t['status'].lower() for x in ['done','closed','resolved']))
    inprog  = sum(1 for t in tasks if 'progress' in t['status'].lower())
    pending = total - done - inprog
    pct     = round(done / total * 100) if total > 0 else 0

    # Fix title style to match slide 3
    xml = fix_title_style(xml)

    # Update month label
    xml = re.sub(r'(<a:t>Performance BU Online - )\w+(</a:t>)',
                 rf'\g<1>{month_label}\2', xml)

    # Auto-detect shape positions — works with any template version
    pos = find_kpi_positions(xml)

    if pos.get('total'):   xml = replace_in_shape(xml, pos['total'],     total)
    if pos.get('done'):    xml = replace_in_shape(xml, pos['done'],      done)
    if pos.get('inprog'):  xml = replace_in_shape(xml, pos['inprog'],    inprog)
    if pos.get('pending'): xml = replace_in_shape(xml, pos['pending'],   pending)

    # PCT — handle both merged "20%" and split "20"+"%" formats
    if pos.get('pct_merged'):
        parts = re.split(r'(?=<p:sp>)', xml)
        result = []
        for part in parts:
            if f'<a:off x="{pos["pct_merged"]}"' in part:
                part = re.sub(r'(<a:t>)\d+(%</a:t>)', rf'\g<1>{pct}\2', part)
            result.append(part)
        xml = ''.join(result)
    elif pos.get('pct_split'):
        xml = replace_in_shape(xml, pos['pct_split'], pct)

    return xml




# ─────────────────────────────────────────────
# PPTX ZIP READ/WRITE
# ─────────────────────────────────────────────
def read_slide(pptx_path, num):
    with zipfile.ZipFile(pptx_path, 'r') as z:
        return z.read(f'ppt/slides/slide{num}.xml').decode('utf-8')

def write_pptx(src, dst, updates):
    """Copy src to dst, replacing specified slides. Preserves all zip metadata."""
    with zipfile.ZipFile(src, 'r') as zin:
        entries = {info.filename: (info, zin.read(info.filename)) for info in zin.infolist()}

    for num, xml in updates.items():
        key = f'ppt/slides/slide{num}.xml'
        if key in entries:
            old_info = entries[key][0]
            new_info = zipfile.ZipInfo(old_info.filename, old_info.date_time)
            new_info.compress_type  = old_info.compress_type
            new_info.external_attr  = old_info.external_attr
            entries[key] = (new_info, xml.encode('utf-8'))

    with zipfile.ZipFile(dst, 'w') as zout:
        for name, (info, data) in entries.items():
            zout.writestr(info, data)

    # Integrity check
    with zipfile.ZipFile(dst, 'r') as z:
        bad = z.testzip()
        if bad:
            raise RuntimeError(f"ZIP corrupted: {bad}")


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
def main():
    print()
    parser = argparse.ArgumentParser()
    parser.add_argument('--pptx',    default=None)
    parser.add_argument('--month',   default=None)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()

    dry_run     = args.dry_run or CONFIG['DRY_RUN']
    month_label = args.month or datetime.now().strftime('%b')

    print('=' * 60)
    print('  UpdateFromJira — Slides 2 & 3')
    print(f'  Month : {month_label}')
    print(f'  Parent: {CONFIG["PARENT_TASK"]} in [{CONFIG["PROJECT"]}]')
    print(f'  Mode  : {"DRY RUN" if dry_run else "LIVE UPDATE"}')
    print('=' * 60)

    # 1. Find PPTX
    print('\n[1/4] Finding PPTX...')
    pptx = find_pptx(args.pptx)
    if not pptx:
        input('\nPress Enter to exit...'); return
    print(f'  ✅ {os.path.basename(pptx)}')

    # 2. Jira connection
    print('\n[2/4] Connecting to Jira...')
    if not test_connection():
        input('\nPress Enter to exit...'); return

    # 3. Fetch tasks
    print(f'\n[3/4] Fetching tasks [{CONFIG["PARENT_TASK"]}]...')
    parent_key = find_parent_key()
    if not parent_key:
        print(f'  ❌ Parent "{CONFIG["PARENT_TASK"]}" not found.')
        input('\nPress Enter to exit...'); return
    print(f'  ✅ Parent: [{parent_key}]')

    tasks = fetch_subtasks(parent_key)
    if not tasks:
        print('  ❌ No sub-tasks found.')
        input('\nPress Enter to exit...'); return

    total   = len(tasks)
    done    = sum(1 for t in tasks if any(x in t['status'].lower() for x in ['done','closed','resolved']))
    inprog  = sum(1 for t in tasks if 'progress' in t['status'].lower())
    pending = total - done - inprog
    pct     = round(done / total * 100) if total > 0 else 0

    print(f'\n  {total} tasks  ·  {done} Done  ·  {inprog} In Progress  ·  {pending} To Do/Pending  ·  {pct}% complete\n')
    print(f'  {"#":<3} {"KEY":<10} {"STATUS":<14} {"TITLE"}')
    print('  ' + '─' * 72)
    for i, t in enumerate(tasks, 1):
        sd, _ = status_display(t['status'])
        print(f'  {i:<3} {t["key"]:<10} {sd:<14} {t["title"][:50]}')

    print(f'\n  Slide 2: total={total}, done={done}, inprog={inprog}, pending={pending}, {pct}%')
    print(f'  Slide 3: fresh table with all {total} task(s)')

    if dry_run:
        print('\n  ✅ Dry run complete — no files changed.')
        print('  Run without --dry-run to generate the updated PPTX.')
        input('\nPress Enter to exit...'); return

    confirm = input('\n  Update slides 2 & 3? (YES to confirm): ').strip()
    if confirm.upper() != 'YES':
        print('  Cancelled.')
        input('\nPress Enter to exit...'); return

    # 4. Update slides
    print('\n[4/4] Updating PPTX...')
    s2 = read_slide(pptx, 2)
    s3 = read_slide(pptx, 3)

    assert '2006' in s2, 'Slide 2 namespace issue'
    assert '2006' in s3, 'Slide 3 namespace issue'

    s2 = update_slide2(s2, tasks, month_label)
    s3 = update_slide3(s3, tasks)

    assert '2006' in s2, 'Slide 2 corrupted!'
    assert '2006' in s3, 'Slide 3 corrupted!'

    base      = os.path.splitext(pptx)[0]
    timestamp = datetime.now().strftime('%Y%m%d_%H%M')
    out       = f'{base}_updated_{timestamp}.pptx'

    write_pptx(pptx, out, {2: s2, 3: s3})

    print(f'  ✅ Saved: {os.path.basename(out)}')
    print(f'  📁 {os.path.dirname(out)}')
    print('=' * 60)
    input('\nDone! Press Enter to exit...')


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\n  Cancelled.')
    except Exception as e:
        import traceback
        print('\n  UNEXPECTED ERROR:')
        traceback.print_exc()
        input('\nPress Enter to exit...')
