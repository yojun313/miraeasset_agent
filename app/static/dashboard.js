/* 공시 Agent 대시보드 클라이언트 로직 (의존성 없음) */
(function () {
  'use strict';

  var $ = function (id) { return document.getElementById(id); };
  var THEME_KEY = 'gongsi_theme';
  var HISTORY_MAX = 12;

  var state = { last: null, view: 'live', companies: [], asking: false, live: [], usage: null, startedAt: 0, finalAnswer: '', runId: null, runQuestion: '', runQid: '' };

  // ---------- 유틸 ----------

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function store(key, fallback) {
    try {
      var raw = localStorage.getItem(key);
      return raw ? JSON.parse(raw) : fallback;
    } catch (e) { return fallback; }
  }

  function save(key, value) {
    try {
      localStorage.setItem(key, JSON.stringify(value));
      return true;
    } catch (e) {
      // 용량 초과: 오래된 항목부터 버리며 다시 시도한다
      if (Array.isArray(value) && value.length > 1) return save(key, value.slice(0, value.length - 1));
      try { localStorage.removeItem(key); } catch (e2) { /* 무시 */ }
      return false;
    }
  }

  /** 테마는 첫 페인트 전 인라인 스크립트가 raw 문자열로 읽으므로
      JSON으로 감싸지 않고 그대로 저장한다. */
  function saveTheme(value) {
    try { localStorage.setItem(THEME_KEY, value); } catch (e) { /* 저장 불가 시 무시 */ }
  }

  function toast(message) {
    var root = $('toast-root');
    if (!root) return;
    var el = document.createElement('div');
    el.className = 'toast';
    el.textContent = message;
    root.appendChild(el);
    setTimeout(function () { el.remove(); }, 2600);
  }

  function fetchJSON(url, timeoutMs, method, body) {
    var ctrl = new AbortController();
    var timer = setTimeout(function () { ctrl.abort(); }, timeoutMs || 30000);
    var opts = { signal: ctrl.signal, method: method || 'GET' };
    if (body !== undefined) {
      opts.headers = { 'Content-Type': 'application/json' };
      opts.body = JSON.stringify(body);
    }
    return fetch(url, opts)
      .then(function (res) {
        return res.text().then(function (text) {
          var data = null;
          try { data = JSON.parse(text); } catch (e) { /* 비-JSON 응답 */ }
          if (!res.ok) {
            var detail = data && data.detail ? JSON.stringify(data.detail) : text.slice(0, 200);
            throw new Error('HTTP ' + res.status + ' — ' + detail);
          }
          if (data === null) throw new Error('JSON 파싱 실패: ' + text.slice(0, 200));
          return data;
        });
      })
      .catch(function (err) {
        if (err.name === 'AbortError') throw new Error('요청 시간 초과 (' + Math.round((timeoutMs || 30000) / 1000) + '초)');
        throw err;
      })
      .finally(function () { clearTimeout(timer); });
  }

  /** 답변용 최소 마크다운 렌더러(굵게/코드/제목/목록/구분선). 입력은 항상 이스케이프 후 처리. */
  function renderMarkdown(src) {
    var lines = String(src || '').split('\n');
    var html = '';
    var listType = null;

    function closeList() {
      if (listType) { html += '</' + listType + '>'; listType = null; }
    }
    function inline(text) {
      return esc(text)
        .replace(/`([^`]+)`/g, '<code>$1</code>')
        .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
        .replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>');
    }

    lines.forEach(function (raw) {
      var line = raw.replace(/\s+$/, '');
      if (!line.trim()) { closeList(); return; }
      if (/^\s*([-*_])\1{2,}\s*$/.test(line)) { closeList(); html += '<hr />'; return; }

      var heading = line.match(/^(#{1,6})\s+(.*)$/);
      if (heading) { closeList(); html += '<h3>' + inline(heading[2]) + '</h3>'; return; }

      var ordered = line.match(/^\s*\d+[.)]\s+(.*)$/);
      if (ordered) {
        if (listType !== 'ol') { closeList(); html += '<ol>'; listType = 'ol'; }
        html += '<li>' + inline(ordered[1]) + '</li>';
        return;
      }
      var bullet = line.match(/^\s*[-*•]\s+(.*)$/);
      if (bullet) {
        if (listType !== 'ul') { closeList(); html += '<ul>'; listType = 'ul'; }
        html += '<li>' + inline(bullet[1]) + '</li>';
        return;
      }
      closeList();
      html += '<p>' + inline(line) + '</p>';
    });
    closeList();
    return html || '<p class="faint">(빈 응답)</p>';
  }

  // ---------- 테마 ----------

  $('theme-toggle').addEventListener('click', function () {
    var current = document.documentElement.getAttribute('data-theme');
    if (!current) {
      current = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
    }
    var next = current === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    saveTheme(next);
  });

  // ---------- 탭 ----------

  var PANELS = ['qa', 'filings', 'companies', 'about'];
  Array.prototype.forEach.call(document.querySelectorAll('.tab'), function (btn) {
    btn.addEventListener('click', function () {
      var target = btn.dataset.tab;
      Array.prototype.forEach.call(document.querySelectorAll('.tab'), function (b) {
        b.classList.toggle('active', b === btn);
      });
      PANELS.forEach(function (name) { $('panel-' + name).hidden = name !== target; });
      if (target === 'companies' && !state.companies.length) loadCompanies();
    });
  });

  // ---------- 서버 상태 ----------

  function refreshHealth() {
    var pill = $('health-pill');
    var text = $('health-text');
    fetchJSON('/health', 8000)
      .then(function (h) {
        var issues = [];
        if (!h.clova_key_set) issues.push('API 키 미설정');
        if (!h.fts_index_built) issues.push('인덱스 미빌드');
        pill.className = 'status-pill ' + (issues.length ? 'is-warn' : 'is-ok');
        text.textContent = issues.length ? issues.join(' · ') : (h.model || 'ok') + ' 정상';
        pill.title = JSON.stringify(h, null, 2);
      })
      .catch(function (err) {
        pill.className = 'status-pill is-err';
        text.textContent = '서버 연결 실패';
        pill.title = String(err.message || err);
      });
  }

  // ---------- 히스토리 (서버 저장 — 모든 기기에서 동일) ----------

  function renderHistory() {
    fetchJSON('/history?limit=20', 10000)
      .then(function (items) {
        var host = $('history');
        if (!items.length) {
          host.innerHTML = '<div class="faint small">아직 질의 기록이 없습니다.</div>';
          return;
        }
        host.innerHTML = items.map(function (it) {
          var meta = '<span class="badge ' + (it.ok ? 'badge-ok' : 'badge-err') + '">' +
            esc(it.seconds != null ? it.seconds + 's' : '—') + '</span>';
          if (it.tools) meta += '<span class="badge">도구 ' + it.tools + '</span>';
          return '<div class="history-row">' +
            '<button type="button" class="history-item" data-id="' + it.id + '" data-sec="' + esc(it.seconds) + '">' +
            '<span class="history-main">' +
            '<span class="history-top">' +
            '<span class="badge badge-qid">' + esc(it.question_id) + '</span>' +
            '<span class="history-q">' + esc(it.question) + '</span>' +
            '</span>' +
            '<span class="history-a">' + esc(it.answer_preview || '(답변 없음)') + '</span>' +
            '<span class="history-meta">' + meta + '</span>' +
            '</span></button>' +
            '<button type="button" class="history-del" data-del="' + it.id + '" ' +
            'title="영구 삭제" aria-label="영구 삭제">' +
            '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" ' +
            'stroke-width="2" stroke-linecap="round"><path d="M18 6L6 18M6 6l12 12"/></svg>' +
            '</button></div>';
        }).join('');
      })
      .catch(function (err) {
        $('history').innerHTML = '<div class="alert">기록을 불러오지 못했습니다: ' + esc(err.message || err) + '</div>';
      });
  }

  $('history').addEventListener('click', function (e) {
    var del = e.target.closest('.history-del');
    if (del) {
      e.stopPropagation();
      fetchJSON('/history/' + del.dataset.del, 10000, 'DELETE')
        .then(function () { toast('삭제했습니다'); renderHistory(); })
        .catch(function () { toast('삭제에 실패했습니다'); });
      return;
    }
    var btn = e.target.closest('.history-item');
    if (!btn) return;
    fetchJSON('/history/' + btn.dataset.id, 15000)
      .then(function (item) {
        $('q-input').value = item.question;
        $('qid-input').value = item.question_id || 'Q-001';
        state.last = item;
        state.live = [];
        state.usage = null;
        state.finalAnswer = item.answer || '';
        state.view = 'answer';
        $('token-bar').hidden = true;
        renderResult(item, item.seconds != null ? String(item.seconds) : '—', true);
        $('result-body').scrollTop = 0;
      })
      .catch(function (err) { toast('기록을 불러오지 못했습니다: ' + (err.message || err)); });
  });

  $('history-open').addEventListener('click', function () { openHistoryModal(); });

  // ---------- 질의 실행 ----------

  $('qa-form').addEventListener('submit', function (e) {
    e.preventDefault();
    runQuery();
  });

  $('q-input').addEventListener('keydown', function (e) {
    if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') { e.preventDefault(); runQuery(); }
  });

  $('clear-btn').addEventListener('click', function () {
    $('q-input').value = '';
    $('q-input').focus();
  });

  function runQuery() {
    if (state.asking) return;
    var question = $('q-input').value.trim();
    if (!question) { toast('질문을 입력해 주세요'); $('q-input').focus(); return; }
    var qid = $('qid-input').value.trim() || 'Q-001';

    // 실행은 서버가 맡고, 화면 갱신은 /events 로 들어오는 이벤트가 처리한다.
    // 그래야 질의를 시작한 브라우저와 다른 브라우저가 완전히 같은 화면을 본다.
    fetch('/runs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: question, session_id: qid }),
    })
      .then(function (res) {
        if (!res.ok) throw new Error('HTTP ' + res.status);
        return res.json();
      })
      .then(function () {
        $('q-input').value = '';
        bumpQuestionId(qid);
      })
      .catch(function (err) { toast('실행 요청 실패: ' + (err.message || err)); });
  }

  // ---------- 공용 이벤트 스트림 (모든 기기 동기화) ----------

  function beginRun(info) {
    state.asking = true;
    state.runId = info.run_id;
    state.runQuestion = info.question || state.runQuestion;
    state.runQid = info.question_id || state.runQid;
    state.live = [];
    state.usage = null;
    state.finalAnswer = '';
    state.view = 'live';
    state.startedAt = Date.now();
    var btn = $('ask-btn');
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> 실행 중…';
    // 입력창은 사용자가 다음 질문을 작성하는 공간이므로 절대 덮어쓰지 않는다
    // (이전에 덮어쓴 탓에 이어 붙은 질문이 그대로 실행되는 사고가 있었다).
    if (info.question) state.runQuestion = info.question;
    if (info.question_id) state.runQid = info.question_id;
    $('result-tabs').hidden = false;
    $('token-bar').hidden = false;
    resetTokenBar();
    $('result-meta').innerHTML =
      '<h2 class="card-title">응답</h2>' +
      (state.runQid ? '<span class="badge badge-qid">' + esc(state.runQid) + '</span>' : '') +
      '<span class="badge"><span class="spinner" style="width:11px;height:11px"></span> 진행 중</span>' +
      (state.runQuestion
        ? '<span class="running-q" title="' + esc(state.runQuestion) + '">' + esc(state.runQuestion) + '</span>'
        : '');
    Array.prototype.forEach.call($('result-tabs').children, function (b) {
      b.classList.toggle('active', b.dataset.view === 'live');
    });
    renderLive(true);
  }

  function endRun(result, seconds) {
    state.asking = false;
    state.runId = null;
    var btn = $('ask-btn');
    btn.disabled = false;
    btn.textContent = '질의 실행';
    state.last = result;
    state.finalAnswer = result.answer || state.finalAnswer;
    state.view = 'answer';
    renderResult(result, seconds, false);
    $('result-body').scrollTop = 0;
  }

  function connectEvents() {
    var src = new EventSource('/events');
    src.onmessage = function (e) {
      var msg;
      try { msg = JSON.parse(e.data); } catch (err) { return; }

      if (msg.type === 'run_started') {
        beginRun(msg);
      } else if (msg.type === 'run_event') {
        if (!state.asking) beginRun({ run_id: msg.run_id });
        handleEvent(msg.event);
      } else if (msg.type === 'run_done') {
        endRun(msg.result, String(msg.result.elapsed != null ? msg.result.elapsed : '—'));
      } else if (msg.type === 'history_changed') {
        renderHistory();
      }
    };
    src.onerror = function () {
      // 브라우저가 자동 재연결하지만, 끊긴 동안의 상태는 따라잡아야 한다
      setTimeout(catchUp, 3000);
    };
  }

  /** 새로 접속했거나 연결이 끊겼다 붙었을 때 서버의 현재 상태를 따라잡는다. */
  function catchUp() {
    fetchJSON('/runs/active', 10000)
      .then(function (runs) {
        var running = runs.filter(function (r) { return r.status === 'running'; });
        if (running.length) {
          var r = running[running.length - 1];
          beginRun(r);
          state.startedAt = Date.now() - (r.elapsed || 0) * 1000;
          (r.events || []).forEach(handleEvent);
          renderLive(true);
        } else if (runs.length && !state.last) {
          var last = runs[runs.length - 1];
          if (last.result) {
            (last.events || []).forEach(handleEvent);
            endRun(last.result, String(last.elapsed));
          }
        }
      })
      .catch(function () { /* 서버가 잠시 응답하지 않는 경우 — 다음 기회에 */ });
  }

  // ---------- 실시간 이벤트 ----------

  var TOOL_LABEL = {
    get_company_info: '기업 정보 조회',
    search_filings: '공시 메타데이터 검색',
    search_fulltext: '본문 키워드 검색',
    list_sections: '문서 목차 확인',
    read_section: '공시 본문 열람',
  };

  function pushLive(item) {
    item.at = ((Date.now() - (state.startedAt || Date.now())) / 1000).toFixed(1);
    state.live.push(item);
  }

  function handleEvent(ev) {
    if (ev.type === 'start') {
      state.startedAt = Date.now();
      return;
    }
    if (ev.type === 'turn') {
      pushLive({ kind: 'turn', title: '턴 ' + ev.turn + ' / ' + ev.max_turns,
                 text: '모델에게 현재까지의 정보를 전달하고 다음 행동을 요청합니다' });
    } else if (ev.type === 'tool_call') {
      pushLive({ kind: 'call', title: (TOOL_LABEL[ev.tool] || ev.tool),
                 tool: ev.tool, detail: JSON.stringify(ev.input || {}), pending: true });
    } else if (ev.type === 'tool_result') {
      // 같은 도구의 대기 중 항목을 결과로 승격
      for (var i = state.live.length - 1; i >= 0; i--) {
        var it = state.live[i];
        if (it.pending && it.tool === ev.tool) {
          it.pending = false;
          it.kind = ev.ok ? 'result' : 'error';
          it.summary = ev.summary;
          it.docs = ev.doc_ids || [];
          break;
        }
      }
    } else if (ev.type === 'reasoning') {
      pushLive({ kind: 'note', title: '모델 추론', text: ev.text });
    } else if (ev.type === 'note') {
      pushLive({ kind: ev.level === 'error' ? 'error' : 'note',
                 title: ev.level === 'info' ? '모델 메모' : '알림', text: ev.text });
    } else if (ev.type === 'answer') {
      state.finalAnswer = ev.text || '';
      pushLive({ kind: 'answer', title: ev.forced ? '수집된 정보로 답변 생성' : '최종 답변 생성',
                 text: '답변이 위쪽에 표시됩니다' });
    } else if (ev.type === 'usage') {
      updateTokenBar(ev);
      return;
    } else if (ev.type === 'done') {
      pushLive({ kind: 'answer', title: '완료', text: '총 ' + (ev.elapsed || '?') + '초 소요' });
    }
    if (state.view === 'live') renderLive(state.asking);
  }

  function resetTokenBar() {
    ['tok-prompt', 'tok-completion', 'tok-total', 'tok-calls'].forEach(function (id) {
      $(id).textContent = '0';
    });
    $('rl-text').textContent = '—';
    $('rl-reset').textContent = '';
    $('rl-fill').style.width = '100%';
    $('rl-fill').className = 'meter-fill';
  }

  function updateTokenBar(ev) {
    var c = ev.cumulative || {};
    state.usage = c;
    $('tok-prompt').textContent = (c.prompt_tokens || 0).toLocaleString();
    $('tok-completion').textContent = (c.completion_tokens || 0).toLocaleString();
    $('tok-total').textContent = (c.total_tokens || 0).toLocaleString();
    $('tok-calls').textContent = (c.calls || 0).toLocaleString();

    var rl = ev.rate_limit || {};
    var remain = parseInt(rl.remaining_tokens, 10);
    var limit = parseInt(rl.limit_tokens, 10);
    if (!isNaN(remain) && !isNaN(limit) && limit > 0) {
      var pct = Math.max(0, Math.min(100, (remain / limit) * 100));
      var fill = $('rl-fill');
      fill.style.width = pct + '%';
      fill.className = 'meter-fill' + (pct < 15 ? ' is-low' : pct < 35 ? ' is-warn' : '');
      $('rl-text').textContent = remain.toLocaleString() + ' / ' + limit.toLocaleString() + ' 토큰';
      $('rl-reset').textContent = rl.reset ? '(' + rl.reset + ' 후 리셋)' : '';
    }
  }

  var LIVE_ICON = { turn: '↻', call: '→', result: '✓', error: '!', note: 'i', answer: '★' };

  function renderLive(running) {
    var html = state.live.map(function (it) {
      var cls = 'live-item is-' + it.kind + (it.pending ? ' is-pending' : '');
      var out = '<div class="' + cls + '">' +
        '<span class="live-icon">' + (it.pending ? '<span class="spinner" style="width:11px;height:11px"></span>' : (LIVE_ICON[it.kind] || '·')) + '</span>' +
        '<div class="live-body"><div class="live-head"><span class="live-title">' + esc(it.title) + '</span>' +
        (it.summary ? '<span class="badge badge-ok">' + esc(it.summary) + '</span>' : '') +
        '<span class="live-time">+' + esc(it.at) + 's</span></div>';
      if (it.detail) out += '<div class="live-detail">' + esc(it.detail.slice(0, 300)) + '</div>';
      if (it.text) out += '<div class="live-text">' + esc(it.text.slice(0, 400)) + '</div>';
      if (it.docs && it.docs.length) {
        out += '<div class="live-docs">' + it.docs.map(function (d) {
          return '<span class="doc-chip">' + esc(d) + '</span>';
        }).join('') + '</div>';
      }
      return out + '</div></div>';
    }).join('');

    if (running) {
      html += '<div class="live-status"><span class="spinner"></span>에이전트가 공시를 검색하고 있습니다…</div>';
    }

    // 최종 답변은 타임라인 맨 아래가 아니라 맨 위에 고정한다 — 스크롤을
    // 내리지 않아도 바로 읽을 수 있어야 한다.
    var pinned = '';
    if (state.finalAnswer) {
      pinned =
        '<div class="answer-pin">' +
        '<div class="answer-pin-head"><span class="answer-pin-title">최종 답변</span>' +
        '<button class="btn btn-ghost btn-sm" type="button" id="pin-copy">복사</button></div>' +
        '<div class="answer-body">' + renderMarkdown(state.finalAnswer) + '</div></div>';
    }

    var host = $('result-body');
    var atBottom = host.scrollHeight - host.scrollTop - host.clientHeight < 80;
    host.innerHTML = pinned + '<div class="live">' + (html || '<div class="empty">시작하는 중…</div>') + '</div>';
    var pinCopy = $('pin-copy');
    if (pinCopy) {
      pinCopy.addEventListener('click', function () {
        navigator.clipboard.writeText(state.finalAnswer || '')
          .then(function () { toast('답변을 복사했습니다'); })
          .catch(function () { toast('복사에 실패했습니다'); });
      });
    }
    if (running && atBottom) host.scrollTop = host.scrollHeight;
  }

  // ---------- 결과 렌더 ----------

  function parseTrace(trace) {
    return String(trace || '').split('\n').filter(function (l) { return l.trim(); }).map(function (line) {
      var m = line.match(/^\[턴 (\d+)\]\s*도구 호출:\s*([A-Za-z_]+)\((.*)\)\s*→\s*(.*)$/);
      if (m) return { turn: m[1], tool: m[2], args: m[3], out: m[4] };
      var t = line.match(/^\[턴 (\d+)\]\s*(.*)$/);
      if (t) return { turn: t[1], text: t[2] };
      return { text: line };
    });
  }

  function renderResult(data, seconds, fromHistory) {
    var traceSteps = parseTrace(data.think_trace);
    var toolCount = state.live.length
      ? state.live.filter(function (it) { return it.tool; }).length
      : traceSteps.filter(function (s) { return s.tool; }).length;

    $('result-meta').innerHTML =
      '<h2 class="card-title">응답</h2>' +
      '<span class="badge badge-ok">200 OK</span>' +
      '<span class="badge">' + esc(seconds) + 's</span>' +
      '<span class="badge">도구 ' + toolCount + '회</span>' +
      '<span class="badge">근거 ' + (data.retrieved_context || '').length.toLocaleString() + '자</span>' +
      (state.usage && state.usage.total_tokens
        ? '<span class="badge">' + state.usage.total_tokens.toLocaleString() + ' 토큰</span>' : '') +
      (fromHistory ? '<span class="badge">기록</span>' : '');
    $('result-tabs').hidden = false;

    // 스트리밍으로 받은 라이브 타임라인이 있으면 그것을, (기록에서 불러온 경우처럼)
    // 없으면 think_trace를 파싱해 같은 모양으로 보여준다.
    var liveHtml = state.live.length
      ? null
      : (traceSteps.length
          ? '<div class="trace">' + traceSteps.map(function (s, i) {
              var body = s.tool
                ? '<div class="trace-head"><span class="badge badge-tool">' + esc(s.tool) + '</span>' +
                  '<span class="trace-arrow">→</span><span class="trace-out">' + esc(s.out) + '</span></div>' +
                  '<div class="trace-args">' + esc(s.args) + '</div>'
                : '<div class="trace-out">' + esc(s.text) + '</div>';
              return '<div class="trace-step"><span class="trace-num">' + (i + 1) + '</span>' +
                '<div class="trace-main">' + body + '</div></div>';
            }).join('') + '</div>'
          : '<div class="empty">추론 과정 기록이 없습니다.</div>');

    var views = {
      answer: '<div class="answer-body">' + renderMarkdown(data.answer) + '</div>',
      context: (data.retrieved_context || '').trim()
        ? '<pre class="pre-block">' + esc(data.retrieved_context) + '</pre>'
        : '<div class="empty">검색된 근거가 없습니다. 도구를 호출하지 못했을 수 있습니다.</div>',
      json: '<pre class="pre-block">' + esc(JSON.stringify(data, null, 2)) + '</pre>',
    };

    function paint() {
      if (state.view === 'live') {
        if (state.live.length) { renderLive(false); } else { $('result-body').innerHTML = liveHtml; }
        return;
      }
      $('result-body').innerHTML =
        '<div style="display:flex;justify-content:flex-end;margin-bottom:10px">' +
        '<button class="btn btn-ghost btn-sm" type="button" id="copy-btn">복사</button></div>' +
        views[state.view];
      var copy = $('copy-btn');
      if (copy) {
        copy.addEventListener('click', function () {
          var text = state.view === 'json' ? JSON.stringify(data, null, 2)
            : state.view === 'context' ? data.retrieved_context
            : data.answer;
          navigator.clipboard.writeText(text || '')
            .then(function () { toast('클립보드에 복사했습니다'); })
            .catch(function () { toast('복사에 실패했습니다'); });
        });
      }
    }

    Array.prototype.forEach.call($('result-tabs').children, function (btn) {
      btn.classList.toggle('active', btn.dataset.view === state.view);
      btn.onclick = function () {
        state.view = btn.dataset.view;
        $('result-body').scrollTop = 0;
        Array.prototype.forEach.call($('result-tabs').children, function (b) {
          b.classList.toggle('active', b === btn);
        });
        paint();
      };
    });
    paint();
  }

  // ---------- 공시 탐색 ----------

  var SUBTYPES = {
    periodic: ['annual', 'half', 'quarter'],
    exchange: ['단일판매공급계약체결', '단일판매공급계약해지', '신규시설투자등', '투자판단관련주요경영사항'],
    holding: ['대량보유상황보고서'],
    major: [],
  };

  $('f-group').addEventListener('change', function () {
    var list = SUBTYPES[this.value] || [];
    $('f-subtype').innerHTML = '<option value="">전체</option>' +
      list.map(function (s) { return '<option value="' + esc(s) + '">' + esc(s) + '</option>'; }).join('');
  });

  $('filings-reset').addEventListener('click', function () {
    ['f-company', 'f-from', 'f-to', 'f-keyword'].forEach(function (id) { $(id).value = ''; });
    ['f-sector', 'f-group', 'f-subtype', 'f-corr'].forEach(function (id) { $(id).value = ''; });
    $('f-limit').value = '50';
    $('f-subtype').innerHTML = '<option value="">전체</option>';
  });

  var GROUP_LABEL = { periodic: '정기', major: '주요사항', exchange: '거래소', holding: '지분' };

  $('filings-form').addEventListener('submit', function (e) {
    e.preventDefault();
    var params = new URLSearchParams();
    var map = { company: 'f-company', sector: 'f-sector', doc_group: 'f-group', doc_subtype: 'f-subtype',
                date_from: 'f-from', date_to: 'f-to', is_correction: 'f-corr', keyword: 'f-keyword', limit: 'f-limit' };
    Object.keys(map).forEach(function (key) {
      var v = $(map[key]).value.trim();
      if (v) params.set(key, v);
    });

    $('filings-result').innerHTML = '<div class="loading-row"><span class="spinner"></span>검색 중…</div>';
    fetchJSON('/filings?' + params.toString(), 30000)
      .then(function (rows) {
        $('filings-count').textContent = rows.length.toLocaleString() + '건';
        if (!rows.length) {
          $('filings-result').innerHTML = '<div class="empty">조건에 맞는 공시가 없습니다.</div>';
          return;
        }
        $('filings-result').innerHTML =
          '<div class="table-wrap"><table><thead><tr>' +
          '<th>기업</th><th>유형</th><th>보고서명</th><th>접수일</th><th>정정</th><th>doc_id</th>' +
          '</tr></thead><tbody>' +
          rows.map(function (r) {
            return '<tr>' +
              '<td>' + esc(r.listed_name || r.corp_name) + '</td>' +
              '<td><span class="badge">' + esc(GROUP_LABEL[r.doc_group] || r.doc_group) + '</span></td>' +
              '<td class="cell-title">' + esc(r.report_nm) + '</td>' +
              '<td class="cell-mono">' + esc(r.rcept_dt) + '</td>' +
              '<td>' + (r.is_correction ? '<span class="badge badge-err">정정</span>' : '<span class="faint">—</span>') + '</td>' +
              '<td class="cell-mono">' + esc(r.doc_id) + '</td>' +
              '</tr>';
          }).join('') + '</tbody></table></div>';
      })
      .catch(function (err) {
        $('filings-result').innerHTML = '<div class="alert">' + esc(err.message || err) + '</div>';
      });
  });

  // ---------- 기업 목록 ----------

  function loadCompanies() {
    fetchJSON('/companies', 20000)
      .then(function (rows) {
        state.companies = rows;
        var sectors = [];
        rows.forEach(function (r) { if (sectors.indexOf(r.sector) === -1) sectors.push(r.sector); });
        var options = '<option value="">전체 섹터</option>' +
          sectors.map(function (s) { return '<option value="' + esc(s) + '">' + esc(s) + '</option>'; }).join('');
        $('c-sector').innerHTML = options;
        $('f-sector').innerHTML = options.replace('전체 섹터', '전체');
        renderCompanies();
      })
      .catch(function (err) {
        $('companies-result').innerHTML = '<div class="alert">' + esc(err.message || err) + '</div>';
      });
  }

  function renderCompanies() {
    var q = $('c-search').value.trim().toLowerCase();
    var sector = $('c-sector').value;
    var rows = state.companies.filter(function (r) {
      if (sector && r.sector !== sector) return false;
      if (!q) return true;
      return (r.corp_name + ' ' + r.listed_name + ' ' + (r.stock_code || '')).toLowerCase().indexOf(q) !== -1;
    });
    if (!rows.length) {
      $('companies-result').innerHTML = '<div class="empty">검색 결과가 없습니다.</div>';
      return;
    }
    $('companies-result').innerHTML = '<div class="company-grid">' + rows.map(function (r) {
      return '<div class="company-card">' +
        '<div class="company-name">' + esc(r.listed_name || r.corp_name) + '</div>' +
        (r.listed_name !== r.corp_name ? '<div class="faint small">' + esc(r.corp_name) + '</div>' : '') +
        '<div class="company-meta">' +
        '<span class="badge">' + esc(r.stock_code) + '</span>' +
        '<span class="badge">' + esc(r.sector) + '</span>' +
        '</div></div>';
    }).join('') + '</div>';
  }

  $('c-search').addEventListener('input', renderCompanies);
  $('c-sector').addEventListener('change', renderCompanies);

  // ---------- API 명세 ----------

  $('api-spec').textContent =
    '# 요청\n' +
    'GET ' + window.location.origin + '/answer?question_id={id}&question={질의}\n\n' +
    '# 응답 (항상 HTTP 200)\n' +
    JSON.stringify({
      question_id: 'Q-001',
      question: '평가 질의 원문',
      retrieved_context: '답변 생성에 참고한 검색 문서',
      think_trace: '사고 · 추론 · 도구 사용 과정',
      answer: '최종 생성 답변',
    }, null, 2) +
    '\n\n# 참고 엔드포인트\n' +
    'GET /health                기동 상태 · 모델 · 인덱스 여부\n' +
    'GET /companies?query=삼성   유니버스 조회 (query 생략 시 70개사 전체)\n' +
    'GET /filings?company=&doc_group=&date_from=&...   공시 메타데이터 조회\n' +
    'POST /chat                 멀티턴 대화 (데모용)';


  // ---------- 질의 기록 모달 ----------

  var hm = {
    items: [],          // 서버에서 받은 전체 목록
    view: [],           // 검색 필터 적용 결과 (렌더 순서)
    checked: {},        // id → true
    anchor: -1,         // shift 범위 선택 기준
    activeId: null,     // 상세로 열려 있는 항목
    detail: null,       // 상세 데이터
    dview: 'answer',
  };

  function openHistoryModal() {
    $('history-modal').hidden = false;
    document.body.style.overflow = 'hidden';
    loadHistoryModal();
    setTimeout(function () { $('hm-search').focus(); }, 30);
  }

  function closeHistoryModal() {
    $('history-modal').hidden = true;
    document.body.style.overflow = '';
  }

  function loadHistoryModal() {
    return fetchJSON('/history?limit=500', 20000)
      .then(function (items) {
        hm.items = items || [];
        // 사라진 항목은 선택에서 정리
        var alive = {};
        hm.items.forEach(function (it) { alive[it.id] = true; });
        Object.keys(hm.checked).forEach(function (id) { if (!alive[id]) delete hm.checked[id]; });
        if (hm.activeId && !alive[hm.activeId]) { hm.activeId = null; hm.detail = null; }
        renderHistoryModal();
      })
      .catch(function (err) {
        $('hm-rows').innerHTML = '<div class="alert" style="margin:12px">기록을 불러오지 못했습니다: ' +
          esc(err.message || err) + '</div>';
      });
  }

  function hmFiltered() {
    var q = ($('hm-search').value || '').trim().toLowerCase();
    if (!q) return hm.items;
    return hm.items.filter(function (it) {
      return (it.question || '').toLowerCase().indexOf(q) >= 0 ||
        (it.answer_preview || '').toLowerCase().indexOf(q) >= 0 ||
        (it.question_id || '').toLowerCase().indexOf(q) >= 0;
    });
  }

  function hmSelectedIds() {
    return Object.keys(hm.checked).filter(function (id) { return hm.checked[id]; });
  }

  function renderHistoryModal() {
    hm.view = hmFiltered();
    $('hm-count').textContent = hm.view.length === hm.items.length
      ? String(hm.items.length)
      : hm.view.length + ' / ' + hm.items.length;

    if (!hm.view.length) {
      $('hm-rows').innerHTML = '<div class="faint small" style="padding:16px">' +
        (hm.items.length ? '검색 결과가 없습니다.' : '아직 질의 기록이 없습니다.') + '</div>';
    } else {
      $('hm-rows').innerHTML = hm.view.map(function (it, idx) {
        var cls = 'hm-row' + (hm.checked[it.id] ? ' is-checked' : '') +
          (hm.activeId === it.id ? ' is-active' : '');
        var meta = '<span class="badge ' + (it.ok ? 'badge-ok' : 'badge-err') + '">' +
          esc(it.seconds != null ? it.seconds + 's' : '—') + '</span>';
        if (it.tools) meta += '<span class="badge">도구 ' + it.tools + '</span>';
        if (it.ts) meta += '<span class="hm-row-date">' + esc(String(it.ts).slice(0, 16).replace('T', ' ')) + '</span>';
        return '<div class="' + cls + '" data-idx="' + idx + '" data-id="' + esc(it.id) + '">' +
          '<input type="checkbox" class="hm-row-check" data-idx="' + idx + '"' +
          (hm.checked[it.id] ? ' checked' : '') + ' aria-label="선택" />' +
          '<span class="hm-row-main">' +
          '<span class="hm-row-top">' +
          '<span class="badge badge-qid">' + esc(it.question_id) + '</span>' +
          '<span class="hm-row-q">' + esc(it.question) + '</span>' +
          '</span>' +
          '<span class="hm-row-a">' + esc(it.answer_preview || '(답변 없음)') + '</span>' +
          '<span class="hm-row-meta">' + meta + '</span>' +
          '</span></div>';
      }).join('');
    }
    renderHmSelection();
  }

  function renderHmSelection() {
    var sel = hmSelectedIds().length;
    var shown = hm.view.length;
    var shownChecked = hm.view.filter(function (it) { return hm.checked[it.id]; }).length;
    var all = $('hm-select-all');
    all.checked = shown > 0 && shownChecked === shown;
    all.indeterminate = shownChecked > 0 && shownChecked < shown;
    $('hm-select-label').textContent = sel ? sel + '건 선택됨' : '전체 선택';
    var del = $('hm-delete-selected');
    del.disabled = sel === 0;
    del.textContent = sel ? '선택 ' + sel + '건 삭제' : '선택 삭제';
  }

  function hmSetChecked(idx, on) {
    var it = hm.view[idx];
    if (!it) return;
    if (on) hm.checked[it.id] = true; else delete hm.checked[it.id];
    var row = $('hm-rows').querySelector('[data-idx="' + idx + '"].hm-row');
    if (row) {
      row.classList.toggle('is-checked', !!on);
      var box = row.querySelector('.hm-row-check');
      if (box) box.checked = !!on;
    }
  }

  // 체크박스: 클릭(+Shift 범위 선택)
  $('hm-rows').addEventListener('click', function (e) {
    var box = e.target.closest('.hm-row-check');
    if (box) {
      e.stopPropagation();
      var idx = Number(box.dataset.idx);
      var on = box.checked;
      if (e.shiftKey && hm.anchor >= 0 && hm.anchor !== idx) {
        var lo = Math.min(hm.anchor, idx), hi = Math.max(hm.anchor, idx);
        for (var i = lo; i <= hi; i++) hmSetChecked(i, on);
      } else {
        hmSetChecked(idx, on);
      }
      hm.anchor = idx;
      renderHmSelection();
      return;
    }
    var row = e.target.closest('.hm-row');
    if (!row) return;
    openHmDetail(row.dataset.id);
  });

  // Shift+클릭 시 브라우저 텍스트 선택 방지
  $('hm-rows').addEventListener('mousedown', function (e) {
    if (e.shiftKey) e.preventDefault();
  });

  $('hm-select-all').addEventListener('change', function () {
    var on = this.checked;
    hm.view.forEach(function (it) {
      if (on) hm.checked[it.id] = true; else delete hm.checked[it.id];
    });
    renderHistoryModal();
  });

  $('hm-search').addEventListener('input', function () {
    hm.anchor = -1;
    renderHistoryModal();
  });

  $('hm-delete-selected').addEventListener('click', function () {
    var ids = hmSelectedIds();
    if (!ids.length) return;
    if (!window.confirm(ids.length + '건을 영구 삭제합니다 (모든 기기 공통, 복구 불가). 계속할까요?')) return;
    fetchJSON('/history/delete', 20000, 'POST', { ids: ids })
      .then(function (r) {
        toast((r.deleted || 0) + '건을 삭제했습니다');
        hm.checked = {};
        hm.anchor = -1;
        return loadHistoryModal();
      })
      .then(function () { renderHistory(); })
      .catch(function (err) { toast('삭제에 실패했습니다: ' + (err.message || err)); });
  });

  $('hm-delete-all').addEventListener('click', function () {
    if (!hm.items.length) { toast('삭제할 기록이 없습니다'); return; }
    if (!window.confirm('서버에 저장된 모든 질의 기록 ' + hm.items.length +
      '건이 영구 삭제됩니다 (모든 기기 공통, 복구 불가). 계속할까요?')) return;
    fetchJSON('/history', 20000, 'DELETE')
      .then(function (r) {
        toast((r.deleted != null ? r.deleted : r.hidden || 0) + '건을 비웠습니다');
        hm.checked = {};
        hm.activeId = null;
        hm.detail = null;
        return loadHistoryModal();
      })
      .then(function () { renderHistory(); })
      .catch(function () { toast('비우기에 실패했습니다'); });
  });

  function openHmDetail(id) {
    hm.activeId = id;
    Array.prototype.forEach.call($('hm-rows').querySelectorAll('.hm-row'), function (r) {
      r.classList.toggle('is-active', r.dataset.id === id);
    });
    $('hm-empty').hidden = true;
    $('hm-detail-inner').hidden = false;
    $('hm-d-body').textContent = '불러오는 중…';
    fetchJSON('/history/' + id, 20000)
      .then(function (item) {
        hm.detail = item;
        $('hm-d-qid').textContent = item.question_id || '—';
        $('hm-d-time').textContent = [
          item.ts ? String(item.ts).slice(0, 19).replace('T', ' ') : '',
          item.seconds != null ? item.seconds + 's' : '',
        ].filter(Boolean).join(' · ');
        $('hm-d-question').textContent = item.question || '';
        renderHmDetailBody();
      })
      .catch(function (err) {
        $('hm-d-body').textContent = '상세를 불러오지 못했습니다: ' + (err.message || err);
      });
  }

  function renderHmDetailBody() {
    var d = hm.detail;
    if (!d) return;
    var body = $('hm-d-body');
    if (hm.dview === 'json') {
      body.innerHTML = '<pre>' + esc(JSON.stringify(d, null, 2)) + '</pre>';
    } else if (hm.dview === 'context') {
      body.innerHTML = '<pre>' + esc(d.retrieved_context || '(근거 없음)') + '</pre>';
    } else if (hm.dview === 'trace') {
      body.innerHTML = '<pre>' + esc(d.think_trace || '(기록 없음)') + '</pre>';
    } else {
      body.textContent = d.answer || '(답변 없음)';
    }
    body.scrollTop = 0;
  }

  $('hm-views').addEventListener('click', function (e) {
    var btn = e.target.closest('button[data-hv]');
    if (!btn) return;
    hm.dview = btn.dataset.hv;
    Array.prototype.forEach.call(this.querySelectorAll('button'), function (b) {
      b.classList.toggle('active', b === btn);
    });
    renderHmDetailBody();
  });

  $('hm-load').addEventListener('click', function () {
    if (!hm.detail) return;
    $('q-input').value = hm.detail.question || '';
    $('qid-input').value = hm.detail.question_id || 'Q-001';
    state.last = hm.detail;
    state.live = [];
    state.usage = null;
    state.finalAnswer = hm.detail.answer || '';
    state.view = 'answer';
    $('token-bar').hidden = true;
    renderResult(hm.detail, hm.detail.seconds != null ? String(hm.detail.seconds) : '—', true);
    $('result-body').scrollTop = 0;
    closeHistoryModal();
    toast('질의 입력에 불러왔습니다');
  });

  $('hm-copy').addEventListener('click', function () {
    if (!hm.detail) return;
    var text = hm.detail.answer || '';
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text)
        .then(function () { toast('답변을 복사했습니다'); })
        .catch(function () { toast('복사에 실패했습니다'); });
    } else {
      toast('이 브라우저에서는 복사를 지원하지 않습니다');
    }
  });

  $('hm-close').addEventListener('click', closeHistoryModal);
  $('history-modal').addEventListener('mousedown', function (e) {
    if (e.target === this) closeHistoryModal();
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && !$('history-modal').hidden) closeHistoryModal();
  });

  // ---------- 초기화 ----------

  renderHistory();
  refreshHealth();
  connectEvents();
  catchUp();
  setInterval(refreshHealth, 30000);
})();
