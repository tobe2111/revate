/**
 * 유어팀 제휴 제안 메일 — 구글 시트 연결 코드 (Apps Script)
 *
 * 이 코드는 "유어애즈 파트너 풀 (자동 동기화)" 시트의
 * [확장 프로그램] → [Apps Script] 에 붙여넣어 사용합니다.
 *
 * 하는 일
 *  - 매일 보낼 대상(통신판매 · 이메일 있음 · 아직 안 보냄 · 대행사 아님 · 수신거부 아님)을 골라서 알려줌
 *  - 보낸 결과를 "자동발송_로그" 탭과 "발송기록" 탭에 기록
 *  - 수신거부 / 반송된 주소를 "수신거부" 탭에 모아서 다음부터 제외
 *
 * 메일을 직접 보내지는 않습니다(발송은 GitHub 에서 다음 메일 서버로 함).
 */

const TAB_POOL = 'pool';
const TAB_LOG = '자동발송_로그';
const TAB_SENT = '발송기록';
const TAB_SUPPRESS = '수신거부';

// pool 탭 열 번호 (A=1)
const COL_ID = 1;      // A  ID
const COL_TYPE = 3;    // C  업종
const COL_SUB = 4;     // D  세부업종
const COL_NAME = 5;    // E  업체명
const COL_EMAIL = 8;   // H  이메일
const COL_SENT = 19;   // S  이메일 보냄

const TARGET_SUBTYPE = '통신판매';
const AGENCY_TYPE = '대행사';
const AGENCY_NAME_RE = /마케팅|광고|애드|바이럴|에이전시|대행|브랜딩|퍼포먼스|홍보|미디어|marketing|agency|media/i;
const EMAIL_RE = /^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$/;

/* ───────────────────── 처음 한 번만 실행 ───────────────────── */

/** 처음 한 번 실행: 필요한 탭을 만들고, 비밀 토큰을 만들어 실행 로그에 보여줍니다. */
function setup() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  ensureTab_(ss, TAB_LOG, ['발송일시(KST)', 'pool ID', '업체명', '이메일', '결과']);
  ensureTab_(ss, TAB_SUPPRESS, ['이메일', '이유', '등록일시(KST)']);
  const props = PropertiesService.getScriptProperties();
  let token = props.getProperty('API_TOKEN');
  if (!token) {
    token = Utilities.getUuid().replace(/-/g, '') + Utilities.getUuid().replace(/-/g, '');
    props.setProperty('API_TOKEN', token);
  }
  Logger.log('SHEET_API_TOKEN (GitHub 에 등록할 값): ' + token);
}

/** 미리보기: 오늘 보낼 대상 수와 앞 10곳을 실행 로그에 보여줍니다(메일은 안 보냄). */
function preview() {
  const r = computeTargets_(10);
  Logger.log('남은 전체 대상: ' + r.remaining + '곳');
  r.targets.forEach(function (t, i) { Logger.log((i + 1) + '. ' + t.name + ' <' + t.email + '>'); });
}

/* ───────────────────── 웹 앱 (GitHub 가 호출) ───────────────────── */

function doGet(e) {
  return handle_(e, function (p) {
    if (p.action === 'ping') return { ok: true };
    if (p.action === 'targets') {
      const limit = Math.max(0, Math.min(2000, parseInt(p.limit, 10) || 0));
      return computeTargets_(limit);
    }
    throw new Error('unknown action');
  });
}

function doPost(e) {
  return handle_(e, function (p) {
    const body = JSON.parse((e.postData && e.postData.contents) || '{}');
    const lock = LockService.getScriptLock();
    lock.waitLock(30000);
    try {
      if (p.action === 'record') return record_(body.rows || []);
      if (p.action === 'suppress') return suppress_(body.items || []);
      throw new Error('unknown action');
    } finally {
      lock.releaseLock();
    }
  });
}

function handle_(e, fn) {
  let out;
  try {
    const p = (e && e.parameter) || {};
    const token = PropertiesService.getScriptProperties().getProperty('API_TOKEN');
    if (!token || p.token !== token) throw new Error('unauthorized');
    out = fn(p);
  } catch (err) {
    out = { ok: false, error: String(err && err.message || err) };
  }
  return ContentService.createTextOutput(JSON.stringify(out)).setMimeType(ContentService.MimeType.JSON);
}

/* ───────────────────── 대상 고르기 ───────────────────── */

function computeTargets_(limit) {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const pool = ss.getSheetByName(TAB_POOL);
  const n = pool.getLastRow() - 1;
  if (n <= 0) return { ok: true, targets: [], remaining: 0 };

  const col = function (c) { return pool.getRange(2, c, n, 1).getValues(); };
  const ids = col(COL_ID), types = col(COL_TYPE), subs = col(COL_SUB),
        names = col(COL_NAME), emails = col(COL_EMAIL), sent = col(COL_SENT);

  // 이미 보낸(또는 실패한) ID / 주소
  const doneIds = new Set(), doneEmails = new Set();
  const log = ss.getSheetByName(TAB_LOG);
  if (log && log.getLastRow() > 1) {
    log.getRange(2, 2, log.getLastRow() - 1, 3).getValues().forEach(function (r) {
      if (r[0] !== '') doneIds.add(String(r[0]));
      if (r[2]) doneEmails.add(String(r[2]).trim().toLowerCase());
    });
  }
  // 수신거부 / 반송
  const sup = ss.getSheetByName(TAB_SUPPRESS);
  if (sup && sup.getLastRow() > 1) {
    sup.getRange(2, 1, sup.getLastRow() - 1, 1).getValues().forEach(function (r) {
      if (r[0]) doneEmails.add(String(r[0]).trim().toLowerCase());
    });
  }

  // 마케팅 대행사 목록
  const agEmails = new Set(), agNames = new Set();
  for (let i = 0; i < n; i++) {
    if (String(types[i][0]) !== AGENCY_TYPE) continue;
    splitEmails_(emails[i][0]).forEach(function (x) { agEmails.add(x.toLowerCase()); });
    const nn = normName_(names[i][0]);
    if (nn) agNames.add(nn);
  }

  const targets = [];
  const picked = new Set();
  let remaining = 0;
  for (let i = 0; i < n; i++) {
    if (String(subs[i][0]) !== TARGET_SUBTYPE) continue;
    if (String(sent[i][0]).trim() !== '') continue;
    const id = String(ids[i][0]);
    if (doneIds.has(id)) continue;
    const cands = splitEmails_(emails[i][0]);
    if (!cands.length) continue;
    const name = String(names[i][0]);
    if (AGENCY_NAME_RE.test(name)) continue;
    if (cands.some(function (x) { return agEmails.has(x.toLowerCase()); })) continue;
    if (agNames.has(normName_(name))) continue;
    const pick = cands.filter(function (x) {
      const l = x.toLowerCase();
      return !doneEmails.has(l) && !picked.has(l);
    })[0];
    if (!pick) continue;
    picked.add(pick.toLowerCase());
    remaining++;
    if (targets.length < limit) targets.push({ id: id, name: name, email: pick });
  }
  return { ok: true, targets: targets, remaining: remaining };
}

function splitEmails_(cell) {
  return String(cell || '').split(/[,;\s]+/).map(function (x) { return x.trim(); })
    .filter(function (x) { return EMAIL_RE.test(x); });
}

function normName_(s) {
  return String(s || '').replace(/\(주\)|㈜|주식회사|유한회사|\s/g, '');
}

/* ───────────────────── 기록 ───────────────────── */

/** rows: [{ts, id, name, email, result, ok}] */
function record_(rows) {
  if (!rows.length) return { ok: true, written: 0 };
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const log = ensureTab_(ss, TAB_LOG, ['발송일시(KST)', 'pool ID', '업체명', '이메일', '결과']);
  const values = rows.map(function (r) { return [r.ts, String(r.id), r.name, r.email, r.result]; });
  log.getRange(log.getLastRow() + 1, 1, values.length, 5).setValues(values);

  const okEmails = rows.filter(function (r) { return r.ok; }).map(function (r) { return [r.email]; });
  if (okEmails.length) {
    const sent = ss.getSheetByName(TAB_SENT);
    if (sent) {
      const last = Math.max(sent.getLastRow(), 1); // A1 안내문은 그대로 둠
      sent.getRange(last + 1, 1, okEmails.length, 1).setValues(okEmails);
    }
  }
  return { ok: true, written: values.length };
}

/** items: [{email, reason}] — 이미 있는 주소는 건너뜀 */
function suppress_(items) {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const sh = ensureTab_(ss, TAB_SUPPRESS, ['이메일', '이유', '등록일시(KST)']);
  const have = new Set();
  if (sh.getLastRow() > 1) {
    sh.getRange(2, 1, sh.getLastRow() - 1, 1).getValues().forEach(function (r) {
      have.add(String(r[0]).trim().toLowerCase());
    });
  }
  const now = Utilities.formatDate(new Date(), 'Asia/Seoul', 'yyyy-MM-dd HH:mm');
  const add = [];
  items.forEach(function (it) {
    const em = String(it.email || '').trim().toLowerCase();
    if (!EMAIL_RE.test(em) || have.has(em)) return;
    have.add(em);
    add.push([em, String(it.reason || ''), now]);
  });
  if (add.length) sh.getRange(sh.getLastRow() + 1, 1, add.length, 3).setValues(add);
  return { ok: true, added: add.length };
}

function ensureTab_(ss, name, header) {
  let sh = ss.getSheetByName(name);
  if (!sh) {
    sh = ss.insertSheet(name);
    sh.getRange(1, 1, 1, header.length).setValues([header]);
    sh.setFrozenRows(1);
  }
  return sh;
}
