"""New-project setup wizard (HTML for the 9 steps of the spec).

Rendered inside the dashboard's sidebar layout. All data comes from the
project-scoped APIs in projects_api.py; nothing here writes a Jira or Lark
record except the explicit Backfill step. User strings are escaped with
html.escape on the server and esc() in the browser.
"""
import json

import config
import dashboard
import field_mappings
import projects
from dashboard import e

STEPS = [
    (1, "Name + Jira"),
    (2, "Lark table"),
    (3, "Connect Lark"),
    (4, "Copy from"),
    (5, "Check fields"),
    (6, "People"),
    (7, "Sync types"),
    (8, "Backfill"),
    (9, "Go live"),
]

_CSS = """
<style>
  .wz { padding: 24px 32px; max-width: 980px; }
  .wz h1 { font-size: 20px; margin-bottom: 4px; }
  .wz .sub { color: #64748b; font-size: 13px; margin-bottom: 16px; }
  .stepper { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 18px; }
  .stepper a { padding: 6px 10px; border-radius: 6px; background: #e2e8f0; color: #334155;
               font-size: 12px; text-decoration: none; }
  .stepper a.locked { opacity: .45; cursor: not-allowed; }
  .stepper a.cur { background: #0f172a; color: #fff; font-weight: 600; }
  .wz .panel { background: #fff; border-radius: 10px; padding: 20px 22px;
               box-shadow: 0 1px 2px rgba(0,0,0,.06); }
  .wz label { display: block; font-size: 12px; font-weight: 600; color: #475569; margin: 12px 0 4px; }
  .wz input[type=text], .wz select { width: 100%; max-width: 460px; padding: 7px 9px;
        border: 1px solid #cbd5e1; border-radius: 6px; font-size: 13px; }
  .wz button, .wz a.btn { padding: 7px 14px; border: none; border-radius: 6px; background: #0f172a;
        color: #fff; font-size: 13px; font-weight: 600; cursor: pointer; text-decoration: none;
        display: inline-block; margin: 10px 8px 0 0; }
  .wz button.sec, .wz a.btn.sec { background: #e2e8f0; color: #1e293b; }
  .wz button.danger { background: #dc2626; }
  .wz button:disabled { background: #94a3b8; cursor: not-allowed; }
  .wz .msg { margin-top: 12px; font-size: 13px; }
  .wz .ok { color: #16a34a; } .wz .bad { color: #dc2626; } .wz .warn { color: #b45309; }
  .wz table { border-collapse: collapse; width: 100%; font-size: 13px; margin-top: 10px; }
  .wz th, .wz td { text-align: left; padding: 6px 8px; border-bottom: 1px solid #e2e8f0; }
  .wz code { background: #f1f5f9; padding: 1px 5px; border-radius: 4px; word-break: break-all; }
  .wz .chk { display: inline-block; margin: 4px 14px 4px 0; font-weight: 400; font-size: 13px; color: #1e293b; }
</style>
"""

_JS = r"""
function esc(v) {
  return String(v == null ? '' : v).replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}
function $(id) { return document.getElementById(id); }
function say(id, cls, text) { var n = $(id); if (n) n.innerHTML = '<span class="' + cls + '">' + esc(text) + '</span>'; }
async function api(method, url, body) {
  var opt = {method: method, headers: {}};
  if (body !== undefined) { opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(body); }
  var r = await fetch(url, opt), j = {};
  try { j = await r.json(); } catch (x) {}
  if (!r.ok) throw new Error(j.error || j.detail || ('HTTP ' + r.status));
  if (j && j.ok === false) throw new Error(j.error || 'failed');
  return j;
}
function go(step) { location.href = '/p/' + PID + '/setup?step=' + step; }

/* ── steps 1-2 (new project) ─────────────────────────────────────────── */
var JIRA = {boards: [], types: [], name: ''}, LARK = {base: '', tables: [], table: ''};
async function checkJira() {
  var key = $('jkey').value.trim().toUpperCase();
  if (!key) { say('jmsg', 'bad', 'Enter a Jira project key'); return; }
  say('jmsg', 'warn', 'Checking...');
  try {
    var j = await api('GET', '/api/jira/projects/' + encodeURIComponent(key));
    if (!j.exists) { say('jmsg', 'bad', 'Jira project ' + key + ' was not found with the shared credentials'); return; }
    JIRA = {boards: j.boards || [], types: j.issue_types || [], name: j.name || ''};
    if (!$('pname').value.trim()) $('pname').value = j.name || key;
    var sel = $('board'); sel.innerHTML = '';
    JIRA.boards.forEach(function (b) { sel.innerHTML += '<option value="' + esc(b.id) + '">' + esc(b.name) + '</option>'; });
    $('boardrow').style.display = JIRA.boards.length ? '' : 'none';
    $('jinfo').innerHTML = '<span class="ok">Found: ' + esc(j.name) + '</span><br>Issue types: ' +
      JIRA.types.map(function (t) { return '<code>' + esc(t) + '</code>'; }).join(' ');
    $('jmsg').innerHTML = '';
  } catch (x) { say('jmsg', 'bad', x.message); }
}
async function readLink() {
  say('lmsg', 'warn', 'Reading link...');
  try {
    var j = await api('POST', '/api/lark/resolve-link', {lark_base_link: $('llink').value.trim()});
    LARK.base = j.base_token; LARK.tables = j.tables || [];
    var sel = $('tbl'); sel.innerHTML = '';
    LARK.tables.forEach(function (t) {
      sel.innerHTML += '<option value="' + esc(t.table_id) + '"' + (t.table_id === j.table_id ? ' selected' : '') +
        ' data-name="' + esc(t.name) + '">' + esc(t.name) + ' (' + esc(t.table_id) + ')</option>';
    });
    if (!LARK.tables.length) {
      if (!j.table_id) { say('lmsg', 'bad', 'The bot cannot list this Base yet and the link names no table. Use a link ending in ?table=tbl... (open the table in Lark and copy the link).'); return; }
      sel.innerHTML = '<option value="' + esc(j.table_id) + '" selected>' + esc(j.table_id) + '</option>';
      say('lmsg', 'warn', 'The bot cannot read this Base yet (' + (j.tables_error || 'no access') + '). Continue; connect Lark in the next step.');
    } else { $('lmsg').innerHTML = '<span class="ok">Base found, ' + LARK.tables.length + ' table(s)</span>'; }
    $('tblrow').style.display = '';
    $('create').disabled = false;
  } catch (x) { say('lmsg', 'bad', x.message); }
}
async function createDraft() {
  if (!JIRA.name) { say('cmsg', 'bad', 'Check the Jira project first (step 1)'); return; }
  $('create').disabled = true;
  var key = $('jkey').value.trim().toUpperCase();
  var tsel = $('tbl'), tname = tsel.selectedIndex >= 0 ? (tsel.options[tsel.selectedIndex].getAttribute('data-name') || '') : '';
  try {
    var p = await api('POST', '/api/projects', {name: $('pname').value.trim() || key, jira_project_key: key,
      lark_base_token: LARK.base, lark_table_id: tsel.value});
    var patch = {lark_table_name: tname, lark_table_id: tsel.value};
    if ($('board').value) patch.board = $('board').value;
    await api('PATCH', '/api/p/' + p.id, patch);
    location.href = '/p/' + p.id + '/setup?step=3';
  } catch (x) { say('cmsg', 'bad', x.message); $('create').disabled = false; }
}

/* ── steps 1-2 (existing draft) ──────────────────────────────────────── */
async function saveName() {
  try {
    var body = {name: $('pname').value.trim()};
    if ($('board') && $('board').value) body.board = $('board').value;
    await api('PATCH', '/api/p/' + PID, body); say('smsg', 'ok', 'Saved');
  } catch (x) { say('smsg', 'bad', x.message); }
}
async function loadBoards() {
  try {
    var j = await api('GET', '/api/jira/projects/' + encodeURIComponent(JKEY));
    if (!j.exists) { say('smsg', 'bad', 'Jira project not found'); return; }
    var sel = $('board'); sel.innerHTML = '<option value="">(none)</option>';
    (j.boards || []).forEach(function (b) { sel.innerHTML += '<option value="' + esc(b.id) + '"' + (String(b.id) === String(BOARD) ? ' selected' : '') + '>' + esc(b.name) + '</option>'; });
    $('jinfo').innerHTML = 'Issue types: ' + (j.issue_types || []).map(function (t) { return '<code>' + esc(t) + '</code>'; }).join(' ');
  } catch (x) { say('smsg', 'bad', x.message); }
}
async function loadTables() {
  try {
    var j = await api('GET', '/api/p/' + PID + '/tables');
    if (j.error) { say('tmsg', 'warn', 'Cannot list tables yet: ' + j.error + ' (connect Lark in step 3)'); return; }
    var sel = $('tbl'); sel.innerHTML = '';
    (j.tables || []).forEach(function (t) { sel.innerHTML += '<option value="' + esc(t.table_id) + '"' + (t.table_id === j.active_table_id ? ' selected' : '') + ' data-name="' + esc(t.name) + '">' + esc(t.name) + ' (' + esc(t.table_id) + ')</option>'; });
  } catch (x) { say('tmsg', 'bad', x.message); }
}
async function saveTable() {
  var sel = $('tbl');
  if (sel.selectedIndex < 0) return;
  try {
    await api('PATCH', '/api/p/' + PID, {lark_table_id: sel.value,
      lark_table_name: sel.options[sel.selectedIndex].getAttribute('data-name') || ''});
    say('tmsg', 'ok', 'Saved');
  } catch (x) { say('tmsg', 'bad', x.message); }
}

/* ── step 3 ──────────────────────────────────────────────────────────── */
async function testAccess() {
  say('amsg', 'warn', 'Testing...');
  try {
    var j = await api('GET', '/api/p/' + PID + '/tables');
    if (j.error) say('amsg', 'bad', 'Bot cannot read the Base yet: ' + j.error);
    else say('amsg', 'ok', 'Bot can read the Base (' + (j.tables || []).length + ' table(s))');
  } catch (x) { say('amsg', 'bad', x.message); }
}

/* ── step 4 ──────────────────────────────────────────────────────────── */
async function copyFrom() {
  var src = $('src').value;
  if (!src) { say('cpmsg', 'bad', 'Pick a project'); return; }
  try {
    var j = await api('POST', '/api/p/' + PID + '/copy-from', {source: parseInt(src, 10)});
    say('cpmsg', 'ok', 'Copied ' + j.copied_mappings + ' mapping(s), sync types and people. Next: check fields.');
  } catch (x) { say('cpmsg', 'bad', x.message + ' (use Delete draft below to start over)'); }
}
async function seedDefaults() {
  try {
    var j = await api('POST', '/api/p/' + PID + '/fields/seed-defaults');
    say('cpmsg', 'ok', 'Seeded ' + j.seeded + ' default mapping(s). Next: check fields.');
  } catch (x) { say('cpmsg', 'bad', x.message + ' (use Delete draft below to start over)'); }
}
async function deleteDraft() {
  if (!confirm('Delete this draft project and its field mappings? Jira and Lark records are not touched.')) return;
  try { await api('DELETE', '/api/p/' + PID); location.href = '/p/new'; }
  catch (x) { say('cpmsg', 'bad', x.message); }
}
async function showCount() {
  try {
    var j = await api('GET', '/api/p/' + PID + '/fields');
    $('mapcount').textContent = (j.mappings || []).length;
  } catch (x) {}
}

/* ── step 5 ──────────────────────────────────────────────────────────── */
var LARKCOLS = [];
async function runCheck() {
  say('chmsg', 'warn', 'Checking...');
  $('next5').disabled = true;
  try {
    var lf = await api('GET', '/api/p/' + PID + '/lark-fields');
    LARKCOLS = (lf.fields || []).map(function (f) { return f.field_name; });
    var j = await api('GET', '/api/p/' + PID + '/check');
    var html = '';
    j.fields.forEach(function (pr, i) {
      html += '<tr><td>' + esc(pr.problem) + '</td><td>';
      if (pr.mapping_id) {
        html += '<select id="rp' + i + '"><option value="">re-point to...</option>' +
          LARKCOLS.map(function (c) { return '<option>' + esc(c) + '</option>'; }).join('') + '</select> ' +
          '<button class="sec" onclick="repoint(' + pr.mapping_id + ',' + i + ')">Re-point</button>' +
          '<button class="danger" onclick="dropMap(' + pr.mapping_id + ')">Delete mapping</button>';
      } else if (pr.jira_field && REQUIRED[pr.jira_field]) {
        html += '<button class="sec add-default" data-jira-field="' + esc(pr.jira_field) + '">Add default mapping</button>';
      }
      html += '</td></tr>';
    });
    $('chtable').innerHTML = html ? '<table><tr><th>Problem</th><th>Fix</th></tr>' + html + '</table>' : '';
    Array.prototype.forEach.call(document.querySelectorAll('#chtable .add-default'), function (b) {
      b.addEventListener('click', function () { addDefault(b.getAttribute('data-jira-field')); });
    });
    var warn = '';
    if (j.people && (j.people.unmapped_jira.length || j.people.unknown_lark.length))
      warn = 'People still to review in step 6: ' + j.people.unmapped_jira.length + ' Jira user(s) unmapped, ' + j.people.unknown_lark.length + ' mapped name(s) not in the Lark Assignee options.';
    $('chpeople').textContent = warn;
    setLock(!j.ready);
    if (j.ready) { say('chmsg', 'ok', 'All mappings match. You can continue.'); $('next5').disabled = false; }
    else say('chmsg', 'bad', j.fields.length + ' problem(s). Re-point or delete each mapped column (deleting means that field does not sync) before continuing. Title, Jira Key and Type are required.');
  } catch (x) { say('chmsg', 'bad', x.message); }
}
function setLock(on) {
  Array.prototype.forEach.call(document.querySelectorAll('.stepper a[data-lock]'), function (a) {
    if (on) { a.classList.add('locked'); a.setAttribute('href', '#'); a.onclick = function () { return false; }; }
    else { a.classList.remove('locked'); a.setAttribute('href', a.getAttribute('data-href')); a.onclick = null; }
  });
}
async function repoint(id, i) {
  var v = $('rp' + i).value; if (!v) return;
  try { await api('POST', '/api/p/' + PID + '/fields', {id: id, lark_field: v}); runCheck(); }
  catch (x) { say('chmsg', 'bad', x.message); }
}
async function dropMap(id) {
  try { await api('DELETE', '/api/p/' + PID + '/fields/' + id); runCheck(); }
  catch (x) { say('chmsg', 'bad', x.message); }
}
async function addDefault(jf) {
  try { await api('POST', '/api/p/' + PID + '/fields', REQUIRED[jf]); runCheck(); }
  catch (x) { say('chmsg', 'bad', x.message); }
}

/* ── step 6 ──────────────────────────────────────────────────────────── */
var PEOPLE = {};
// Lark name "" = ignored: that person's Jira assignment is never synced to Lark.
function personRow(k, v) {
  var ign = !String(v || '').trim();
  return '<tr><td>' + esc(k) + '</td><td>' + (ign ? '<em>(ignored)</em>' : esc(v)) +
    '</td><td><button class="sec" data-k="' + esc(k) + '" onclick="rmPerson(this.dataset.k)">Remove</button></td></tr>';
}
function drawPeople() {
  var h = '';
  Object.keys(PEOPLE).sort().forEach(function (k) { h += personRow(k, PEOPLE[k]); });
  $('ptable').innerHTML = '<table><tr><th>Jira user</th><th>Lark name</th><th></th></tr>' + (h || '<tr><td colspan="3">No one mapped yet. A Jira assignee not listed here is added automatically under their Jira name.</td></tr>') + '</table>';
}
function rmPerson(k) { delete PEOPLE[k]; drawPeople(); }
// Returns '' when added, else an error message (nothing is added).
function addPersonTo(people, j, l, ign) {
  j = (j || '').trim(); l = (l || '').trim();
  if (!j) return 'Enter a Jira user.';
  if (!ign && !l) return 'Enter a Lark name for ' + j + ', or tick Ignore.';
  people[j] = ign ? '' : l;
  return '';
}
function addPerson() {
  var err = addPersonTo(PEOPLE, $('pj').value, $('pl').value, $('pign').checked);
  if (err) { say('pmsg', 'bad', err); return; }
  $('pj').value = ''; $('pl').value = ''; $('pign').checked = false; drawPeople();
}
async function loadPeople() {
  try {
    PEOPLE = (await api('GET', '/api/p/' + PID + '/people')).assignee_map || {};
    drawPeople();
    var s = await api('GET', '/api/p/' + PID + '/people/suggestions');
    $('jl').innerHTML = (s.jira_users || []).map(function (n) { return '<option value="' + esc(n) + '">'; }).join('');
    $('ll').innerHTML = (s.lark_options || []).map(function (n) { return '<option value="' + esc(n) + '">'; }).join('');
    var notes = [];
    if (s.jira_error) notes.push('Jira users: ' + s.jira_error);
    if (s.lark_error) notes.push('Lark options: ' + s.lark_error);
    var unm = (s.jira_users || []).filter(function (n) { return !(n in PEOPLE); });
    if (unm.length) notes.push('Jira users not mapped yet: ' + unm.join(', '));
    $('pnote').textContent = notes.join(' | ');
  } catch (x) { say('pmsg', 'bad', x.message); }
}
async function savePeople() {
  try { await api('PUT', '/api/p/' + PID + '/people', {assignee_map: PEOPLE}); say('pmsg', 'ok', 'Saved'); }
  catch (x) { say('pmsg', 'bad', x.message); }
}

/* ── step 7 ──────────────────────────────────────────────────────────── */
async function loadTypes() {
  try {
    var cur = await api('GET', '/api/p/' + PID + '/sync-types');
    var jt = [], lt = [];
    try { var j = await api('GET', '/api/jira/projects/' + encodeURIComponent(JKEY)); jt = j.issue_types || []; } catch (x) {}
    try { lt = (await api('GET', '/api/p/' + PID + '/lark-type-options')).options || []; } catch (x) {}
    function box(name, all, on) {
      var set = {}; all.concat(on).forEach(function (t) { set[t] = 1; });
      return Object.keys(set).sort().map(function (t) {
        return '<label class="chk"><input type="checkbox" name="' + name + '" value="' + esc(t) + '"' + (on.indexOf(t) >= 0 ? ' checked' : '') + '> ' + esc(t) + '</label>';
      }).join('');
    }
    $('jt').innerHTML = box('jt', jt, cur.allowed_jira || []);
    $('lt').innerHTML = box('lt', lt, cur.allowed_lark || []);
  } catch (x) { say('tymsg', 'bad', x.message); }
}
function checked(name) { return Array.prototype.map.call(document.querySelectorAll('input[name=' + name + ']:checked'), function (n) { return n.value; }); }
async function saveTypes() {
  try { await api('POST', '/api/p/' + PID + '/sync-types', {jira_types: checked('jt'), lark_types: checked('lt')}); say('tymsg', 'ok', 'Saved'); }
  catch (x) { say('tymsg', 'bad', x.message); }
}

/* ── step 8 ──────────────────────────────────────────────────────────── */
async function runBackfill() {
  if (!confirm('Backfill links existing records and CREATES missing records in Jira and Lark for this project. Run it now?')) return;
  $('bf').disabled = true; say('bfmsg', 'warn', 'Running...');
  try {
    var j = await api('POST', '/api/p/' + PID + '/backfill');
    if (j.error) { say('bfmsg', 'bad', j.error); $('bf').disabled = false; return; }
    $('bfmsg').innerHTML = '<span class="ok">Done.</span> Duplicates removed: ' + esc(j.removed_duplicates) +
      ', matched: ' + esc(j.matched) + ', ambiguous skipped: ' + esc(j.skipped_ambiguous) +
      ', created in Lark: ' + esc(j.created_lark) + ', created in Jira: ' + esc(j.created_jira) + ', synced: ' + esc(j.synced);
  } catch (x) { say('bfmsg', 'bad', x.message); }
  $('bf').disabled = false;
}

/* ── step 9 ──────────────────────────────────────────────────────────── */
async function goLive() {
  $('live').disabled = true;
  try {
    await api('PATCH', '/api/p/' + PID, {setup_state: 'active', sync_enabled: true, reconcile_enabled: $('rec').checked});
    location.href = '/p/' + PID;
  } catch (x) { say('lvmsg', 'bad', x.message); $('live').disabled = false; }
}
"""


def _page(title: str, selected: "int | None", body: str, script: str) -> str:
    side = dashboard.sidebar(dashboard._sidebar_projects(), selected)
    return dashboard._shell(title, side, _CSS + f'<div class="wz">{body}</div>', f"<script>{script}</script>")


def _stepper(pid: "int | None", cur: int) -> str:
    out = ""
    for n, label in STEPS:
        cls = ' class="cur"' if n == cur else ""
        if pid is not None and cur <= 5 and n > 5:
            # Inert until step 5's check has passed (runCheck unlocks them).
            out += (f'<a{cls} class="locked" data-lock="1" href="#" '
                    f'data-href="/p/{pid}/setup?step={n}" onclick="return false">{n}. {e(label)}</a>')
        elif pid is None:
            out += f'<a{cls} href="#" onclick="return false">{n}. {e(label)}</a>'
        else:
            out += f'<a{cls} href="/p/{pid}/setup?step={n}">{n}. {e(label)}</a>'
    return f'<div class="stepper">{out}</div>'


def _panel(inner: str) -> str:
    return f'<div class="panel">{inner}</div>'


def _jira_form(prefilled: bool) -> str:
    return """
<label for="pname">Project name</label>
<input type="text" id="pname" placeholder="e.g. Verve Mobile">
<label for="jkey">Jira project key</label>
<input type="text" id="jkey" placeholder="e.g. VRM" style="text-transform:uppercase">
<button type="button" onclick="checkJira()">Check Jira project</button>
<div class="msg" id="jmsg"></div>
<div class="msg" id="jinfo"></div>
<div id="boardrow" style="display:none"><label for="board">Board</label><select id="board"></select></div>"""


def render_new() -> str:
    body = (
        '<h1>New project</h1><div class="sub">Saved as a draft with sync off until you go live.</div>'
        + _stepper(None, 1)
        + _panel(
            "<h3>Step 1 - Name + Jira</h3>" + _jira_form(False)
            + '<h3 style="margin-top:22px">Step 2 - Lark Base</h3>'
            '<label for="llink">Lark Base link</label>'
            '<input type="text" id="llink" placeholder="https://.../base/...?table=tbl...">'
            '<button type="button" onclick="readLink()">Read link</button>'
            '<div class="msg" id="lmsg"></div>'
            '<div id="tblrow" style="display:none"><label for="tbl">Table</label><select id="tbl"></select></div>'
            '<button type="button" id="create" disabled onclick="createDraft()">Create draft and continue</button>'
            '<div class="msg" id="cmsg"></div>'
        )
    )
    script = "var PID = null;\n" + _JS
    return _page("New project", None, body, script)


def _required_rows() -> dict:
    return {d["jira_field"]: {k: d[k] for k in ("lark_field", "jira_field", "jira_label", "direction",
                                                "field_type", "is_system", "active")}
            for d in field_mappings._DEFAULTS if d["jira_field"] in field_mappings.REQUIRED_JIRA_FIELDS}


def _clamp(step) -> int:
    try:
        n = int(step)
    except (TypeError, ValueError):
        return 3
    return min(max(n, 1), len(STEPS))


def _nav(pid: int, n: int, next_id: str = "") -> str:
    prev = f'<a class="btn sec" href="/p/{pid}/setup?step={n - 1}">Back</a>' if n > 1 else ""
    nxt = (f'<button type="button" id="{next_id}" disabled onclick="go({n + 1})">Next</button>' if next_id
           else f'<a class="btn" href="/p/{pid}/setup?step={n + 1}">Next</a>') if n < len(STEPS) else ""
    return f'<div>{prev}{nxt}</div>'


def _step_html(p: "projects.Project", n: int) -> str:
    pid = p.id
    key = e(p.jira_project_key)
    if n == 1:
        return ("<h3>Step 1 - Name + Jira</h3>"
                f'<label for="pname">Project name</label><input type="text" id="pname" value="{e(p.name)}">'
                f'<div class="msg">Jira project: <code>{key}</code> (fixed)</div><div class="msg" id="jinfo"></div>'
                '<label for="board">Board</label><select id="board"><option value="">(loading)</option></select>'
                '<button type="button" onclick="saveName()">Save</button><div class="msg" id="smsg"></div>')
    if n == 2:
        return ("<h3>Step 2 - Lark table</h3>"
                f'<div class="msg">Base: <code>{e(p.lark_base_token)}</code><br>'
                f'Current table: <code>{e(p.lark_table_name or p.lark_table_id)}</code></div>'
                '<label for="tbl">Table</label><select id="tbl"></select>'
                '<button type="button" onclick="saveTable()">Save table</button>'
                '<div class="msg" id="tmsg"></div>'
                '<div class="msg warn">Changing the table rebuilds this project\'s index in the background.</div>')
    if n == 3:
        return ("<h3>Step 3 - Connect Lark</h3>"
                "<p>Authorise once so the bot is added as editor on this project's Base and subscribed to record-change events.</p>"
                f'<a class="btn" href="/auth/start?project={pid}">Connect Lark</a>'
                '<button type="button" class="sec" onclick="testAccess()">Test bot access</button>'
                '<div class="msg" id="amsg"></div>'
                "<p style=\"margin-top:16px\">Alternative (Lark Base Automation, no bot access needed). POST record events to:</p>"
                f'<p><code>{e(config.PUBLIC_URL)}/webhook/lark-auto/{pid}</code></p>')
    if n == 4:
        others = "".join(f'<option value="{o.id}">{e(o.name)} ({e(o.jira_project_key)})</option>'
                         for o in projects.all_projects() if o.id != pid)
        return ("<h3>Step 4 - Copy from</h3>"
                "<p>Copy field mappings, sync types and people from an existing project, or start from the default mappings. "
                "Both refuse when this project already has mappings.</p>"
                f'<div class="msg">Mappings now: <b id="mapcount">?</b></div>'
                f'<label for="src">Copy from project</label><select id="src"><option value="">(choose)</option>{others}</select>'
                '<button type="button" onclick="copyFrom()">Copy</button>'
                '<button type="button" class="sec" onclick="seedDefaults()">Start from defaults</button>'
                '<div class="msg" id="cpmsg"></div>'
                '<div style="margin-top:18px"><button type="button" class="danger" onclick="deleteDraft()">Delete draft</button> '
                '<span class="sub">Removes only this draft and its mappings; Jira and Lark are never touched.</span></div>')
    if n == 5:
        return ("<h3>Step 5 - Check fields</h3>"
                "<p>Every mapped Lark column must exist in the new table (and Jira custom fields in Jira). "
                "Title, Jira Key and Type are required: without Jira Key the link between records and the index cannot work.</p>"
                '<button type="button" class="sec" onclick="runCheck()">Re-check</button>'
                '<div class="msg" id="chmsg"></div><div id="chtable"></div><div class="msg warn" id="chpeople"></div>')
    if n == 6:
        return ("<h3>Step 6 - People</h3>"
                "<p>Map Jira users to Lark Assignee names. A Jira assignee not listed here is added automatically "
                "under their Jira name. Tick <b>Ignore</b> for a person whose assignment should never be synced to Lark "
                "(the Lark Assignee is left unchanged).</p>"
                '<div id="ptable"></div><div class="msg warn" id="pnote"></div>'
                '<label for="pj">Jira user</label><input type="text" id="pj" list="jl">'
                '<datalist id="jl"></datalist>'
                '<label for="pl">Lark name</label><input type="text" id="pl" list="ll"><datalist id="ll"></datalist>'
                '<label><input type="checkbox" id="pign"> Ignore (never sync this person to Lark)</label>'
                '<button type="button" class="sec" onclick="addPerson()">Add</button>'
                '<button type="button" onclick="savePeople()">Save people</button><div class="msg" id="pmsg"></div>')
    if n == 7:
        return ("<h3>Step 7 - Sync types</h3>"
                '<label>Jira issue types</label><div id="jt"></div>'
                '<label>Lark Type options</label><div id="lt"></div>'
                '<button type="button" onclick="saveTypes()">Save types</button><div class="msg" id="tymsg"></div>')
    if n == 8:
        return ("<h3>Step 8 - Backfill</h3>"
                "<p>Links existing Jira issues and Lark records and creates the missing counterpart. "
                "This is the only wizard step that writes Jira or Lark records. Sync stays off until Go live. You can skip this step.</p>"
                '<button type="button" id="bf" onclick="runBackfill()">Run backfill</button><div class="msg" id="bfmsg"></div>')
    return ("<h3>Step 9 - Go live</h3>"
            f'<p>Turns sync on for <b>{e(p.name)}</b> (<code>{key}</code>) only. Required mappings must be present.</p>'
            '<label class="chk"><input type="checkbox" id="rec" checked> Also enable the periodic reconcile</label><br>'
            '<button type="button" id="live" onclick="goLive()">Go live</button><div class="msg" id="lvmsg"></div>'
            f'<div class="msg warn">If your Jira webhook is filtered by JQL to one project, add <code>{key}</code> '
            'to its filter in Jira &rarr; System &rarr; Webhooks.</div>')


_ON_LOAD = {2: "loadTables();", 1: "loadBoards();", 4: "showCount();", 5: "runCheck();",
            6: "loadPeople();", 7: "loadTypes();"}


def render_setup(p: "projects.Project", step) -> str:
    n = _clamp(step)
    state = "draft" if p.setup_state == "draft" else "active"
    body = (f'<h1>{e(p.name)} <span class="sub">setup ({e(state)})</span></h1>'
            f'<div class="sub"><code>{e(p.jira_project_key)}</code> &harr; <code>{e(p.lark_table_name or p.lark_table_id)}</code></div>'
            + _stepper(p.id, n)
            + _panel(_step_html(p, n) + _nav(p.id, n, "next5" if n == 5 else "")))
    script = (f"var PID = {p.id}; var JKEY = {json.dumps(p.jira_project_key)}; var BOARD = {json.dumps(p.jira_board_id)};\n"
              f"var REQUIRED = {json.dumps(_required_rows())};\n".replace("</", "<\\/")
              + _JS + _ON_LOAD.get(n, ""))
    return _page(f"Setup - {p.name}", p.id, body, script)
