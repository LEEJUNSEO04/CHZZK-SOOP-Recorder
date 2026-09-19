const pages = document.querySelectorAll(".page");
const navs = document.querySelectorAll(".nav");
const pageTitle = document.querySelector("#pageTitle");
const updatedAt = document.querySelector("#updatedAt");

const titles = {
  dashboard: "대시보드",
  streamers: "스트리머 관리",
  thumbnails: "썸네일 관리",
  jobs: "작업 상태",
  history: "녹화/업로드 기록",
  logs: "오류/로그",
  settings: "설정/경로",
};

let lastStatus = null;
let lastDbLive = null;
let uploadQueuePage = 1;
const uploadQueuePageSize = 25;
let thumbnailFilter = "all";
let thumbnailItems = [];
const queueTitleDrafts = new Map();
const queueSavesInFlight = new Set();
let refreshInFlight = false;
let uploadQueueRequest = 0;
document.addEventListener('input', (event) => {
  if (!event.target.classList?.contains('queue-title-input')) return;
  const id = Number(event.target.closest('.queue-row')?.dataset.recordingId);
  const dirty = event.target.value !== event.target.dataset.originalValue;
  if (dirty) queueTitleDrafts.set(id, event.target.value);
  else queueTitleDrafts.delete(id);
  event.target.classList.toggle('dirty', dirty);
  const message = document.querySelector(`#queueTitleMessage${id}`);
  if (message) { message.className = dirty ? 'queue-title-message unsaved' : 'queue-title-message'; message.textContent = dirty ? '저장하지 않은 제목 · 저장 버튼을 눌러 주세요. Esc로 되돌리기' : '저장된 제목과 같아요.'; }
});
document.addEventListener('keydown', event => {
  if (event.key !== 'Escape' || !event.target.classList?.contains('queue-title-input')) return;
  event.target.value = event.target.dataset.originalValue || '';
  event.target.dispatchEvent(new Event('input', {bubbles:true}));
});
window.addEventListener('beforeunload', event => {
  if (!queueTitleDrafts.size) return;
  event.preventDefault(); event.returnValue = '';
});

navs.forEach((btn) => {
  btn.addEventListener("click", () => {
    const page = btn.dataset.page;
    navs.forEach((n) => n.classList.remove("active"));
    btn.classList.add("active");
    pages.forEach((p) => p.classList.toggle("active", p.id === page));
    pageTitle.textContent = titles[page] || "대시보드";
    refresh();
  });
});

async function api(url, options = {}) {
  const readOnly = !options.method || options.method.toUpperCase() === 'GET';
  const res = await fetch(url, { ...options, signal: options.signal || (readOnly ? AbortSignal.timeout(20000) : undefined) });
  if (!res.ok) throw new Error(await res.text());
  const ct = res.headers.get("content-type") || "";
  return ct.includes("application/json") ? res.json() : res.text();
}

function esc(text) {
  return String(text ?? "").replace(/[&<>"']/g, (m) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#039;",
  }[m]));
}

function asNumber(v, fallback = 0) {
  const n = Number(v);
  return Number.isFinite(n) ? n : fallback;
}

function fmtGb(v, digit = 1) {
  const n = Number(v);
  return Number.isFinite(n) ? n.toFixed(digit) : "-";
}

function parseLocalDateTime(text) {
  if (!text) return null;
  const m = String(text).match(/^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})/);
  if (!m) return null;
  return new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]), Number(m[4]), Number(m[5]), Number(m[6]));
}

function formatDuration(ms) {
  if (!Number.isFinite(ms) || ms < 0) return "-";
  const total = Math.floor(ms / 1000);
  const d = Math.floor(total / 86400);
  const h = Math.floor((total % 86400) / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (d > 0) return `${d}일 ${h}시간 ${m}분`;
  if (h > 0) return `${h}시간 ${m}분`;
  if (m > 0) return `${m}분 ${s}초`;
  return `${s}초`;
}

function elapsedText(startedAt) {
  const dt = parseLocalDateTime(startedAt);
  if (!dt) return "-";
  return formatDuration(Date.now() - dt.getTime());
}

function setText(id, value) {
  const el = document.querySelector(id);
  if (el) el.textContent = value;
}

function setClass(id, className) {
  const el = document.querySelector(id);
  if (el) el.className = className;
}

function levelByFreeGb(free) {
  if (!Number.isFinite(free)) return "gray";
  if (free < 300) return "red";
  if (free < 500) return "yellow";
  return "green";
}

function setBar(id, valueGb, maxGb) {
  const el = document.querySelector(id);
  if (!el) return;
  const pct = Math.max(0, Math.min(100, (asNumber(valueGb) / Math.max(1, maxGb)) * 100));
  el.style.width = `${pct}%`;
}

function updateRunning(status) {
  const running = status.engine_state === "running";
  const uploader = !!status.uploader_running;
  const uploaderStalled = !!status.upload_progress?.stalled;
  const age = status.status_age_sec;

  setText("#sideStateText", running ? "녹화기 실행 중" : "녹화기 정지");
  setText("#sideUpdatedAt", age == null ? "-" : `${age}초 전 갱신`);
  setClass("#sideStateDot", "pulse " + (running ? "green" : "red"));

  const banner = document.querySelector("#engineBanner");
  if (banner) banner.className = "hero " + (running ? "running" : "stopped");

  setText("#engineTitle", running ? "녹화기 정상 실행 중" : "녹화기가 꺼져 있음");
  setText("#engineDesc", running
    ? "방송 감시 루프가 돌아가고 있습니다. 녹화/후처리/병합 상태를 아래에서 확인하세요."
    : "녹화기 시작 버튼을 누르면 방송 감시가 시작됩니다.");
  setText("#engineAge", age == null ? "기록 없음" : `${age}초 전`);
  setText("#uploaderState", uploaderStalled ? "정체 확인 중" : uploader ? "실행 중" : "정지");
  setClass("#enginePill", "state-pill " + (running ? "green" : "red"));
  setText("#enginePill", running ? "RUNNING" : "STOPPED");

  document.querySelector("#startBtn")?.classList.toggle("disabled", running);
  document.querySelector("#ytStartBtn")?.classList.toggle("disabled", uploader);
  if (document.querySelector('#startBtn')) document.querySelector('#startBtn').disabled = running;
  if (document.querySelector('#ytStartBtn')) document.querySelector('#ytStartBtn').disabled = uploader;
}

function updateHealth(status) {
  const yt = status.youtube_summary || {};
  const running = status.engine_state === "running";
  const age = status.status_age_sec;
  const free = Number(status.temp_free_gb);
  const failed = asNumber(yt.failed);
  const pending = asNumber(yt.pending);
  const queueWaiting = yt.queue_waiting == null
    ? pending + asNumber(yt.not_ready) + failed
    : asNumber(yt.queue_waiting);
  const uploaderStalled = !!status.upload_progress?.stalled;

  let level = "green";
  if (!running || failed > 0) level = "red";
  else if (uploaderStalled || (age != null && age > 180) || queueWaiting >= 20 || levelByFreeGb(free) === "yellow") level = "yellow";

  setClass("#overallHealthPill", "health-pill " + level);
  setText("#overallHealthPill", level === "green" ? "정상" : level === "yellow" ? "주의" : "확인 필요");

  let ulevel = "green";
  if (failed > 0) ulevel = "red";
  else if (uploaderStalled || queueWaiting >= 20) ulevel = "yellow";
  setClass("#uploadHealthPill", "health-pill " + ulevel);
  setText("#uploadHealthPill", failed > 0 ? "실패 확인" : uploaderStalled ? "업로드 정체" : queueWaiting >= 20 ? "대기 많음" : "정상");
}

function updateStorage(status) {
  const local = status.local_storage || {};
  const root = local.root || status.paths?.drive_dir || "-";
  const temp = asNumber(local.temp?.size_gb);
  const merged = asNumber(local.merged?.size_gb);
  const failed = asNumber(local.failed?.size_gb);
  const drive = asNumber(local.drive?.size_gb ?? local.local_merged?.size_gb);
  const free = Number(status.temp_free_gb ?? local.drive?.free_gb);

  setText("#storageTemp", fmtGb(temp, 2));
  setText("#storageMerged", fmtGb(merged, 2));
  setText("#storageFailed", fmtGb(failed, 2));
  setText("#storageDrive", fmtGb(drive, 2));
  setText("#storageRootPath", root);
  setText("#sideFreeGb", Number.isFinite(free) ? `${free.toFixed(1)} GB` : "- GB");

  const driveInfo = local.drive || {};
  const totalGb = Number(driveInfo.total_gb);
  const usedGb = Number(driveInfo.used_gb);
  const usedPct = Number(driveInfo.used_percent);
  const driveName = driveInfo.drive || (root.match(/^[A-Z]:/i)?.[0] || "-");

  setText("#diskDriveName", driveName);
  setText("#diskTotalGb", Number.isFinite(totalGb) ? `${totalGb.toFixed(1)} GB` : "- GB");
  setText("#diskUsedGb", Number.isFinite(usedGb) ? `${usedGb.toFixed(1)} GB` : "- GB");
  setText("#diskFreeGb", Number.isFinite(free) ? `${free.toFixed(1)} GB` : "- GB");

  const diskBar = document.querySelector("#diskUsedBar");
  if (diskBar) diskBar.style.width = `${Math.max(0, Math.min(100, Number.isFinite(usedPct) ? usedPct : 0))}%`;

  const diskLevel = levelByFreeGb(free);
  setClass("#storagePill", "health-pill " + diskLevel);
  setText("#storagePill", diskLevel === "green" ? "여유" : diskLevel === "yellow" ? "주의" : diskLevel === "red" ? "위험" : "확인 불가");

  setBar("#barTemp", temp, 200);
  setBar("#barMerged", merged, 300);
  setBar("#barFailed", failed, 100);
  setBar("#barDrive", drive, 1500);

  const files = local.recording_files || [];
  const list = document.querySelector("#recordingFileList");
  if (!list) return;

  if (!files.length) {
    list.innerHTML = `<div class="empty">현재 temp에 녹화 파일이 없습니다.</div>`;
    return;
  }

  list.innerHTML = files.slice(0, 8).map((f) => `
    <div class="file-row">
      <div>
        <b>${esc(f.name)}</b>
        <small>${esc(f.path)}</small>
      </div>
      <span>${fmtGb(f.size_gb, 2)}GB</span>
    </div>
  `).join("");
}

function updateSafety(status) {
  const proc = status.process_summary || {};
  const safety = status.operations_safety || {};
  const yt = status.youtube_summary || {};
  const duplicates = asNumber(safety.duplicate_final_paths);
  const staleTs = asNumber(safety.stale_ts);
  const protectedItems = asNumber(safety.protected_split_items);
  const dbOk = !!safety.db_ok;

  setText("#safetyProcesses", `recorder ${proc.recorder ?? "-"} / streamlink ${proc.streamlink ?? "-"}`);
  setText("#safetyProcessSource", proc.source === "windows" ? "실제 Windows 프로세스 기준" : "녹화기 heartbeat 기준");
  setText("#safetyDb", dbOk ? "정상" : "연결 확인 필요");
  setText("#safetyDbDetail", safety.db_latency_ms == null ? "응답 시간 없음" : `${safety.db_latency_ms}ms 전후`);
  setText("#safetyUpload", `대기 ${yt.pending ?? 0} / 업로드 ${yt.uploading ?? 0} / 처리 확인 ${safety.processing ?? 0}`);
  setText("#safetyWarnings", `중복 ${duplicates} / 정체 TS ${staleTs} / 삭제 보호 ${protectedItems}`);

  const level = !dbOk || duplicates > 0 ? "red" : staleTs > 0 ? "yellow" : "green";
  setClass("#safetyHealthPill", `health-pill ${level}`);
  setText("#safetyHealthPill", level === "green" ? "안전" : level === "yellow" ? "검토 필요" : "즉시 확인");
}

function shortClock(text) {
  const dt = parseLocalDateTime(text);
  if (!dt) return "--:--:--";
  return dt.toLocaleTimeString("ko-KR", { hour12: false });
}

function updateDbLive(data) {
  lastDbLive = data;
  const panel = document.querySelector(".db-live-panel");
  panel?.classList.toggle("bad", !data?.ok);

  if (!data?.ok) {
    setText("#dbLiveSummary", "DB 연결 확인 필요");
    setText("#dbLiveUpdated", "조회 실패");
    const events = document.querySelector("#dbLiveEvents");
    if (events) events.innerHTML = `<span class="db-live-empty">DB 상태를 읽지 못했습니다.</span>`;
    return;
  }

  const c = data.counts || {};
  setText(
    "#dbLiveSummary",
    `녹화 ${asNumber(c.recording)} · 병합 ${asNumber(c.merging)} · 대기 ${asNumber(c.pending)} · 업로드 ${asNumber(c.uploading)} · 처리 확인 ${asNumber(c.processing)} · 오늘 완료 ${asNumber(c.done_today)}`,
  );
  setText("#dbLiveUpdated", `${shortClock(data.checked_at)} · ${asNumber(data.latency_ms)}ms`);
  setText("#safetyDbDetail", `실시간 조회 ${asNumber(data.latency_ms)}ms · ${shortClock(data.checked_at)}`);

  const events = document.querySelector("#dbLiveEvents");
  if (!events) return;
  const rows = Array.isArray(data.recent) ? data.recent : [];
  events.innerHTML = rows.length
    ? rows.map((row) => `
        <span class="db-live-event">
          <time>${esc(shortClock(row.at))}</time>
          <b>${esc(row.streamer)}</b>
          <em>${esc(row.event)}</em>
        </span>
      `).join("")
    : `<span class="db-live-empty">표시할 DB 변경 기록이 없습니다.</span>`;
}

async function refreshDbLive() {
  try {
    updateDbLive(await api("/api/db/activity?limit=5"));
  } catch (e) {
    updateDbLive({ ok: false });
  }
}

function updateDashboard(status) {
  updateRunning(status);
  updateHealth(status);
  updateStorage(status);
  updateSafety(status);

  updatedAt.textContent = status.updated_at
    ? `마지막 업데이트: ${status.updated_at}`
    : "아직 상태 파일이 없습니다.";

  setText("#latestLogLine", status.latest_log_line || "아직 로그가 없습니다.");

  const yt = status.youtube_summary || {};
  const daily = status.youtube_daily_stats || {};
  const uploadProgress = status.upload_progress || {};
  const uploadProgressItems = Array.isArray(uploadProgress.items) ? uploadProgress.items : [];
  const uploaderLog = status.uploader_log || {};
  const uploadPct = Number(uploadProgress.progress_percent);
  const hasUploadProgress = uploadProgress.progress_percent != null && Number.isFinite(uploadPct);
  const queueWaiting = yt.queue_waiting == null
    ? asNumber(yt.pending) + asNumber(yt.not_ready) + asNumber(yt.failed)
    : asNumber(yt.queue_waiting);
  setText("#ytPending", queueWaiting);
  setText("#ytPendingDetail", `대기 ${asNumber(yt.pending)} · 보류 ${asNumber(yt.not_ready)} · 실패 ${asNumber(yt.failed)}`);
  setText("#ytUploading", yt.uploading ?? 0);
  const transferred = Number(uploadProgress.transferred_gb);
  const total = Number(uploadProgress.total_gb);
  const workerHost = document.querySelector("#ytUploadWorkers");
  if (workerHost) {
    const visibleWorkers = uploadProgressItems.slice(0, 2);
    workerHost.innerHTML = visibleWorkers.length
      ? visibleWorkers.map((item) => {
          const pctRaw = Number(item.progress_percent);
          const pct = Number.isFinite(pctRaw) ? Math.max(0, Math.min(100, pctRaw)) : 0;
          const sent = Number(item.transferred_gb);
          const totalItem = Number(item.total_gb);
          const workerName = item.worker_id ? `W${item.worker_id}` : "업로드";
          const stalled = !!item.stalled;
          return `
            <div class="upload-worker-pane ${stalled ? "stalled" : ""}">
              <div class="upload-worker-title">
                <b>${esc(workerName)}</b>
                <strong>${Math.round(pct)}%</strong>
              </div>
              <div class="upload-worker-name">${esc(item.streamer_name || `영상 #${item.recording_id || "-"}`)}</div>
              <div class="metric-progress"><i style="width:${pct}%"></i></div>
              <small>${Number.isFinite(sent) ? sent.toFixed(2) : "-"} / ${Number.isFinite(totalItem) ? totalItem.toFixed(2) : "-"}GB${stalled ? " · 정체 확인 중" : ""}</small>
            </div>`;
        }).join("")
      : `<div class="upload-worker-empty">${asNumber(yt.uploading) > 0 ? "진행률 연결 대기 중" : "진행 중인 업로드 없음"}</div>`;
  }
  let uploadDetail = uploadProgress.stalled
    ? `정체 감지 · 최근 ${Math.max(1, Math.round(asNumber(uploadProgress.age_sec) / 60))}분간 진행 상태 갱신 없음 · 자동 복구 감시 중`
    : "";
  if (uploaderLog.last_error && uploaderLog.last_error_age_sec != null
      && asNumber(uploaderLog.last_error_age_sec) <= 3600) {
    uploadDetail += `${uploadDetail ? " · " : ""}최근 오류: ${uploaderLog.last_error}`;
  }
  setText("#ytUploadProgressDetail", uploadDetail);
  setText("#ytDone", yt.done ?? 0);
  const processingPending = Math.max(0, asNumber(yt.processing_pending));
  setText(
    "#ytProcessingPending",
    processingPending > 0 ? `전송 완료 · YouTube 처리 확인 대기 ${processingPending}개` : "YouTube 처리 확인 완료"
  );
  setText("#ytTodayDone", daily.today_count ?? 0);
  const todayGb = Number(daily.today_gb);
  const avg7Count = Number(daily.avg_7d_count);
  const avg7Gb = Number(daily.avg_7d_gb);
  const dailyText = `오늘 ${Number.isFinite(todayGb) ? todayGb.toFixed(1) : "0.0"}GB · 최근 7일 평균 ${Number.isFinite(avg7Count) ? avg7Count.toFixed(1) : "0.0"}개 / ${Number.isFinite(avg7Gb) ? avg7Gb.toFixed(1) : "0.0"}GB`;
  setText("#ytDailyAverage", dailyText);
  setText("#ytFailed", yt.failed ?? 0);

  const addedGb = Math.max(0, Number(daily.today_added_gb) || 0);
  const uploadedGb = Math.max(0, Number(daily.today_gb) || 0);
  const deletedGb = Math.max(0, Number(daily.today_deleted_gb) || 0);
  const netGb = Number.isFinite(Number(daily.today_net_gb))
    ? Number(daily.today_net_gb)
    : addedGb - deletedGb;
  const flowMax = Math.max(addedGb, uploadedGb, deletedGb, 1);

  setText("#dailyAddedGb", `+${addedGb.toFixed(1)}GB`);
  setText("#dailyUploadedGb", `${uploadedGb.toFixed(1)}GB`);
  setText("#dailyDeletedGb", `−${deletedGb.toFixed(1)}GB`);
  setText("#dailyStorageNet", `${netGb >= 0 ? "+" : "−"}${Math.abs(netGb).toFixed(1)}GB`);
  setText(
    "#dailyStorageNetDetail",
    netGb > 0.05
      ? `오늘은 파일이 ${netGb.toFixed(1)}GB 더 쌓였어요.`
      : netGb < -0.05
        ? `오늘은 저장공간이 ${Math.abs(netGb).toFixed(1)}GB 줄었어요.`
        : "오늘은 추가된 양과 삭제된 양이 거의 같아요."
  );
  setClass("#dailyStorageNet", `daily-net ${netGb > 0.05 ? "up" : netGb < -0.05 ? "down" : "even"}`);

  const addedBar = document.querySelector("#dailyAddedBar");
  const uploadedBar = document.querySelector("#dailyUploadedBar");
  const deletedBar = document.querySelector("#dailyDeletedBar");
  if (addedBar) addedBar.style.width = `${(addedGb / flowMax) * 100}%`;
  if (uploadedBar) uploadedBar.style.width = `${(uploadedGb / flowMax) * 100}%`;
  if (deletedBar) deletedBar.style.width = `${(deletedGb / flowMax) * 100}%`;

  const avgAddedGb = Math.max(0, Number(daily.avg_7d_added_gb) || 0);
  const avgUploadedGb = Math.max(0, Number(daily.avg_7d_gb) || 0);
  const avgDeletedGb = Math.max(0, Number(daily.avg_7d_deleted_gb) || 0);
  setText(
    "#dailyStorageAverage",
    `최근 7일 하루 평균 · 추가 ${avgAddedGb.toFixed(1)}GB / 업로드 ${avgUploadedGb.toFixed(1)}GB / 삭제 ${avgDeletedGb.toFixed(1)}GB`
  );

  const results = status.last_check_results || [];
  const active = status.active_recordings || [];
  const checksByName = new Map(results.map((r) => [r.name, r]));
  const activeCheckFailed = (r) => r.status_check_failed || checksByName.get(r.name)?.check_ok === false;
  const recordingActive = active.filter((r) => r.phase !== "finalizing" || activeCheckFailed(r));
  const finalizingActive = active.filter((r) => r.phase === "finalizing" && !activeCheckFailed(r));
  const live = results.filter((r) => r.check_ok !== false && r.is_live);

  setText("#watchCount", status.streamers_count ?? results.length ?? 0);
  setText("#liveCount", live.length);
  setText("#recordingCount", recordingActive.length);
  setText("#recordingBadge", recordingActive.length);
  setText("#freeGb", status.temp_free_gb ?? "-");

  if (!window.RecorderUI) {
  const activeHtml = !active.length
    ? `<div class="empty">현재 녹화 중인 방송이 없습니다.</div>`
    : active.map((r) => {
      const checkFailed = activeCheckFailed(r);
      const finalizing = r.phase === "finalizing" && !checkFailed;
      const phaseLabel = checkFailed
        ? "상태 조회 일시 실패 · 녹화 유지 중"
        : finalizing ? "종료 감지 · 마감 처리 중" : `녹화 중 ${esc(elapsedText(r.started_at))}`;
      return `
      <div class="job-row">
        <div>
          <b>${esc(r.name)}</b>
          <small>
            ${phaseLabel} · 시작 ${esc(r.started_at)} · 화질 ${esc(r.quality)}
            ${r.live_title ? `<br>제목: ${esc(r.live_title)}` : ""}
            <br>${esc(r.ts_path)}
          </small>
        </div>
        <span class="pill ${finalizing ? "post" : "rec"}">${finalizing ? "POST" : "REC"}</span>
      </div>
    `}).join("");

  const activeBox = document.querySelector("#activeRecordings");
  if (activeBox) {
    activeBox.className = active.length ? "status-list" : "";
    activeBox.innerHTML = activeHtml;
  }

  const liveList = document.querySelector("#liveList");
  if (liveList) {
    liveList.innerHTML = !results.length
      ? `<div class="empty">아직 방송 상태 확인 기록이 없습니다.</div>`
      : results.map((r) => {
        const checkFailed = r.check_ok === false;
        const badgeClass = checkFailed ? "post" : r.is_live ? "live" : "off";
        const badgeText = checkFailed ? "CHECK" : r.is_live ? "LIVE" : "OFF";
        return `
        <div class="status-item">
          <div>
            <b>${esc(r.name)}</b>
            <small>${esc(r.quality)} · ${esc(r.checked_at)} · ${esc(r.elapsed_sec)}s</small>
          </div>
          <span class="pill ${badgeClass}">${badgeText}</span>
        </div>
      `;
      }).join("");
  }

  }
  const proc = status.process_summary || {};
  const safety = status.operations_safety || {};
  const recorderRunning = status.engine_state === "running";
  const uploaderRunning = !!status.uploader_running;
  const pendingCount = yt.queue_waiting == null
    ? asNumber(yt.pending) + asNumber(yt.not_ready) + asNumber(yt.failed)
    : asNumber(yt.queue_waiting);
  const processingCount = asNumber(safety.processing);

  const jobSummary = document.querySelector("#jobSummary");
  if (jobSummary) {
    jobSummary.innerHTML = `
      <div class="job-summary-card ${recorderRunning ? "ok" : "bad"}">
        <span>녹화기</span>
        <strong>${recorderRunning ? "정상 실행 중" : "정지"}</strong>
        <small>프로세스 ${proc.recorder ?? "-"}개</small>
      </div>
      <div class="job-summary-card ${recordingActive.length ? "active" : "idle"}">
        <span>현재 녹화</span>
        <strong>${recordingActive.length}개</strong>
        <small>${recordingActive.length ? `방송 저장 중 · 마감 처리 ${finalizingActive.length}개` : finalizingActive.length ? `녹화 종료 · 마감 처리 ${finalizingActive.length}개` : "현재 방송 중인 대상이 없어요."}</small>
      </div>
      <div class="job-summary-card ${uploaderRunning ? "active" : "idle"}">
        <span>YouTube 업로더</span>
        <strong>${uploaderRunning ? "실행 중" : "정지"}</strong>
        <small>${asNumber(yt.uploading)}개 업로드 중</small>
      </div>
      <div class="job-summary-card ${pendingCount >= 20 ? "warn" : "ok"}">
        <span>남은 대기열</span>
        <strong>${pendingCount}개</strong>
        <small>처리 확인 ${processingCount}개</small>
      </div>
    `;
  }

  const currentJobs = [];
  if (recorderRunning) {
    currentJobs.push(`
      <div class="job-row">
        <div>
          <b>방송 상태 감시 중</b>
          <small>등록된 ${status.streamers_count ?? results.length ?? 0}명을 주기적으로 확인하고 새 방송을 자동 녹화합니다.</small>
        </div>
        <span class="pill live">WATCH</span>
      </div>
    `);
  }
  for (const r of active) {
    const checkFailed = activeCheckFailed(r);
    const finalizing = r.phase === "finalizing" && !checkFailed;
    const activityText = checkFailed
      ? "상태 조회가 잠시 실패했지만 현재 녹화는 안전하게 유지 중"
      : finalizing ? "방송 종료를 감지해 파일을 안전하게 닫는 중" : `${esc(elapsedText(r.started_at))} 경과 · ${esc(r.quality)}`;
    currentJobs.push(`
      <div class="job-row">
        <div>
          <b>${esc(r.name)} ${finalizing ? "마감 처리 중" : "녹화 중"}</b>
          <small>
            ${activityText}
            ${r.live_title ? `<br>${esc(r.live_title)}` : ""}
          </small>
        </div>
        <span class="pill ${finalizing ? "post" : "rec"}">${finalizing ? "POST" : "REC"}</span>
      </div>
    `);
  }

  if (asNumber(yt.uploading) > 0) {
    const pctText = uploadProgress.stalled ? "진행 정체 감지" : hasUploadProgress ? `${uploadPct}%` : "진행률 확인 중";
    const sizeText = hasUploadProgress && Number.isFinite(transferred) && Number.isFinite(total)
      ? `${transferred.toFixed(2)}/${total.toFixed(2)}GB`
      : "업로드 연결 대기";
    currentJobs.push(`
      <div class="job-row">
        <div>
          <b>YouTube 업로드 ${uploadProgress.recording_id ? `#${esc(uploadProgress.recording_id)}` : ""}</b>
          <small>${esc(uploadProgress.streamer_name || "대기열 처리 중")} · ${pctText} · ${sizeText}</small>
        </div>
        <span class="pill ${uploadProgress.stalled ? "post" : "upload"}">${uploadProgress.stalled ? "WAIT" : hasUploadProgress ? `${uploadPct}%` : "RUN"}</span>
      </div>
    `);
  }

  if (processingCount > 0) {
    currentJobs.push(`
      <div class="job-row">
        <div>
          <b>YouTube 처리 완료 확인 중</b>
          <small>업로드 전송 후 YouTube 영상 처리를 확인하고 있어요.</small>
        </div>
        <span class="pill off">${processingCount}개</span>
      </div>
    `);
  }

  const jobList = document.querySelector("#jobList");
  if (jobList) {
    jobList.innerHTML = currentJobs.length
      ? currentJobs.join("")
      : `<div class="empty">현재 처리 중인 개별 작업은 없습니다. 녹화기는 방송을 계속 감시하고 있어요.</div>`;
  }
  setText("#jobActiveCount", `${currentJobs.length}개`);
  const jobLevel = !recorderRunning ? "red" : pendingCount >= 20 ? "yellow" : "green";
  setClass("#jobHealthPill", `health-pill ${jobLevel}`);
  setText("#jobHealthPill", !recorderRunning ? "녹화기 확인" : pendingCount >= 20 ? "대기 많음" : "정상");

  const settings = document.querySelector("#settingsBox");
  const paths = status.paths || {};
  if (settings) {
    settings.innerHTML = Object.entries(paths).map(([k, v]) =>
      `<div class="setting-line"><strong>${esc(k)}</strong><span>${esc(v)}</span></div>`
    ).join("");
  }
  window.RecorderUI?.onStatus(status);
}


function uploadQueuePageTokens(current, total) {
  const pages = new Set([1, total]);
  for (let page = current - 2; page <= current + 2; page += 1) {
    if (page >= 1 && page <= total) pages.add(page);
  }
  const sorted = [...pages].sort((a, b) => a - b);
  const tokens = [];
  sorted.forEach((page, index) => {
    if (index && page - sorted[index - 1] > 1) tokens.push("...");
    tokens.push(page);
  });
  return tokens;
}

function renderUploadQueuePagination(data) {
  const pager = document.querySelector("#uploadQueuePagination");
  if (!pager) return;
  const current = Math.max(1, Number(data.page) || 1);
  const totalPages = Math.max(1, Number(data.total_pages) || 1);
  const total = Math.max(0, Number(data.total) || 0);
  uploadQueuePage = Math.min(current, totalPages);

  if (total <= uploadQueuePageSize) {
    pager.innerHTML = `<span class="queue-page-summary">총 ${total}개</span>`;
    return;
  }

  const numberButtons = uploadQueuePageTokens(uploadQueuePage, totalPages).map((token) => {
    if (token === "...") return `<span class="queue-page-gap">…</span>`;
    const active = token === uploadQueuePage ? " active" : "";
    return `<button class="queue-page-btn${active}" type="button" onclick="goToUploadQueuePage(${token})" ${active ? 'aria-current="page"' : ""}>${token}</button>`;
  }).join("");

  pager.innerHTML = `
    <span class="queue-page-summary">총 ${total}개 · ${uploadQueuePage}/${totalPages} 페이지</span>
    <div class="queue-page-buttons">
      <button class="queue-page-btn wide" type="button" onclick="goToUploadQueuePage(${uploadQueuePage - 1})" ${uploadQueuePage <= 1 ? "disabled" : ""}>이전</button>
      ${numberButtons}
      <button class="queue-page-btn wide" type="button" onclick="goToUploadQueuePage(${uploadQueuePage + 1})" ${uploadQueuePage >= totalPages ? "disabled" : ""}>다음</button>
    </div>
  `;
}

async function loadUploadQueue(page = uploadQueuePage) {
  const box = document.querySelector("#uploadQueueList");
  const pager = document.querySelector("#uploadQueuePagination");
  if (!box) return;

  const editing = () => window.RecorderUI?.shouldPauseQueueRefresh(document.activeElement, queueSavesInFlight.size)
    ?? (queueSavesInFlight.size || document.activeElement?.classList?.contains('queue-title-input'));
  if (editing()) {
    setText('#queueRefreshHint', '제목 편집 중 · 목록 갱신 잠시 대기');
    return;
  }
  const requestId = ++uploadQueueRequest;

  try {
    const requestedPage = Math.max(1, Number(page) || 1);
    const data = await api(`/api/upload_queue?page=${requestedPage}&page_size=${uploadQueuePageSize}`);
    // The user may have focused an editor while this request was in flight.
    if (requestId !== uploadQueueRequest) return;
    if (editing()) {
      setText('#queueRefreshHint', '제목 편집 중 · 목록 갱신 잠시 대기');
      return;
    }
    const restoreFocus = window.RecorderUI?.rememberFocus(box);
    setText('#queueRefreshHint', `목록 갱신 ${new Date().toLocaleTimeString('ko-KR', {hour12:false})}`);
    const items = data.items || [];
    if (data.error) {
      setText('#queueRefreshHint', '목록 조회 실패 · 다음 갱신에 재시도');
      box.innerHTML = `<div class="empty">업로드 대기열 조회 실패: ${esc(data.error)}</div>`;
      if (pager) pager.innerHTML = "";
      return;
    }
    const totalPages = Math.max(1, Number(data.total_pages) || 1);
    if (!items.length && Number(data.total) > 0 && requestedPage > totalPages) {
      uploadQueuePage = totalPages;
      await loadUploadQueue(totalPages);
      return;
    }
    if (!items.length) {
      box.innerHTML = `<div class="empty">업로드 대기/진행/실패 항목이 없습니다.</div>`;
      renderUploadQueuePagination(data);
      return;
    }

    const uploadingTotal = asNumber(lastStatus?.youtube_summary?.uploading);
    const pageOffset = asNumber(data.offset, (requestedPage - 1) * uploadQueuePageSize);
    box.innerHTML = items.map((item, idx) => {
      const st = item.youtube_status || "-";
      // Held/repair rows are still part of this visible list.  Give them a
      // stable display number instead of the confusing '-' badge without
      // changing their real DB status or making them uploadable.
      const fallbackOrder = Math.max(1, pageOffset + idx + 1 - uploadingTotal);
      const order = st === "uploading"
        ? "NOW"
        : `#${asNumber(item.queue_order) > 0 ? asNumber(item.queue_order) : fallbackOrder}`;
      const cls = st === "uploading" ? "upload" : (st === "pending" ? "rec" : ((st.includes("fail") || st === "file_missing") ? "fail" : "off"));
      const exists = item.exists ? "파일 있음" : "파일 없음";
      const existsCls = item.exists ? "exists-ok" : "exists-bad";
      const size = item.actual_size_gb ?? (item.file_size_mb ? Number(item.file_size_mb) / 1024 : null);
      const progress = Number(item.upload_progress_percent);
      const hasProgress = item.upload_progress_percent != null && Number.isFinite(progress);
      const progressHtml = st === "uploading" && hasProgress ? `
        <div class="queue-progress-label"><b>${progress}%</b><span>업로드 중</span></div>
        <div class="queue-progress"><i style="width:${Math.max(0, Math.min(100, progress))}%"></i></div>
      ` : "";
      const hasDraft = queueTitleDrafts.has(Number(item.id));
      const broadcastTitle = hasDraft ? queueTitleDrafts.get(Number(item.id)) : item.broadcast_title || "";
      const titleHelp = st === "uploading"
        ? "저장하면 전송 완료 직후 최신 제목으로 반영됩니다."
        : "저장한 제목은 이 영상이 업로드될 때 사용됩니다.";

      return `
        <div class="queue-row" data-recording-id="${Number(item.id)}">
          <div class="queue-order">${esc(order)}</div>
          <div class="queue-title">
            <b>${esc(item.display_title || item.streamer_name || "-")}</b>
            <div class="queue-title-editor">
              <label for="queueTitle${Number(item.id)}">방송 제목</label>
              <div class="queue-title-controls">
                <input
                  id="queueTitle${Number(item.id)}"
                  class="queue-title-input ${hasDraft ? 'dirty' : ''}"
                  type="text"
                  maxlength="100"
                  value="${esc(broadcastTitle)}"
                  data-original-value="${esc(item.broadcast_title || '')}"
                  placeholder="방송 제목이 없습니다"
                  onkeydown="if(event.key==='Enter'){event.preventDefault();saveUploadQueueTitle(${Number(item.id)})}"
                >
                <button class="btn small queue-title-save" type="button" onclick="saveUploadQueueTitle(${Number(item.id)})">저장</button>
              </div>
              <small class="queue-title-preview">YouTube 제목: ${esc(item.upload_title_preview || item.display_title || "-")}</small>
              <small id="queueTitleMessage${Number(item.id)}" class="queue-title-message ${hasDraft ? 'unsaved' : ''}">${hasDraft ? '저장하지 않은 제목 · 저장 버튼을 눌러 주세요.' : esc(titleHelp)}</small>
            </div>
            <details class="file-details" data-detail-key="queue:${Number(item.id)}">
              <summary>파일 위치 · ${cls === 'fail' ? '오류 확인' : '처리 메모'}</summary>
              <div><code>${esc(item.upload_path || '경로 없음')}</code>
                ${item.upload_path ? `<button type="button" class="text-button" data-copy-text="${esc(item.upload_path)}">경로 복사</button>` : ''}
                ${item.youtube_error ? `<p>${esc(item.youtube_error)}</p>` : ''}
              </div>
            </details>
          </div>
          <div class="queue-meta">
            상태
            <strong><span class="pill ${cls}" title="${esc(st)}">${esc(window.RecorderUI?.statusLabel(st) || st)}</span></strong>
          </div>
          <div class="queue-status">
            <div class="queue-meta">크기<strong>${Number.isFinite(size) ? size.toFixed(2) + "GB" : "-"}</strong></div>
            ${progressHtml}
            <small class="${existsCls}">${exists}</small>
          </div>
        </div>
      `;
    }).join("");
    window.RecorderUI?.onQueue();
    restoreFocus?.();
    renderUploadQueuePagination(data);
  } catch (e) {
    if (requestId !== uploadQueueRequest) return;
    setText('#queueRefreshHint', '목록 조회 실패 · 다음 갱신에 재시도');
    box.innerHTML = `<div class="empty">업로드 대기열 조회 실패: ${esc(e.message || e)}</div>`;
    if (pager) pager.innerHTML = "";
  }
}

window.loadUploadQueue = loadUploadQueue;

window.goToUploadQueuePage = async function(page) {
  const target = Math.max(1, Number(page) || 1);
  if (target === uploadQueuePage) return;
  uploadQueuePage = target;
  await loadUploadQueue(target);
  document.querySelector(".queue-headline")?.scrollIntoView({ behavior: "smooth", block: "start" });
};

window.saveUploadQueueTitle = async function(recordingId) {
  const input = document.querySelector(`#queueTitle${recordingId}`);
  const message = document.querySelector(`#queueTitleMessage${recordingId}`);
  const row = input?.closest(".queue-row");
  const button = row?.querySelector(".queue-title-save");
  if (!input) return;
  if (queueSavesInFlight.has(Number(recordingId))) return;
  queueSavesInFlight.add(Number(recordingId));

  const originalButtonText = button?.textContent || "저장";
  const submittedValue = input.value;
  if (button) {
    button.disabled = true;
    button.textContent = "저장 중";
  }
  if (message) {
    message.className = "queue-title-message saving";
    message.textContent = "방송 제목을 저장하고 있어요...";
  }

  try {
    const result = await api(`/api/upload_queue/${encodeURIComponent(recordingId)}/title`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ broadcast_title: submittedValue.trim() }),
    });
    const editedDuringSave = input.value !== submittedValue;
    input.dataset.originalValue = result.broadcast_title || '';
    if (!editedDuringSave) {
      queueTitleDrafts.delete(Number(recordingId));
      input.value = result.broadcast_title || "";
      input.classList.remove('dirty');
    } else {
      queueTitleDrafts.set(Number(recordingId), input.value);
      input.classList.add('dirty');
    }
    const preview = row?.querySelector(".queue-title-preview");
    if (preview) preview.textContent = `YouTube 제목: ${result.upload_title_preview || "-"}`;
    if (message) {
      message.className = editedDuringSave ? 'queue-title-message unsaved' : 'queue-title-message saved';
      message.textContent = editedDuringSave ? '이전 제목은 저장했어요. 방금 수정한 제목은 아직 저장 전입니다.' : result.message || "저장했습니다.";
    }
  } catch (e) {
    if (message) {
      message.className = "queue-title-message error";
      message.textContent = `저장 실패: ${e.message || e}`;
    }
  } finally {
    queueSavesInFlight.delete(Number(recordingId));
    if (button) {
      button.disabled = false;
      button.textContent = originalButtonText;
    }
  }
};


function calculateDailyUploadStats(rows) {
  const startOfToday = new Date();
  startOfToday.setHours(0, 0, 0, 0);
  const startOfYesterday = new Date(startOfToday);
  startOfYesterday.setDate(startOfYesterday.getDate() - 1);
  const startOfLast7Days = new Date(startOfToday);
  startOfLast7Days.setDate(startOfLast7Days.getDate() - 7);

  let todayCount = 0;
  let todayMb = 0;
  let yesterdayCount = 0;
  let yesterdayMb = 0;
  let last7Count = 0;
  let last7Mb = 0;
  let todayAddedMb = 0;
  let todayDeletedMb = 0;
  let last7AddedMb = 0;
  let last7DeletedMb = 0;

  for (const row of Array.isArray(rows) ? rows : []) {
    const sizeMb = Math.max(0, Number(row.file_size_mb) || 0);
    const completedAt = parseLocalDateTime(row.ended_at || row.created_at);
    if (row.status === "done" && completedAt) {
      if (completedAt >= startOfToday) {
        todayAddedMb += sizeMb;
      } else if (completedAt >= startOfLast7Days) {
        last7AddedMb += sizeMb;
      }
    }

    if (!["done", "uploaded_pending_youtube_processing"].includes(row.youtube_status)) continue;
    const doneAt = parseLocalDateTime(row.youtube_done_at || row.youtube_started_at);
    if (!doneAt) continue;

    if (doneAt >= startOfToday) {
      todayCount += 1;
      todayMb += sizeMb;
      if (Number(row.deleted_after_upload) === 1) todayDeletedMb += sizeMb;
    } else if (doneAt >= startOfYesterday) {
      yesterdayCount += 1;
      yesterdayMb += sizeMb;
    }

    if (doneAt >= startOfLast7Days && doneAt < startOfToday) {
      last7Count += 1;
      last7Mb += sizeMb;
      if (Number(row.deleted_after_upload) === 1) last7DeletedMb += sizeMb;
    }
  }

  return {
    today_count: todayCount,
    today_gb: todayMb / 1024,
    yesterday_count: yesterdayCount,
    yesterday_gb: yesterdayMb / 1024,
    last_7d_count: last7Count,
    last_7d_gb: last7Mb / 1024,
    avg_7d_count: last7Count / 7,
    avg_7d_gb: last7Mb / 1024 / 7,
    today_added_gb: todayAddedMb / 1024,
    today_deleted_gb: todayDeletedMb / 1024,
    today_net_gb: (todayAddedMb - todayDeletedMb) / 1024,
    avg_7d_added_gb: last7AddedMb / 1024 / 7,
    avg_7d_deleted_gb: last7DeletedMb / 1024 / 7,
  };
}


async function loadStatus() {
  const status = await api("/api/status");
  // The static dashboard can be refreshed before the Python server is
  // restarted. Derive the same figures from the existing recordings API so
  // the new card works immediately, then prefer the server-side aggregate.
  if (!status.youtube_daily_stats) {
    try {
      const rows = await api("/api/recordings?limit=500");
      status.youtube_daily_stats = calculateDailyUploadStats(rows);
    } catch (_) {
      status.youtube_daily_stats = {};
    }
  }
  lastStatus = status;
  updateDashboard(status);
}

async function loadStreamers() {
  const items = await api("/api/streamers");
  setText("#streamerCount", items.length);

  const box = document.querySelector("#streamerTable");
  if (!box) return;
  if (!items.length) {
    box.innerHTML = `<div class="empty">등록된 스트리머가 없습니다.</div>`;
    return;
  }

  box.innerHTML = items.map((item) => `
    <div class="streamer-row">
      <div>
        <b>${esc(item.name)}</b>
        <small>${esc(item.quality)} · ${esc(item.url)}</small>
      </div>
      <div class="table-actions">
        <span class="pill ${item.enabled ? "live" : "off"}">${item.enabled ? "ON" : "OFF"}</span>
        <button class="btn small ghost" onclick="toggleStreamer('${esc(item.name)}')">${item.enabled ? "OFF 전환" : "ON 전환"}</button>
        <button class="btn small bad-soft" onclick="deleteStreamer('${esc(item.name)}')">삭제</button>
      </div>
    </div>
  `).join("");
}

async function loadLogs() {
  const box = document.querySelector("#logBox");
  if (box) box.textContent = await api("/api/logs?lines=180") || "로그가 없습니다.";
}

async function loadHistory() {
  const data = await api("/api/recordings?limit=60");
  const box = document.querySelector("#recordingHistory");
  if (!box) return;

  if (data.error) {
    box.innerHTML = `<div class="empty">DB 조회 실패: ${esc(data.error)}</div>`;
    return;
  }
  if (!data.length) {
    box.innerHTML = `<div class="empty">아직 DB 녹화 기록이 없습니다.</div>`;
    return;
  }

  box.innerHTML = data.map((r) => {
    const status = r.status || "-";
    const cls = status === "done" ? "done" : ((status.includes("fail") || status.includes("error")) ? "fail" : "rec");
    const ys = r.youtube_status || "-";
    const ycls = ys === "done" ? "done" : (ys === "uploading" ? "upload" : (ys === "pending" ? "rec" : ((ys.includes("fail") || ys === "file_missing") ? "fail" : "off")));
    return `
      <div class="history-row">
        <div>
          <b>${esc(r.streamer_name)}</b>
          <small>
            ${esc(r.started_at)} → ${esc(r.ended_at || "-")} · ${esc(r.quality || "-")} · ${esc(r.file_size_mb || "-")}MB
            <br>${esc(r.final_path || r.error_message || "")}
            <br>YouTube: ${esc(r.youtube_url || r.youtube_error || ys)}
          </small>
        </div>
        <div class="table-actions">
          <span class="pill ${cls}">${esc(status)}</span>
          <span class="pill ${ycls}">${esc(ys)}</span>
        </div>
      </div>
    `;
  }).join("");
}

async function refresh() {
  if (refreshInFlight) return;
  refreshInFlight = true;
  try {
    await loadStatus();
    // Refresh only visible secondary panels; do not poll every hidden page.
    const activePage = document.querySelector('.page.active')?.id;
    const loaders = {dashboard:loadUploadQueue, streamers:loadStreamers, logs:loadLogs, history:loadHistory, thumbnails:loadThumbnails};
    if (loaders[activePage]) {
      try { await loaders[activePage](); }
      catch (error) { console.error(error); window.RecorderUI?.toast('이 화면의 정보를 갱신하지 못했어요. 다시 시도해 주세요.'); }
    }
  } catch (e) {
    console.error(e);
    window.RecorderUI?.connectionError();
  } finally {
    refreshInFlight = false;
  }
}

document.querySelector('#refreshViewBtn')?.addEventListener('click', async () => {
  const button = document.querySelector('#refreshViewBtn');
  if (refreshInFlight) { window.RecorderUI?.toast('이미 새 정보를 확인하고 있어요.'); return; }
  button.disabled = true;
  button.textContent = '확인 중…';
  try { await refresh(); }
  finally { button.disabled = false; button.textContent = '화면 새로고침'; }
});

window.toggleStreamer = async function(name) {
  const isRecording = (lastStatus?.active_recordings || []).some((r) => r.name === name);
  if (isRecording) {
    const ok = confirm(
      `${name}을 OFF로 전환할까요?\n\n현재 녹화 중인 part를 안전하게 마감하고 병합 단계로 넘깁니다.`
    );
    if (!ok) return;
  }
  await api(`/api/streamers/${encodeURIComponent(name)}/toggle`, { method: "PUT" });
  await refresh();
};

window.deleteStreamer = async function(name) {
  if (!confirm(`${name} 삭제할까요?`)) return;
  await api(`/api/streamers/${encodeURIComponent(name)}`, { method: "DELETE" });
  await refresh();
};

document.querySelector("#addForm")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = {
    enabled: true,
    name: document.querySelector("#nameInput").value.trim(),
    url: document.querySelector("#urlInput").value.trim(),
    quality: document.querySelector("#qualityInput").value,
  };
  await api("/api/streamers", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  e.target.reset();
  await refresh();
});

document.querySelector("#startBtn")?.addEventListener("click", async () => {
  await api("/api/recorder/start", { method: "POST" });
  setTimeout(refresh, 1000);
});

document.querySelector("#reloadBtn")?.addEventListener("click", async () => {
  const ok = confirm("녹화기를 리로드할까요?\n\n현재 녹화 중인 streamlink는 종료되고, 남은 ts는 후처리됩니다.");
  if (!ok) return;
  const btn = document.querySelector("#reloadBtn");
  btn.textContent = "리로드 중...";
  btn.classList.add("disabled");
  try {
    await api("/api/recorder/reload", { method: "POST" });
    setTimeout(refresh, 2000);
  } finally {
    setTimeout(() => {
      btn.textContent = "녹화기 리로드";
      btn.classList.remove("disabled");
    }, 2500);
  }
});

document.querySelector("#stopBtn")?.addEventListener("click", async () => {
  if (!confirm('녹화기를 종료할까요? 현재 녹화와 방송 감시가 중단될 수 있습니다.')) return;
  await api("/api/recorder/stop", { method: "POST" });
  setTimeout(refresh, 1500);
});

document.querySelector("#ytStartBtn")?.addEventListener("click", async () => {
  await api("/api/youtube/start", { method: "POST" });
  setTimeout(refresh, 1000);
});

document.querySelector("#ytStopBtn")?.addEventListener("click", async () => {
  if (!confirm('업로더를 종료할까요? 진행 중인 영상 전송도 중단됩니다.')) return;
  await api("/api/youtube/stop", { method: "POST" });
  setTimeout(refresh, 1500);
});

document.querySelector("#openDriveBtn")?.addEventListener("click", async () => {
  await api("/api/open/drive", { method: "POST" });
});

document.querySelector("#openLogsBtn")?.addEventListener("click", async () => {
  await api("/api/open/logs", { method: "POST" });
});

refresh();
setInterval(refresh, 5000);
refreshDbLive();
setInterval(refreshDbLive, 2000);

async function refreshNetworkStatus() {
  try {
    const res = await fetch("/static/network_status.json?t=" + Date.now());
    const data = await res.json();

    const updated = parseLocalDateTime(data.updated_at);
    const ageMs = updated ? Date.now() - updated.getTime() : Infinity;
    const stale = !updated || ageMs > 60 * 1000;
    const down = Number(data.down_mbps);
    const up = Number(data.up_mbps);

    const downEl = document.querySelector("#netDown");
    const upEl = document.querySelector("#netUp");

    if (downEl) downEl.textContent = "↓ " + (!stale && Number.isFinite(down) ? down.toFixed(2) : "N/A");
    if (upEl) upEl.textContent = "↑ " + (!stale && Number.isFinite(up) ? up.toFixed(2) : "N/A");
    setText("#netUpdated", updated ? (stale ? `${data.updated_at} · 오래됨` : `${Math.max(0, Math.floor(ageMs / 1000))}초 전`) : "갱신 기록 없음");
  } catch (e) {
    setText("#netDown", "↓ N/A");
    setText("#netUp", "↑ N/A");
    setText("#netUpdated", "상태 파일 확인 불가");
  }
}

const thumbnailStatusLabels = {
  applied: ["적용 완료", "applied"],
  ready: ["적용 예정", "ready"],
  retry_pending: ["재시도 보류", "retry"],
  generating: ["생성 중", "generating"],
  waiting: ["업로드 대기", "waiting"],
};

function renderThumbnailGrid() {
  const grid = document.querySelector("#thumbnailGrid");
  if (!grid) return;
  const visible = thumbnailFilter === "all"
    ? thumbnailItems
    : thumbnailItems.filter((item) => item.thumbnail_status === thumbnailFilter);
  if (!visible.length) {
    grid.innerHTML = `<div class="empty thumbnail-empty">선택한 상태에 해당하는 썸네일이 없습니다.</div>`;
    return;
  }
  grid.innerHTML = visible.map((item) => {
    const [statusLabel, statusClass] = thumbnailStatusLabels[item.thumbnail_status] || ["확인 중", "waiting"];
    const image = item.image_url
      ? `<img src="${esc(item.image_url)}&client=${Date.now()}" alt="${esc(item.streamer_name)} 썸네일 미리보기" loading="lazy" />`
      : `<div class="thumbnail-placeholder"><b>PREPARING</b><span>업로드 시작 시 장면 3개를 준비합니다.</span></div>`;
    const variants = item.variant_count > 0
      ? `<span>후보 ${esc(item.active_variant || 1)}/${esc(item.variant_count)}</span>`
      : `<span>후보 준비 전</span>`;
    const link = item.youtube_url
      ? `<a class="thumbnail-youtube-link" href="${esc(item.youtube_url)}" target="_blank" rel="noopener">YouTube에서 보기 ↗</a>`
      : `<span class="thumbnail-youtube-wait">영상 ID 생성 전</span>`;
    const buttonText = item.variant_count > 1 ? "다른 장면으로 만들기" : "썸네일 만들기";
    return `
      <article class="thumbnail-card" data-recording-id="${esc(item.recording_id || "")}">
        <div class="thumbnail-preview">
          ${image}
          <span class="thumbnail-state ${statusClass}">${statusLabel}</span>
          <span class="thumbnail-streamer">${esc(item.streamer_name)}</span>
        </div>
        <div class="thumbnail-card-body">
          <div class="thumbnail-card-title">
            <b>${esc(item.upload_title || item.broadcast_title || "방송 제목 없음")}</b>
            <small>${esc(item.started_at || "날짜 정보 없음")} · 녹화 #${esc(item.recording_id || "-")}</small>
          </div>
          <div class="thumbnail-card-meta">
            ${variants}
            <span>${item.thumbnail_kind === "ai" ? "AI 고품질" : "로컬 안전 후보"}</span>
            <span>${item.source_exists ? "원본 보존 중" : item.variant_count > 1 ? "후보 보존 중" : "원본 정리됨"}</span>
          </div>
          ${item.thumbnail_error ? `<div class="thumbnail-error">${esc(item.thumbnail_error)}</div>` : ""}
          <div class="thumbnail-card-actions">
            <button class="btn blue thumbnail-regenerate" type="button"
              onclick="regenerateThumbnail(${Number(item.recording_id) || 0}, this)"
              ${!item.can_regenerate || !item.recording_id ? "disabled" : ""}>${buttonText}</button>
            ${link}
          </div>
          <small class="thumbnail-action-message" aria-live="polite"></small>
        </div>
      </article>
    `;
  }).join("");
}

async function loadThumbnails() {
  const grid = document.querySelector("#thumbnailGrid");
  if (!grid) return;
  try {
    const data = await api("/api/thumbnails");
    if (data.error) throw new Error(data.error);
    thumbnailItems = data.items || [];
    const summary = data.summary || {};
    setText("#thumbnailTotal", summary.total || 0);
    setText("#thumbnailApplied", summary.applied || 0);
    setText("#thumbnailReady", summary.ready || 0);
    setText("#thumbnailRetry", summary.retry_pending || 0);
    setText("#thumbnailUpdatedAt", `${data.updated_at || "-"} 기준 · 5초 자동 갱신`);
    renderThumbnailGrid();
  } catch (error) {
    grid.innerHTML = `<div class="empty thumbnail-empty">썸네일 상태를 불러오지 못했습니다: ${esc(error.message)}</div>`;
  }
}

window.regenerateThumbnail = async function(recordingId, button) {
  if (!recordingId || button?.disabled) return;
  const card = button.closest(".thumbnail-card");
  const message = card?.querySelector(".thumbnail-action-message");
  const oldText = button.textContent;
  button.disabled = true;
  button.textContent = "새 장면 준비 중...";
  if (message) {
    message.textContent = "다른 대표 장면을 선택하고 있습니다.";
    message.className = "thumbnail-action-message working";
  }
  try {
    const result = await api(`/api/thumbnails/${recordingId}/regenerate`, { method: "POST" });
    if (message) {
      message.textContent = result.message || "새 썸네일을 준비했습니다.";
      message.className = "thumbnail-action-message success";
    }
    await loadThumbnails();
  } catch (error) {
    if (message) {
      message.textContent = error.message || "다시 만들기에 실패했습니다.";
      message.className = "thumbnail-action-message error";
    }
  } finally {
    button.disabled = false;
    button.textContent = oldText;
  }
};

document.querySelectorAll("[data-thumbnail-filter]").forEach((button) => {
  button.addEventListener("click", () => {
    thumbnailFilter = button.dataset.thumbnailFilter || "all";
    document.querySelectorAll("[data-thumbnail-filter]").forEach((item) => item.classList.toggle("active", item === button));
    renderThumbnailGrid();
  });
});

document.querySelector("#thumbnailRefreshBtn")?.addEventListener("click", loadThumbnails);

setInterval(refreshNetworkStatus, 2000);
refreshNetworkStatus();


