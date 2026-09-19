/* Presentation only. No recording, upload, deletion, or configuration writes. */
(function () {
  'use strict';
  const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const number = value => Number.isFinite(Number(value)) ? Number(value) : 0;
  const labels = {pending:'업로드 대기', uploading:'업로드 중', not_ready:'검증 보류', failed:'업로드 실패', file_missing:'파일 확인 필요', split_required:'분할 필요', split_parent_ready:'분할본 처리 대기', uploaded_pending_youtube_processing:'YouTube 처리 중', processing:'처리 중', done:'완료'};
  const statusLabel = value => labels[value] || value || '확인 중';
  function shouldPauseQueueRefresh(element, saving) {
    return !!saving || !!(element?.classList?.contains('queue-title-input') &&
      element.value !== (element.dataset.originalValue || ''));
  }
  function matchesBroadcast(row, query, filter) {
    const text = `${row.name || ''} ${row.live_title || ''} ${row.title || ''}`.toLocaleLowerCase();
    return text.includes(query.trim().toLocaleLowerCase()) &&
      (filter === 'all' || (filter === 'check' ? row.check_ok === false : row.check_ok !== false && row.is_live === true));
  }
  function alertsFor(status) {
    const alerts = [];
    const yt = status.youtube_summary || {};
    const safety = status.operations_safety || {};
    const unknown = (status.last_check_results || []).filter(r => r.check_ok === false).length;
    if (status.engine_state !== 'running') alerts.push({text:'녹화기 실행 상태 확인', page:'jobs', tone:'bad'});
    if (number(yt.failed)) alerts.push({text:`업로드 실패 ${number(yt.failed)}개 · 원본 확인`, target:'uploadSection', tone:'bad'});
    if (number(yt.not_ready)) alerts.push({text:`검증 보류 ${number(yt.not_ready)}개`, target:'uploadSection', tone:'warn'});
    if (status.upload_progress?.stalled) alerts.push({text:'업로드 진행 정체', target:'uploadSection', tone:'warn'});
    if (unknown) alerts.push({text:`방송 조회 미확인 ${unknown}명`, target:'broadcastSection', filter:'check', tone:'warn'});
    if (number(status.status_age_sec) > 180) alerts.push({text:'녹화기 상태 정보가 오래됨', page:'logs', tone:'warn'});
    if (safety.db_ok === false) alerts.push({text:'데이터베이스 연결 확인', target:'systemDetails', tone:'bad'});
    if (number(safety.duplicate_final_paths)) alerts.push({text:'중복 파일 경로 확인', target:'systemDetails', tone:'warn'});
    if (number(safety.stale_ts)) alerts.push({text:`장시간 정체된 임시 파일 ${number(safety.stale_ts)}개`, target:'storageDetails', tone:'warn'});
    const free = status.local_storage?.drive?.free_gb ?? status.temp_free_gb;
    if (free != null && Number.isFinite(Number(free)) && Number(free) < 500) alerts.push({text:`저장 여유 ${Number(free).toFixed(0)}GB`, target:'storageDetails', tone:Number(free) < 300 ? 'bad':'warn'});
    return alerts;
  }
  // Pure helpers are also exercised without a browser or live API.
  if (typeof module !== 'undefined') module.exports = {matchesBroadcast, alertsFor, statusLabel, escape, shouldPauseQueueRefresh};
  if (typeof document === 'undefined') return;

  const $ = selector => document.querySelector(selector);
  const opened = new Set();
  let snapshot = null;
  let filter = 'all';
  let toastTimer;
  const icons = {
    dashboard:'M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z',
    jobs:'M3 12h4l3-8 4 16 3-8h4',
    logs:'M12 3 2 21h20L12 3z M12 9v5 M12 17v.1',
    streamers:'M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2 M9 3a4 4 0 1 0 0 8 4 4 0 0 0 0-8 M17 4a4 4 0 0 1 0 7 M22 21v-2a4 4 0 0 0-3-3.8',
    thumbnails:'M3 3h18v18H3z M3 16l6-6 4 4 3-3 5 5 M16 7h.1',
    history:'M4 4v6h6 M4 10a9 9 0 1 1 0 5 M12 7v5l3 2',
    settings:'M4 6h16 M4 12h16 M4 18h16 M8 3v6 M16 9v6 M10 15v6',
  };
  document.querySelectorAll('.nav').forEach(button => {
    button.insertAdjacentHTML('afterbegin', `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="${icons[button.dataset.page] || ''}"/></svg>`);
    button.setAttribute('aria-current', button.classList.contains('active') ? 'page' : 'false');
    button.addEventListener('click', () => {
      document.querySelectorAll('.nav').forEach(b => b.setAttribute('aria-current', b === button ? 'page' : 'false'));
      $('.main').scrollTop = 0;
      if (matchMedia('(max-width: 850px)').matches) window.scrollTo({top:0, behavior:'instant'});
    });
  });

  function wrapDetails(section, id, title, hint) {
    if (!section) return;
    const details = document.createElement('details');
    details.id = id;
    details.className = 'support-details';
    details.dataset.detailKey = id;
    details.innerHTML = `<summary><span><b>${title}</b><small>${hint}</small></span><span class="detail-chevron" aria-hidden="true">⌄</span></summary>`;
    section.before(details);
    details.append(section);
  }
  const dashboard = $('#dashboard');
  dashboard.before($('#connectionNotice'));
  const broadcast = $('#broadcastSection');
  $('.overview-section').after(broadcast);
  broadcast.after($('#uploadSection'));
  wrapDetails($('.storage-section'), 'storageDetails', '저장소 살펴보기', '용량 · 파일 위치 · 임시 파일');
  wrapDetails($('.safety-section'), 'systemDetails', '시스템 진단', '프로세스 · 데이터베이스 · 안전 점검');
  dashboard.append($('#storageDetails'), $('#systemDetails'), $('.latest-log'));
  // Keep the legacy network reading, but do not feature an old speed test as live data.
  $('.safety-section').append($('.metric-card.net'));
  const flow = $('.daily-storage-flow');
  wrapDetails(flow, 'dailyStorageDetails', '오늘의 저장공간 변화', '생성 · 전송 · 정리된 용량 보기');
  $('#storageDetails').querySelector('.storage-section').prepend($('#dailyStorageDetails'));
  const workerCard = $('.upload-active-card');
  $('.hero-right').append(workerCard);
  $('.hero-left').insertAdjacentHTML('afterbegin', '<div class="hero-kicker">RECORD. KEEP. REMEMBER.</div>');
  $('#uploadSection .section-head h3').textContent = '업로드 워크스페이스';
  $('#uploadSection .section-head p').textContent = '전송과 YouTube 처리 완료를 구분해 확인하세요.';
  $('.brand-icon').innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 5v14l12-7z" fill="currentColor"/></svg>';

  document.addEventListener('toggle', event => {
    const key = event.target.dataset?.detailKey;
    if (key) event.target.open ? opened.add(key) : opened.delete(key);
  }, true);
  function restoreDetails(root) {
    root?.querySelectorAll('details[data-detail-key]').forEach(d => {d.open = opened.has(d.dataset.detailKey);});
  }
  function rememberFocus(root) {
    const focused = document.activeElement;
    if (!root?.contains(focused)) return () => {};
    const id = focused.id;
    const detailKey = focused.closest('details')?.dataset.detailKey;
    const rowId = focused.closest('.queue-row')?.dataset.recordingId;
    const selection = focused.tagName === 'INPUT' ? [focused.selectionStart, focused.selectionEnd] : null;
    const isCopy = focused.matches('[data-copy-text]');
    const isSave = focused.matches('.queue-title-save');
    return () => {
      let replacement = id ? document.getElementById(id) : null;
      if (!replacement && detailKey) {
        const detail = [...root.querySelectorAll('details')].find(d => d.dataset.detailKey === detailKey);
        replacement = detail?.querySelector(isCopy ? '[data-copy-text]' : 'summary');
      }
      if (!replacement && rowId && isSave) {
        replacement = [...root.querySelectorAll('.queue-row')].find(r => r.dataset.recordingId === rowId)?.querySelector('.queue-title-save');
      }
      replacement?.focus({preventScroll:true});
      if (replacement && selection) replacement.setSelectionRange(...selection);
    };
  }
  function toast(message) {
    const box = $('#viewToast');
    box.textContent = message;
    box.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { box.hidden = true; }, 2600);
  }
  function jump(target, page) {
    if (page) { document.querySelector(`.nav[data-page="${page}"]`)?.click(); return; }
    const element = document.getElementById(target);
    if (!element) return;
    if (element.tagName === 'DETAILS') element.open = true;
    element.scrollIntoView({behavior:matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant':'smooth', block:'start'});
  }
  document.addEventListener('click', async event => {
    if (!event.target.closest('.control-menu')) $('.control-menu').open = false;
    const copy = event.target.closest('[data-copy-text]');
    if (copy) {
      try { await navigator.clipboard.writeText(copy.dataset.copyText); toast('파일 경로를 복사했어요.'); }
      catch (_) { toast('복사하지 못했어요. 펼쳐진 경로를 직접 선택해 주세요.'); }
    }
    const jumpButton = event.target.closest('[data-jump], [data-page-jump]');
    if (jumpButton) {
      if (jumpButton.dataset.broadcastFilter) {
        filter = jumpButton.dataset.broadcastFilter;
        $('#broadcastSearch').value = '';
        renderStreams();
      }
      jump(jumpButton.dataset.jump, jumpButton.dataset.pageJump);
    }
  });
  $('#broadcastSearch').addEventListener('input', renderStreams);
  $('#broadcastFilters').addEventListener('click', event => {
    const button = event.target.closest('[data-filter]');
    if (!button) return;
    filter = button.dataset.filter;
    renderStreams();
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && $('.control-menu').open) {
      $('.control-menu').open = false;
      $('.control-menu summary').focus();
    }
    if (event.key !== '/' || event.ctrlKey || event.metaKey || event.altKey ||
        event.target.closest('input, textarea, select, [contenteditable="true"]') || !dashboard.classList.contains('active')) return;
    event.preventDefault(); $('#broadcastSearch').focus();
  });

  function renderStreams() {
    if (!snapshot) return;
    const query = $('#broadcastSearch').value;
    const active = snapshot.active_recordings || [];
    const checks = snapshot.last_check_results || [];
    const byName = new Map(checks.map(row => [row.name, row]));
    $('#broadcastFilters').querySelectorAll('button').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.filter === filter)));
    const visible = active.filter(r => matchesBroadcast({...byName.get(r.name), ...r, check_ok:r.status_check_failed ? false : byName.get(r.name)?.check_ok}, query, filter));
    $('#recordingBadge').textContent = query || filter !== 'all' ? `${visible.length} / ${active.length}` : String(active.length);
    // Restore focus instead of leaving an outdated recording card on screen.
    const restoreFocus = rememberFocus($('#activeRecordings'));
    {
      $('#activeRecordings').className = 'capture-grid';
      $('#activeRecordings').innerHTML = visible.length ? visible.map(r => {
        const unknown = r.status_check_failed || byName.get(r.name)?.check_ok === false;
        const finalizing = r.phase === 'finalizing' && !unknown;
        const state = unknown ? '조회 미확인 · 녹화 유지' : finalizing ? '파일 마감 중' : '녹화 중';
        const hue = [...(r.name || '?')].reduce((n, char) => n + char.codePointAt(0), 0) % 360;
        const path = r.ts_path || '';
        return `<article class="capture-card ${unknown ? 'needs-check':''}">
          <div class="capture-head"><span class="stream-avatar" style="--avatar-hue:${hue}">${escape((r.name || '?').slice(0,1))}</span><div class="capture-name"><b>${escape(r.name)}</b><span>${escape(r.quality || 'best')} 화질</span></div><span class="capture-state ${finalizing || unknown ? 'is-warn':''}"><i></i>${state}</span></div>
          <p class="capture-title" title="${escape(r.live_title)}">${escape(r.live_title || '방송 제목을 기다리고 있어요.')}</p>
          <div class="capture-bottom"><span>${finalizing ? '후처리 대기':'녹화 시간'}</span><strong>${escape(typeof elapsedText === 'function' ? elapsedText(r.started_at) : r.started_at || '-')}</strong></div>
          <details class="file-details" data-detail-key="capture:${escape(r.name)}"><summary>파일 정보</summary><div><span>시작 ${escape(r.started_at || '-')}</span><code>${escape(path || '경로 확인 중')}</code>${path ? `<button type="button" class="text-button" data-copy-text="${escape(path)}">경로 복사</button>`:''}</div></details>
        </article>`;
      }).join('') : `<div class="empty"><b>${query || filter !== 'all' ? '조건에 맞는 녹화가 없어요.':'지금은 녹화 중인 방송이 없어요.'}</b><p>${query || filter !== 'all' ? '검색어 또는 상태 필터를 바꿔 보세요.':'방송이 시작되면 이곳에서 확인할 수 있어요.'}</p></div>`;
      restoreDetails($('#activeRecordings'));
      restoreFocus();
    }
    const order = r => r.check_ok === false ? 0 : r.is_live ? 1 : 2;
    const lives = checks.map(r => ({...r, live_title:active.find(a => a.name === r.name)?.live_title || r.live_title})).filter(r => matchesBroadcast(r, query, filter)).sort((a,b) => order(a)-order(b));
    $('#liveList').innerHTML = lives.length ? lives.map(r => {
      const unknown = r.check_ok === false;
      return `<div class="broadcast-row"><span class="broadcast-dot ${unknown ? 'unknown':r.is_live ? 'live':'off'}"></span><b>${escape(r.name)}</b><time title="${escape(r.checked_at)}">${escape(String(r.checked_at || '').slice(11,16) || '-')}</time><span class="broadcast-label ${unknown ? 'unknown':r.is_live ? 'live':'off'}">${unknown ? '미확인':r.is_live ? '방송 중':'오프라인'}</span></div>`;
    }).join('') : '<div class="empty">조건에 맞는 방송이 없어요.</div>';
  }

  window.RecorderUI = {
    statusLabel,
    shouldPauseQueueRefresh,
    rememberFocus,
    onStatus(status) {
      snapshot = status;
      $('#connectionNotice').hidden = true;
      const alerts = alertsFor(status);
      $('#attentionCenter').hidden = !alerts.length;
      $('#attentionSummary').textContent = `${alerts.length}개 항목 · 눌러서 자세히 보기`;
      $('#attentionItems').innerHTML = alerts.map(a => `<button type="button" class="attention-link ${a.tone}" ${a.target ? `data-jump="${a.target}"`:`data-page-jump="${a.page}"`} ${a.filter ? `data-broadcast-filter="${a.filter}"`:''}>${escape(a.text)} <span aria-hidden="true">↗</span></button>`).join('');
      renderStreams();
    },
    onQueue() { restoreDetails($('#uploadQueueList')); },
    connectionError() {
      $('#connectionNotice').hidden = false;
      $('#connectionNotice').textContent = '대시보드 연결이 끊겼어요. 아래는 마지막으로 받은 정보입니다. 자동으로 다시 연결할게요.';
    },
    toast,
  };
})();
