/* RunBeat 前端逻辑。
 * 倍率换算规则必须与后端 app/audio.py 的 plan_ratio() 保持一致：
 * 1:1 → 需要 BPM = 目标步频；1:2 → ÷2；倍率 = 需要 BPM ÷ 原曲 BPM，再按范围截断。 */

const $ = (id) => document.getElementById(id);

const HARD_MIN_RATIO = 0.25;
const HARD_MAX_RATIO = 4.0;
const GREEN = 0.15;
const YELLOW = 0.25;

const MIN_CLIP_SEC = 5.0;    // 选区最短长度，与后端 config.MIN_CLIP_SEC 保持一致
const CLIP_TAIL_SEC = 5.0;   // 改终点时从「终点前这么多秒」开始试听，专门听收尾

const MAX_BATCH = 20;   // 与后端 config.MAX_BATCH_FILES 保持一致
const FADE_MAX = 3.0;   // 开头渐入时长上限（秒），与后端 config.MAX_FADE_SEC 保持一致

const state = {
  track: null,
  sourceBpm: null,
  spm: 170,
  mapping: '1:1',
  minRatio: 0.5,
  maxRatio: 2.0,
  metroGain: 0.5,       // 节拍声音量，试听与导出共用
  busy: false,
  // 裁剪区间（原曲时间轴）。end = null 表示一路到曲末，也就是「没裁剪」。
  clip: { start: 0, end: null },

  /* ---- 批量 ----------------------------------------------------------
   * 上面那些字段是「当前编辑这一首」的工作副本；切歌时先写回 files[current].params，
   * 再装入目标那首，渲染函数照旧读 state，不用改读数组。
   * 每项 = 上传元信息 + params（每首一份可调参数）+ checked + status（'ok'|'fail'，失败无 params）。
   */
  files: [],
  current: -1,          // 正在编辑的下标；-1 = 还没选中任何一首
};

const liveAudio = $('liveAudio');
const metroAudio = $('metroAudio');
let activeMode = null;      // 'live' | 'metro' | null
let seekDebounce = null;
let bpmDebounce = null;     // 手改 BPM 后重取拍点的防抖计时器
let beatInfo = { beat_count: 0, interval_cv: 0, detected_count: 0 };

/* 两种播放源共用一个波形控制器：live 播原曲（视窗滚动），metro 播「原曲+节拍声」
 * （视窗固定）。波形数据是原曲的，与倍率无关，取全一次后不再发请求。 */
const WAVE = {
  peaks: null,
  buckets: 0,
  duration: 0,
  beats: [],
  windowSec: 20,
  viewStart: 0,
  pinned: false,      // true = 视窗固定（片段模式），播放头自己走
  pinStart: 0,        // 固定视窗的左边界（原曲秒）
  metroStart: 0,
  time: 0,            // 当前原曲时间轴位置
  playing: false,
  raf: null,
  source: null,
};

const PLAYHEAD_X = 0.34;   // 滚动模式下播放头固定在画布这个横向比例处

const CSSV = getComputedStyle(document.documentElement);
const COL = {
  accent: CSSV.getPropertyValue('--accent').trim() || '#22d3a6',
  head: '#7aa2f7',
  wave: '#33465a',
  grid: 'rgba(34,48,64,.5)',
  fade: 'rgba(10,18,25,.66)',
};

/* ------------------------------------------------------------ 工具函数 */

function formatTime(sec) {
  if (!isFinite(sec) || sec < 0) sec = 0;
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return String(m).padStart(2, '0') + ':' + String(s).padStart(2, '0');
}

/* 四舍五入到整秒，专用于输入框。不能复用 formatTime（向下取整），否则会出现
 * 「填 0:10 却回显 00:09」这种像出错的情况。 */
function formatClock(sec) {
  const total = Math.max(0, Math.round(sec || 0));
  return String(Math.floor(total / 60)).padStart(2, '0')
    + ':' + String(total % 60).padStart(2, '0');
}

function escapeHtml(str) {
  return String(str).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

let toastTimer = null;
function toast(message, kind = 'info') {
  const el = $('toast');
  el.textContent = message;
  el.className = 'toast show ' + kind;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.className = 'toast'; }, 6000);
}

function setBtnLoading(btn, on, busyText) {
  const label = btn.querySelector('.btn-label');
  if (on) {
    if (label) {
      btn.dataset.oldLabel = label.textContent;
      label.innerHTML = '<span class="spinner"></span>' + (busyText || '处理中');
    }
    btn.disabled = true;
  } else {
    if (label && btn.dataset.oldLabel) label.textContent = btn.dataset.oldLabel;
    btn.disabled = false;
  }
}

function setPreservesPitch(audio, keep) {
  if ('preservesPitch' in audio) audio.preservesPitch = keep;
  else if ('mozPreservesPitch' in audio) audio.mozPreservesPitch = keep;
  else if ('webkitPreservesPitch' in audio) audio.webkitPreservesPitch = keep;
}

/* ------------------------------------------------------------ 倍率换算 */

/* 换算的**唯一实现**：必须能对任意一套参数算，而不是只对 state 算 ——
 * 批量列表每行都要显示自己的倍率，各算一份会和编辑器对不上。 */
function planOf(p) {
  const beatsPerStep = p.mapping === '1:2' ? 2 : 1;
  const sourceBpm = p.sourceBpm || 120;
  const targetBpm = p.spm / beatsPerStep;
  const requested = targetBpm / sourceBpm;

  const lo = Math.max(HARD_MIN_RATIO, Math.min(p.minRatio, p.maxRatio));
  const hi = Math.min(HARD_MAX_RATIO, Math.max(p.minRatio, p.maxRatio));

  const ratio = Math.min(Math.max(requested, lo), hi);
  const clamped = Math.abs(ratio - requested) > 1e-6;
  const actualSpm = sourceBpm * ratio * beatsPerStep;

  let natural;
  if (clamped) {
    natural = 'clamped';
  } else {
    const drift = Math.abs(ratio - 1);
    natural = drift <= GREEN ? 'green' : (drift <= YELLOW ? 'yellow' : 'red');
  }

  return { beatsPerStep, targetBpm, requested, ratio, clamped, actualSpm, natural, lo, hi };
}

function computePlan() {
  return planOf(state);
}

/* 进度条是**相对选区**的：0 = 区间开头，1000 = 区间结尾。没选区间时按整首。 */
function seekStartSeconds() {
  const r = clipRange();
  return r.start + (Number($('seek').value) / 1000) * r.length;
}

/* ------------------------------------------------------------ 波形示波器 */

function waveWindow() {
  const d = state.track && state.track.defaults;
  return (d && d.wave_window) || WAVE.windowSec || 20;
}

/* 当前播放位置换算到原曲时间轴：live 的 currentTime 本来就是原曲秒数；
 * metro 播的是截取片段，加上起点即可。 */
function currentOriginalTime() {
  if (WAVE.source === 'live') return liveAudio.currentTime || 0;
  if (WAVE.source === 'metro') return WAVE.metroStart + (metroAudio.currentTime || 0);
  return WAVE.time;
}

function anyPlaying() {
  return !liveAudio.paused || !metroAudio.paused;
}

function updateWaveView() {
  const dur = WAVE.duration || 1;
  const span = Math.min(WAVE.windowSec || 20, dur);
  if (WAVE.pinned) {
    let vs = WAVE.pinStart;
    if (vs + span > dur) vs = Math.max(0, dur - span);
    WAVE.viewStart = vs;
  } else {
    const vs = WAVE.time - PLAYHEAD_X * span;
    WAVE.viewStart = Math.max(0, Math.min(vs, Math.max(0, dur - span)));
  }
}

function roundRect(g, x, y, w, h, r) {
  g.beginPath();
  if (g.roundRect) { g.roundRect(x, y, w, h, r); return; }
  g.moveTo(x + r, y);
  g.arcTo(x + w, y, x + w, y + h, r);
  g.arcTo(x + w, y + h, x, y + h, r);
  g.arcTo(x, y + h, x, y, r);
  g.arcTo(x, y, x + w, y, r);
  g.closePath();
}

function resizeWaveCanvas() {
  const cvs = $('waveCanvas');
  const dpr = window.devicePixelRatio || 1;
  const w = cvs.clientWidth || 620;
  const h = cvs.clientHeight || 150;
  const pw = Math.round(w * dpr);
  const ph = Math.round(h * dpr);
  if (cvs.width !== pw || cvs.height !== ph) {
    cvs.width = pw;
    cvs.height = ph;
  }
  return { w, h, dpr };
}

function drawWave() {
  const cvs = $('waveCanvas');
  if (!cvs || !cvs.clientWidth) return;

  const { w: W, h: H, dpr } = resizeWaveCanvas();
  const g = cvs.getContext('2d');
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, W, H);

  const dur = WAVE.duration || 1;
  const span = Math.min(WAVE.windowSec || 20, dur) || 1;
  const view = WAVE.viewStart;
  const mid = H * 0.5;
  const maxH = H * 0.4;

  // ---- 时间刻度网格
  const stepSec = span <= 12 ? 2 : (span <= 25 ? 5 : 10);
  g.lineWidth = 1;
  g.strokeStyle = COL.grid;
  g.beginPath();
  for (let t = Math.ceil(view / stepSec) * stepSec; t < view + span; t += stepSec) {
    const x = Math.round(((t - view) / span) * W) + 0.5;
    g.moveTo(x, 0);
    g.lineTo(x, H);
  }
  g.stroke();

  // ---- 波形：每个像素列取该列覆盖到的峰值点里的最大值
  const peaks = WAVE.peaks;
  if (peaks && peaks.length) {
    const n = peaks.length;
    g.fillStyle = COL.wave;
    for (let x = 0; x < W; x++) {
      const t0 = view + (x / W) * span;
      const t1 = view + ((x + 1) / W) * span;
      let i0 = Math.floor((t0 / dur) * n);
      let i1 = Math.ceil((t1 / dur) * n);
      if (i1 <= i0) i1 = i0 + 1;
      if (i0 < 0) i0 = 0;
      if (i1 > n) i1 = n;
      if (i0 >= n || i1 <= i0) continue;
      let p = 0;
      for (let i = i0; i < i1; i++) { const v = peaks[i]; if (v > p) p = v; }
      const hh = Math.max(1, (p / 1000) * maxH);
      g.fillRect(x, mid - hh, 1, hh * 2);
    }
  }

  // ---- 中线（基线）
  g.strokeStyle = 'rgba(34,48,64,.95)';
  g.beginPath();
  g.moveTo(0, Math.round(mid) + 0.5);
  g.lineTo(W, Math.round(mid) + 0.5);
  g.stroke();

  const headX = Math.max(0, Math.min(W, ((WAVE.time - view) / span) * W));

  // ---- 已播放部分压暗（画在拍点线之前，这样拍点线始终清晰）
  if (headX > 1) {
    g.fillStyle = COL.fade;
    g.fillRect(0, 0, headX, H);
  }

  // ---- 拍点线
  if (WAVE.beats.length) {
    const y0 = mid - maxH * 1.06;
    const y1 = mid + maxH * 1.06;
    g.strokeStyle = COL.accent;
    g.lineWidth = 1;
    g.beginPath();
    for (let k = 0; k < WAVE.beats.length; k++) {
      const bx = ((WAVE.beats[k] - view) / span) * W;
      if (bx > W + 2) break;          // 拍点是有序的，出了右边界就可以停
      if (bx < -2) continue;
      const x = Math.round(bx) + 0.5;
      g.moveTo(x, y0);
      g.lineTo(x, y1);
    }
    g.stroke();
  }

  // ---- 播放头（播放时呼吸）
  g.save();
  const alpha = WAVE.playing
    ? 0.5 + 0.5 * (0.5 + 0.5 * Math.sin(performance.now() / 420))
    : 1;
  g.globalAlpha = alpha;
  g.strokeStyle = COL.head;
  g.lineWidth = 1.5;
  g.beginPath();
  const hx = Math.round(headX) + 0.5;
  g.moveTo(hx, 0);
  g.lineTo(hx, H);
  g.stroke();

  // 播放头顶部的蓝色时间标签
  g.globalAlpha = 1;
  g.font = '600 10px system-ui, -apple-system, "Segoe UI", sans-serif';
  const label = formatTime(WAVE.time);
  const tw = g.measureText(label).width;
  const bw = tw + 12;
  const bx = Math.max(3, Math.min(W - bw - 3, hx - bw / 2));
  g.fillStyle = COL.head;
  roundRect(g, bx, 4, bw, 15, 4);
  g.fill();
  g.fillStyle = '#08121c';
  g.textAlign = 'center';
  g.textBaseline = 'middle';
  g.fillText(label, bx + bw / 2, 12);
  g.restore();
}

function syncScopeUI() {
  const dur = WAVE.duration || 1;
  const frac = Math.max(0, Math.min(1, WAVE.time / dur));
  $('scopeFill').style.width = (frac * 100).toFixed(2) + '%';
  $('scopeClock').textContent = formatTime(WAVE.time);
}

function waveTick() {
  if (!WAVE.playing) { WAVE.raf = null; return; }
  if (!anyPlaying()) { stopWave(); return; }   // 兜底：任何原因导致全停了就收工
  WAVE.time = currentOriginalTime();

  // 试听只在选区内跑：播到区间末尾就停并把播放头放回区间开头。
  // 逐帧判断而不是靠 timeupdate（约 250ms 才响一次，尾巴会拖出去）。
  if (WAVE.source === 'live' && clipActive() && WAVE.time >= clipRange().end - 0.02) {
    $('seek').value = 0;         // 进度条是相对选区的，归零 = 回到区间开头
    stopAll();
    drawWave();
    return;
  }

  updateWaveView();
  drawWave();
  syncScopeUI();
  WAVE.raf = requestAnimationFrame(waveTick);
}

function startWave() {
  WAVE.playing = true;
  $('beatDot').hidden = false;
  if (!WAVE.raf) WAVE.raf = requestAnimationFrame(waveTick);
}

function stopWave() {
  WAVE.playing = false;
  $('beatDot').hidden = true;
  if (WAVE.raf) { cancelAnimationFrame(WAVE.raf); WAVE.raf = null; }
  WAVE.time = currentOriginalTime();
  updateWaveView();
  drawWave();
  syncScopeUI();
}

/* activeMode 是「当前在放什么」的唯一入口。没有音源在播时播放条就过期了，
 * 必须收起来 —— 放这里是因为停下来的路径不止一条。 */
function setActive(mode) {
  activeMode = mode;
  WAVE.source = mode;
  if (!mode) $('nowPlaying').hidden = true;
  syncBeatPulse();
  syncClipPreviewBtn();
}

/* 「节拍声闪一下」的小点：只有即时试听且开着节拍声时才出现。 */
function syncBeatPulse() {
  const el = $('beatPulse');
  if (el) el.hidden = !(activeMode === 'live' && previewMetronomeOn());
}

/* 试听中时按钮就是停止键。两处按钮控制同一播放（liveAudio），状态必须一致。 */
function syncClipPreviewBtn() {
  const btn = $('clipPreviewBtn');
  if (!btn) return;
  const playing = activeMode === 'live';
  const label = btn.querySelector('.btn-label');
  if (label) label.textContent = playing ? '停止试听' : '试听这段';
  btn.classList.toggle('primary', !playing);
}

/* ------------------------------------------------------------ 界面渲染 */

const NATURAL_LABEL = { green: '舒适', yellow: '偏勉强', red: '很勉强', clamped: '已被截断' };

function renderStatus() {
  if (!state.sourceBpm) return;
  const p = computePlan();

  $('ratioText').textContent = '×' + p.ratio.toFixed(3);
  const badge = $('naturalBadge');
  badge.textContent = NATURAL_LABEL[p.natural];
  badge.className = 'badge ' + p.natural;
  $('status').className = 'status ' + p.natural;

  const resultBpm = state.sourceBpm * p.ratio;
  // 有裁剪区间时，时长按「这一段」算 —— 显示整曲时长会误导人
  const inDur = clipRange().length || (state.track ? state.track.duration : 0);
  const outputDur = inDur / p.ratio;
  const deltaPct = (p.ratio - 1) * 100;
  const dir = deltaPct >= 0 ? '加速' : '放慢';

  let detail = '';
  if (p.clamped) {
    detail = `要凑出 ${state.spm} spm 需要 ${dir} ${Math.abs((p.requested - 1) * 100).toFixed(1)}%，`
      + `超出你设的 ${p.lo.toFixed(2)}x~${p.hi.toFixed(2)}x，已按边界 ×${p.ratio.toFixed(3)} 生成，`
      + `<b>实际是 ${p.actualSpm.toFixed(0)} spm</b>。想真的到 ${state.spm}，得放宽范围或换一首 BPM 更接近的歌。`;
  } else {
    const tail = p.natural === 'red'
      ? '这个幅度会明显听出别扭，建议换个 BPM 更接近的歌。'
      : (p.natural === 'yellow' ? '已经能听出节奏被拉扯了。' : '基本听不出处理痕迹。');
    detail = `原曲 ${state.sourceBpm.toFixed(1)} BPM ${dir}到 ${resultBpm.toFixed(1)} BPM`
      + `（${deltaPct >= 0 ? '+' : ''}${deltaPct.toFixed(1)}%），${tail}<br>`
      + `${clipActive() ? '这段' : '整曲'}时长 <b>${formatTime(inDur)} → ${formatTime(outputDur)}</b>`;
  }
  $('statusDetail').innerHTML = detail;
}

function renderMappingHint() {
  const beatsPerStep = state.mapping === '1:2' ? 2 : 1;
  const need = Math.round(state.spm / beatsPerStep);
  $('mappingHint').textContent = state.mapping === '1:1'
    ? `${state.spm} spm 需要 ${need} BPM 的歌 —— 每一步都踩在拍子上。`
    : `${state.spm} spm 只要 ${need} BPM 的歌 —— 每两步踩一拍，歌单好找很多。`;
}

/* 候选值与检测值的关系标注，让用户一眼看出「这个按钮会把节拍翻倍还是减半」 */
const RELATIONS = [[2, '×2'], [0.5, '÷2'], [2 / 3, '×2/3'], [1.5, '×3/2']];

function relationLabel(value, base) {
  if (!base) return '';
  if (Math.abs(value - base) < 0.05) return '当前';
  const r = value / base;
  for (const [k, label] of RELATIONS) {
    if (Math.abs(r - k) < 0.02) return label;
  }
  return '';
}

function renderBpmChips() {
  const host = $('bpmChips');
  host.innerHTML = '';
  const list = (state.track && state.track.bpm_candidates) || [];
  if (!list.length) return;
  const base = state.track.detected_bpm;

  list.forEach((value) => {
    const active = Math.abs(value - state.sourceBpm) < 0.05;
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'chip' + (active ? ' active' : '');
    btn.innerHTML = `<b>${value.toFixed(1)}</b><em>${escapeHtml(relationLabel(value, base))}</em>`;
    btn.title = active
      ? '当前使用的 BPM，波形上的拍点线就是这一组'
      : `切到 ${value.toFixed(1)} BPM，波形上的拍点线会立刻重画`;
    btn.addEventListener('click', () => {
      if (active) return;
      state.sourceBpm = value;
      $('bpmInput').value = value;
      renderBpmChips();
      renderBeatHead();
      renderStatus();
      loadBeats(value);
    });
    host.appendChild(btn);
  });
}

/* 可信度取两个指标里更差的：clarity（鼓点是否明确）和 interval_cv（速度是否稳）。
 * 只看前者会把现场版判成可靠，只看后者会把节奏干脆的电子乐判成不可靠。 */
function confidenceInfo() {
  const clarity = (state.track && state.track.clarity) || 0;
  const cv = beatInfo.interval_cv || 0;
  const a = clarity >= 6 ? 3 : (clarity >= 4 ? 2 : 1);
  const b = cv <= 0.08 ? 3 : (cv <= 0.16 ? 2 : 1);
  const lv = Math.min(a, b);

  let text;
  if (lv === 3) text = '鼓点清晰、速度稳定';
  else if (a === 1 && b === 1) text = '鼓点弱、速度也不稳';
  else if (a < b) text = '鼓点不够清晰';
  else if (b < a) text = '速度不太稳定';
  else text = '结果比较可信';
  return { lv, text };
}

function renderBeatHead() {
  $('bpmBig').textContent = state.sourceBpm ? state.sourceBpm.toFixed(1) : '—';
  const { lv, text } = confidenceInfo();
  $('confBars').className = 'conf-bars lv' + lv;
  $('confText').textContent = text;
}

function renderVerdict() {
  const el = $('verdict');
  const n = beatInfo.beat_count || 0;
  const cv = beatInfo.interval_cv || 0;
  const cvPct = (cv * 100).toFixed(1);
  const clarity = state.track ? state.track.clarity : null;

  let cls = 'ok';
  let html;

  if (n < 4) {
    cls = 'bad';
    html = `只找到 <b>${n}</b> 个拍点，太少了 —— 这首曲子的节拍可能根本测不出来，换一首节奏更明确的歌试试。`;
  } else if (cv > 0.16) {
    cls = 'warn';
    html = `共 <b>${n}</b> 个拍点，但间隔波动达 <b>${cvPct}%</b> —— 这首曲子的速度一直在变（现场版、古典里常见），`
      + `<b>它不存在唯一的 BPM</b>，检测值只能算个平均值。`;
  } else if (cv > 0.08) {
    cls = 'warn';
    html = `共 <b>${n}</b> 个拍点，间隔波动 <b>${cvPct}%</b> —— 基本规整，但速度偏松，检测值属于平均值。`;
  } else {
    html = `共 <b>${n}</b> 个拍点，间隔波动只有 <b>${cvPct}%</b> —— 节拍很规整。`
      + `确认上面的绿线都踩在鼓点上，就可以直接往下做了。`;
  }

  // 清晰度低时补一句提示（已与顶部可信度文字合并，避免重复）
  if (clarity !== null && clarity !== undefined && clarity < 4) {
    if (cls !== 'bad') cls = 'warn';
    html += ` 不过这曲子鼓点偏弱（清晰度 ${clarity}），建议点「听节拍对齐」用耳朵再确认一次。`;
  }

  // 网格贴合度低置信提示。grid_hit = 网格点处起音强度 ÷ 全曲平均（1.0 ≈ 随机落点，
  // 3 以上算准）；低于 1.5 说明 BPM 不可信，resid 大说明曲子在飘、网格天然贴不上。
  const q = state.track ? state.track.grid_hit : null;
  const resid = state.track ? state.track.grid_fit_resid : null;
  if (q !== null && q !== undefined && q > 0 && (q < 1.5 || (resid !== null && resid > 0.15))) {
    if (cls !== 'bad') cls = 'warn';
    if (q < 1.5) {
      html += ` 拍点网格与鼓点的贴合度偏低（${q.toFixed(2)}），这个 BPM 的把握不大 —— `
        + `建议点「听节拍对齐」用耳朵确认，不对就手动改 BPM。`;
    } else {
      html += ` 这首曲子的速度在飘（拍点残差 ${(resid * 1000).toFixed(0)}ms），网格只能取平均位置。`;
    }
  }

  el.className = 'verdict ' + cls;
  el.innerHTML = html;
}

function renderBeatStat() {
  const n = beatInfo.beat_count || 0;
  const bpm = state.sourceBpm || 0;
  const gap = bpm > 0 ? (60 / bpm) : 0;
  $('beatStat').textContent = n
    ? `共 ${n} 个拍点 · 每 ${gap.toFixed(2)} 秒一个`
    : '暂无拍点';
}

/* ------------------------------------------------------------ 数据加载 */

let beatsToken = 0;

async function loadBeats(bpm) {
  if (!state.track) return;
  // 拍点换了，正在放的节拍声就对不上了，先停掉（不自动重生成，避免意外出声）
  if (WAVE.source === 'metro') stopAll();
  const token = ++beatsToken;
  try {
    const res = await fetch(`/api/beats/${state.track.file_id}?bpm=${encodeURIComponent(bpm)}`);
    const d = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(d.detail || `拍点计算失败（HTTP ${res.status}）`);
    if (token !== beatsToken) return;      // 已经有更新的请求发出去了，丢弃这次结果
    WAVE.beats = d.beats || [];
    invalidateClickTimes();        // 拍点整组换了，浏览器里那份 click 位置作废
    resyncClicks();
    beatInfo = {
      beat_count: d.beat_count || 0,
      interval_cv: d.interval_cv || 0,
      detected_count: d.detected_count || 0,
    };
    renderBeatHead();
    renderBeatStat();
    renderVerdict();
    drawWave();
  } catch (err) {
    if (token === beatsToken) {
      $('beatStat').textContent = '拍点读取失败';
    }
  }
}

async function loadWaveform(fileId) {
  $('scopeEmpty').hidden = false;
  $('scopeEmpty').textContent = '正在读取波形…';
  try {
    const res = await fetch(`/api/waveform/${fileId}`);
    const d = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(d.detail || '波形读取失败');
    if (!state.track || state.track.file_id !== fileId) return;   // 已经换歌了
    WAVE.peaks = d.peaks || [];
    WAVE.buckets = d.buckets || 0;
    WAVE.duration = d.duration || state.track.duration;
    WAVE.windowSec = d.window || waveWindow();
    $('scopeEmpty').hidden = true;
    $('clipEmpty').hidden = true;
    $('scopeTotal').textContent = '/ ' + formatTime(WAVE.duration);
    updateWaveView();
    drawWave();
    drawClipCanvas();
  } catch (err) {
    $('scopeEmpty').hidden = false;
    $('scopeEmpty').textContent = '波形读取失败';
  }
}

const NOW_PLAYING_TAG = {
  live: '试听（本地变速）',
  metro: '节拍对齐（原曲 + 节拍声）',
};

function showNowPlaying(mode, info) {
  const host = $('nowPlaying');
  host.hidden = false;
  $('nowPlayingTag').textContent = NOW_PLAYING_TAG[mode] || mode;
  $('nowPlayingTag').className = 'tag ' + mode;
  $('nowPlayingInfo').textContent = info;
}

/* ------------------------------------------------------------ 裁剪区间
 *
 * 在整曲缩略波形上拉一段，后续试听 / 听节拍对齐 / 生成只处理这一段。
 * 端点吸附到最近拍点（最多挪 CLIP_SNAP_SEC 秒），让变速后开头压在一拍上。
 * 没有选区时一律按整首处理。
 */

function trackDuration() {
  return (state.track && state.track.duration) || 0;
}

/* 当前选区（原曲时间轴）。end 为 null 表示到曲末。 */
function clipRange() {
  const total = trackDuration();
  const start = Math.max(0, Math.min(state.clip.start || 0, total));
  const raw = state.clip.end == null ? total : state.clip.end;
  const end = Math.max(start, Math.min(raw, total));
  return { start, end, length: Math.max(0, end - start) };
}

/* 是不是真的选了「一段」——整首不算。 */
function clipActive() {
  const r = clipRange();
  return r.start > 0.05 || r.end < trackDuration() - 0.05;
}

/* 开头渐进切入的时长：没勾就是 0（不淡入），勾了取输入框的值并夹在上限内。 */
function fadeSeconds() {
  if (!$('clipFade').checked) return 0;
  const v = Number($('clipFadeSec').value);
  if (!Number.isFinite(v) || v <= 0) return 0;
  return Math.min(v, FADE_MAX);
}

/* 没勾时把时长框置灰 —— 留着可编辑会让人以为改它有用。 */
function syncFadeUI() {
  $('clipFadeSec').disabled = !$('clipFade').checked;
}

/* 找离 t 最近的拍点。刻意**不设**固定吸附半径 —— 半径小于半个拍距时会出现
 * 「这次吸上、下次吸不上」的薛定谔行为。始终取最近一拍，偏移最多半拍。 */
function snapToBeat(t) {
  const beats = WAVE.beats;
  if (!beats || !beats.length) return t;
  let lo = 0;
  let hi = beats.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (beats[mid] < t) lo = mid + 1; else hi = mid;
  }
  let best = beats[lo];
  if (lo > 0 && Math.abs(beats[lo - 1] - t) < Math.abs(best - t)) best = beats[lo - 1];
  return best;
}

/* 改选区的唯一入口：夹范围 → （可选）吸附 → 保证不短于 MIN_CLIP_SEC → 刷新界面。
 * 拖拽过程中传 snap=false（跟手），松手时传 true（吸附）。 */
function setClip(start, end, snap) {
  const total = trackDuration();
  let s = Math.max(0, Math.min(Number(start) || 0, total));
  let e = Math.max(0, Math.min(Number(end) || 0, total));
  if (e < s) { const tmp = s; s = e; e = tmp; }

  if (snap) {
    if (s > 0.02) s = snapToBeat(s);
    if (e < total - 0.02) e = snapToBeat(e);
  }

  // 吸附有可能把区间压到最短长度以下：优先往后撑，撑不下就整体前移
  if (e - s < MIN_CLIP_SEC) {
    e = Math.min(total, s + MIN_CLIP_SEC);
    s = Math.max(0, e - MIN_CLIP_SEC);
  }

  state.clip.start = s;
  state.clip.end = e >= total - 0.005 ? null : e;   // 顶到曲末就记成 null，等于没裁
  syncClipUI();
}

/* 把选区同步到所有相关的地方：标题状态、两个时间框、长度、缩略波形、进度条 */
function syncClipUI() {
  const total = trackDuration();
  const r = clipRange();
  const active = clipActive();

  $('clipStart').value = formatClock(r.start);
  $('clipEnd').value = formatClock(r.end);
  $('clipLength').textContent = active
    ? `共 ${formatClock(r.length)}`
    : `整首 ${formatClock(total)}`;
  $('clipState').textContent = active
    ? `${formatClock(r.start)} – ${formatClock(r.end)}`
    : '整首';
  $('clipState').classList.toggle('on', active);

  // 进度条跟着选区走：选了区间就只在区间里走，没选就是整首
  $('seekTotal').textContent = formatTime(r.end);
  syncSeekBar(WAVE.time);

  drawClipCanvas();
}

function clipLocalX(ev) {
  return ev.clientX - $('clipCanvas').getBoundingClientRect().left;
}

function clipXToTime(x) {
  const W = $('clipCanvas').clientWidth || 1;
  const total = trackDuration();
  return Math.max(0, Math.min(total, (x / W) * total));
}

/* 命中了哪个手柄？都没中返回 null —— 那就是在拉一个新选区。 */
function clipHandleAt(x) {
  if (!clipActive()) return null;
  const W = $('clipCanvas').clientWidth || 1;
  const total = trackDuration() || 1;
  const r = clipRange();
  const d0 = Math.abs(x - (r.start / total) * W);
  const d1 = Math.abs(x - (r.end / total) * W);
  if (Math.min(d0, d1) > 13) return null;
  return d0 <= d1 ? 'start' : 'end';
}

/* 整曲缩略波形：选区外压暗、选区内高亮，两端各一个可拖的手柄 */
function drawClipCanvas() {
  const cvs = $('clipCanvas');
  if (!cvs || !cvs.clientWidth || !state.track) return;

  const dpr = window.devicePixelRatio || 1;
  const W = cvs.clientWidth;
  const H = cvs.clientHeight;
  const pw = Math.round(W * dpr);
  const ph = Math.round(H * dpr);
  if (cvs.width !== pw || cvs.height !== ph) { cvs.width = pw; cvs.height = ph; }

  const g = cvs.getContext('2d');
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, W, H);

  const total = trackDuration() || 1;
  const peaks = WAVE.peaks;
  const r = clipRange();
  const active = clipActive();
  const mid = H * 0.5;
  const maxH = H * 0.32;
  const selX0 = (r.start / total) * W;
  const selX1 = (r.end / total) * W;

  // ---- 选区底色（铺在波形下面）
  if (active) {
    g.fillStyle = 'rgba(34,211,166,.10)';
    g.fillRect(selX0, 0, Math.max(1, selX1 - selX0), H);
  }

  // ---- 波形：整首先画一遍暗的，选区内再覆盖一遍亮的
  const paint = (fromX, toX, color) => {
    if (!peaks || !peaks.length || toX <= fromX) return;
    const n = peaks.length;
    const a = Math.max(0, Math.floor(fromX));
    const b = Math.min(W, Math.ceil(toX));
    g.fillStyle = color;
    for (let x = a; x < b; x++) {
      let i0 = Math.floor((x / W) * n);
      let i1 = Math.ceil(((x + 1) / W) * n);
      if (i1 <= i0) i1 = i0 + 1;
      if (i0 < 0) i0 = 0;
      if (i1 > n) i1 = n;
      if (i0 >= n) continue;
      let p = 0;
      for (let i = i0; i < i1; i++) { const v = peaks[i]; if (v > p) p = v; }
      const hh = Math.max(1, (p / 1000) * maxH);
      g.fillRect(x, mid - hh, 1, hh * 2);
    }
  };

  paint(0, W, 'rgba(90,108,128,.55)');
  if (active) paint(selX0, selX1, COL.accent);

  // ---- 中线
  g.strokeStyle = 'rgba(34,48,64,.9)';
  g.lineWidth = 1;
  g.beginPath();
  g.moveTo(0, Math.round(mid) + 0.5);
  g.lineTo(W, Math.round(mid) + 0.5);
  g.stroke();

  // ---- 两个手柄
  if (active) {
    const hw = 9;
    const put = (x) => {
      const cx = Math.max(hw / 2, Math.min(W - hw / 2, x));
      g.fillStyle = COL.accent;
      roundRect(g, cx - hw / 2, 3, hw, H - 6, 4);
      g.fill();
      g.strokeStyle = 'rgba(4,36,28,.8)';
      g.lineWidth = 1;
      g.beginPath();
      g.moveTo(cx - 1.5, H * 0.36);
      g.lineTo(cx - 1.5, H * 0.64);
      g.moveTo(cx + 1.5, H * 0.36);
      g.lineTo(cx + 1.5, H * 0.64);
      g.stroke();
    };
    put(selX0);
    put(selX1);
  }

  // ---- 两端的时间刻度
  g.font = '600 11px system-ui, -apple-system, "Segoe UI", sans-serif';
  g.textBaseline = 'alphabetic';
  g.fillStyle = 'rgba(139,152,165,.85)';
  g.textAlign = 'left';
  g.fillText(formatTime(0), 5, H - 5);
  g.textAlign = 'right';
  g.fillText(formatTime(total), W - 5, H - 5);
}

/* 选区变了之后该从哪儿接着听：改起点 → 新起点；改终点 → 终点前 CLIP_TAIL_SEC 秒。
 * 区间本身短于 CLIP_TAIL_SEC 时自然退回区间开头。 */
function clipAnchorTime(anchor) {
  const r = clipRange();
  if (anchor === 'end') return Math.max(r.start, r.end - CLIP_TAIL_SEC);
  return r.start;
}

/* 把「下次试听从哪里开始」说出来 —— 改终点会自动跳到终点前 5 秒，光看进度条看不出来。 */
function setClipTip(anchor) {
  const el = $('clipTip');
  if (!el) return;
  if (!state.track) { el.textContent = '—'; return; }
  const r = clipRange();
  const t = clipAnchorTime(anchor);
  const fromTail = anchor === 'end' && r.end - t > 0.5;
  el.textContent = `试听将从 ${formatClock(t)} 开始`
    + (fromTail ? `（终点前 ${CLIP_TAIL_SEC.toFixed(0)} 秒）` : '');
}

/* 选区变了把播放位置挪到 anchor：正在放就跳过去接着放不打断；没在放只挪好位置，
 * 等点「试听这段」从这里开始。两种状态都处理，改起点/改终点的行为才不会只生效一半。 */
function repositionAfterClip(anchor) {
  syncClipUI();                     // 进度条是相对选区的，先按新区间归一化
  const t = clipAnchorTime(anchor);
  syncSeekBar(t);                   // 进度条落到 anchor 那一点 = 下次播放的起点
  setClipTip(anchor);

  if (activeMode === 'live' && !liveAudio.paused) {
    // 正在放：跳到新位置接着放
    try { liveAudio.currentTime = t; } catch (e) { /* 元数据未就绪时忽略 */ }
    armClickSettle();               // 位置跳了：排出去的 click 作废，等媒体时钟稳下来再对准
    // 跳到新起点等于从这一段的开头接着放，和点「试听这段」是同一种开头，也该渐入；
    // 拖终点落在终点前 5 秒（不是段首）就照旧直接切进去，别把收尾也淡一遍。
    const fade = anchor === 'start' ? fadeSeconds() : 0;
    if (fade > 0) startMusicFade(fade);
    else clearMusicFade();          // 落点不是段首：把可能只跑了一半的渐入收掉，音量拉回满值
    WAVE.time = t;
    updateWaveView();
    drawWave();
    return;
  }

  stopAll();                        // 没在放：停下来，顺便把播放位置对齐到进度条那一点
  WAVE.time = t;
  updateWaveView();
  drawWave();
  syncScopeUI();
}

/* 把「01:20」「80」「1:02:03」都读成秒；读不出来返回 null（调用方负责回退显示）。 */
function parseClipTime(text) {
  const s = String(text).trim();
  if (!s) return null;
  if (/^\d+(\.\d+)?$/.test(s)) return parseFloat(s);
  const ms = s.match(/^(\d+):(\d{1,2}(?:\.\d+)?)$/);
  if (ms) return parseInt(ms[1], 10) * 60 + parseFloat(ms[2]);
  const hms = s.match(/^(\d+):(\d{1,2}):(\d{1,2}(?:\.\d+)?)$/);
  if (hms) return parseInt(hms[1], 10) * 3600 + parseInt(hms[2], 10) * 60 + parseFloat(hms[3]);
  return null;
}

/* ------------------------------------------------------------ 上传 */

/* 一首歌的初始参数。每首独立的起点：之后在编辑器里改的都只写回这一首的 params。 */
function defaultParams(data) {
  return {
    sourceBpm: data.detected_bpm,
    spm: data.defaults.spm,
    mapping: '1:1',
    minRatio: data.defaults.min_ratio,
    maxRatio: data.defaults.max_ratio,
    metroGain: 0.5,
    previewMetro: false,
    exportMetro: false,
    keepPitch: true,
    clip: { start: 0, end: null, fade: false, fadeSec: data.defaults.fade_sec },
  };
}

/* 把「当前编辑视图」里的参数写回所属的那一首。
 * 切歌、应用到全部、批量导出之前都必须先存一次，否则刚改的参数会丢。 */
function snapshotCurrent() {
  const entry = state.files[state.current];
  if (!entry || entry.status !== 'ok') return;
  entry.params = {
    sourceBpm: state.sourceBpm,
    spm: state.spm,
    mapping: state.mapping,
    minRatio: state.minRatio,
    maxRatio: state.maxRatio,
    metroGain: state.metroGain,
    previewMetro: $('previewMetro').checked,
    exportMetro: $('exportMetro').checked,
    keepPitch: $('keepPitch').checked,
    // 时长存输入框里的原始数字（不是 fadeSeconds() 的结果）—— 没勾时也要记住填了多少
    clip: {
      start: state.clip.start,
      end: state.clip.end,
      fade: $('clipFade').checked,
      fadeSec: Number($('clipFadeSec').value) || 1,
    },
  };
}

/* 把某一首的参数装进「当前编辑视图」。 */
function restoreInto(index) {
  const entry = state.files[index];
  const p = entry.params;
  state.track = entry;
  state.sourceBpm = p.sourceBpm;
  state.spm = p.spm;
  state.mapping = p.mapping;
  state.minRatio = p.minRatio;
  state.maxRatio = p.maxRatio;
  state.metroGain = p.metroGain;
  state.clip = { start: p.clip.start, end: p.clip.end };
  $('clipFade').checked = !!p.clip.fade;
  $('clipFadeSec').value = String(p.clip.fadeSec || 1);
  $('previewMetro').checked = p.previewMetro;
  $('exportMetro').checked = p.exportMetro;
  $('keepPitch').checked = p.keepPitch;
  syncFadeUI();
}

/* 换编辑对象的**唯一入口**（列表点行、上传完自动进入都走这里）。
 * 顺序不能反：停播 → 存旧的 → 切下标 → 装新的 → 重置播放器 → 重画；
 * 反了会把新那首的参数覆盖成旧的，resetPlayer 也必须在 restore 之后清缓存。 */
function switchTo(index) {
  if (index === state.current) return;
  const entry = state.files[index];
  if (!entry) return;
  if (entry.status !== 'ok') {
    toast(entry.error || '这个文件没能处理成功，换一个吧', 'error');
    return;
  }

  stopAll();
  snapshotCurrent();
  state.current = index;
  restoreInto(index);
  resetPlayer();
  renderFileList();
  initUI();
}

async function handleFiles(fileList) {
  const picked = Array.from(fileList || []).filter(Boolean);
  if (!picked.length || state.busy) return;

  let batch = picked;
  if (state.files.length + batch.length > MAX_BATCH) {
    const room = Math.max(0, MAX_BATCH - state.files.length);
    if (!room) {
      toast(`列表里已经有 ${MAX_BATCH} 首了，先清掉一些再添加`, 'error');
      return;
    }
    toast(`一次最多 ${MAX_BATCH} 首，这次只收前 ${room} 个`, 'warn');
    batch = batch.slice(0, room);
  }

  state.busy = true;
  $('dropzone').classList.remove('over');
  // 上传中锁住清空按钮：异步循环还握着下标，中途清空会让它写进新列表
  $('clearListBtn').disabled = true;

  // 先把整批以「排队」状态进列表 —— 用户立刻看到这批都有谁、正在处理第几首
  const startIndex = state.files.length;
  batch.forEach((f) => {
    state.files.push({ name: f.name, status: 'wait', checked: false, params: null });
  });
  renderFileList();

  // 逐首上传：每完成一首就刷新列表，进度是真实的（后端本来就是串行逐首处理，
  // 一次传整批反而拿不到中间进度）。单首失败只记自己，不影响后面的排队。
  let okCount = 0;
  let failCount = 0;
  let firstNew = -1;

  for (let k = 0; k < batch.length; k++) {
    const row = startIndex + k;
    state.files[row].status = 'loading';
    setDropzoneBusy(k + 1, batch.length, batch[k].name);
    renderFileList();

    try {
      const form = new FormData();
      form.append('file', batch[k]);
      const res = await fetch('/api/upload', { method: 'POST', body: form });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || `上传失败（HTTP ${res.status}）`);

      state.files[row] = {
        ...data,
        params: defaultParams(data),
        checked: true,
        status: 'ok',
      };
      okCount++;

      // 第一首成功且当前没在编辑别的歌时，立刻进编辑器 ——
      // 后面的继续排队处理，用户不用等整批跑完才开始干活
      if (state.current < 0 && firstNew < 0) {
        firstNew = row;
        state.current = -1;
        switchTo(row);
      }
    } catch (err) {
      state.files[row] = {
        name: batch[k].name,
        status: 'fail',
        error: err.message || '处理失败',
        checked: false,
        params: null,
      };
      failCount++;
    }
    renderFileList();
  }

  if (failCount) {
    toast(`处理完成：成功 ${okCount} 首，失败 ${failCount} 首`, 'warn');
  } else {
    toast(`处理完成：${okCount} 首已就绪`, 'info');
  }

  state.busy = false;
  setDropzoneBusy(0);
  $('clearListBtn').disabled = false;
  renderFileList();
}

/* 上传区的忙碌提示。逐首上传时带上「第几首 / 共几首」和歌名 ——
 * 批量时用户最想知道的就是进度到哪儿了。 */
function setDropzoneBusy(done, total, name) {
  const big = $('dropzone').querySelector('.big');
  const sub = $('dropzone').querySelector('.sub');
  if (!big || !sub) return;
  if (done > 0 && total > 1) {
    big.textContent = `正在处理 ${done} / ${total} 首…`;
    sub.textContent = `当前：${name}`;
    return;
  }
  if (done > 0) {
    big.textContent = '正在上传并分析节拍…';
    sub.textContent = '检测完就出结果，请稍等';
    return;
  }
  big.textContent = '把音频文件拖到这里';
  sub.textContent = '可以一次选多个 · mp3 / wav / m4a / flac · 每个最大 20 MB、10 分钟';
}

function initUI() {
  const t = state.track;
  // 批量模式下上传区**不隐藏** —— 随时可以再拖几首进来（追加到列表末尾）。
  $('panel').hidden = false;

  const okList = state.files.filter((f) => f.status === 'ok');
  const nth = okList.indexOf(t) + 1;
  $('trackName').textContent = t.name;
  $('trackMeta').textContent =
    `第 ${nth} / ${okList.length} 首 · ${formatTime(t.duration)} · ${t.sample_rate} Hz · ${t.codec}`;

  // 用 params 里的值而不是检测值：用户可能点过候选 BPM 或手填过
  $('bpmInput').value = state.sourceBpm;

  const range = $('spmRange');
  range.min = t.defaults.spm_min;
  range.max = t.defaults.spm_max;
  range.value = state.spm;
  $('spmValue').textContent = state.spm;

  // 同理，这三个也都可能被改过，装的是这一首自己记住的值
  $('minRatio').value = state.minRatio;
  $('maxRatio').value = state.maxRatio;
  $('metroGain').value = Math.round(state.metroGain * 100);

  $('seek').value = 0;
  $('seekTime').textContent = '00:00';
  $('seekTotal').textContent = formatTime(t.duration);
  $('exportResult').innerHTML = '';
  $('exportHint').textContent = '';
  updateMetroVolRow();

  // ---- 裁剪区间：换歌后回到「整首」。波形要等 loadWaveform 回来才画得出来。
  $('clipEmpty').hidden = false;
  $('clipEmpty').textContent = '正在读取波形…';
  syncClipUI();
  setClipTip('start');

  // ---- 示波器与拍点
  WAVE.duration = t.duration;
  WAVE.windowSec = (t.defaults && t.defaults.wave_window) || 20;
  $('scopeClock').textContent = '00:00';
  $('scopeTotal').textContent = '/ ' + formatTime(t.duration);
  $('scopeFill').style.width = '0%';
  $('beatStat').textContent = '正在读取…';
  $('verdict').className = 'verdict';
  $('verdict').textContent = '正在读取拍点…';
  $('scopeEmpty').hidden = false;
  $('scopeEmpty').textContent = '正在读取波形…';

  document.querySelectorAll('#mappingGroup button').forEach((b) => {
    b.classList.toggle('active', b.dataset.mapping === state.mapping);
  });

  renderBpmChips();
  renderBeatHead();
  renderMappingHint();
  renderStatus();
  updateWaveView();
  drawWave();

  loadWaveform(t.file_id);
  loadBeats(state.sourceBpm);
}

/* ------------------------------------------------------------ 文件列表 */

function renderFileList() {
  const card = $('listCard');
  const okList = state.files.filter((f) => f.status === 'ok');
  const checked = okList.filter((f) => f.checked);

  card.hidden = state.files.length === 0;
  if (card.hidden) {
    $('batchCard').hidden = true;
    return;
  }

  // 上传中把汇总行让给进度：几首里完成了几首，一眼看到还剩多少
  $('listSum').textContent = state.busy
    ? `正在处理…（${okList.length} / ${state.files.length} 完成）`
    : `${state.files.length} 首 · 已选 ${checked.length} 首 · 可导出 ${okList.length} 首`;
  $('fileList').innerHTML = state.files.map(fileRowHtml).join('');
  $('applyAllBtn').disabled = state.current < 0;

  renderBatchCard();
}

function fileRowHtml(f, i) {
  const on = i === state.current ? ' on' : '';

  // 失败的行没有勾选框、也点不进编辑器 —— 留个等宽空位让各列对齐
  // 排队中 / 分析中：没有勾选框、点不进编辑器，状态列给动画
  if (f.status === 'wait' || f.status === 'loading') {
    const loading = f.status === 'loading';
    return `<div class="file-row" data-i="${i}" data-wait="1">
      <span style="flex:none;width:15px"></span>
      <div class="fr-main">
        <div class="fr-name">${escapeHtml(f.name)}</div>
        <div class="fr-meta">${loading ? '<span class="spin"></span>正在上传并检测节拍…' : '排队等待处理'}</div>
      </div>
      <div class="fr-bpm"><b>—</b></div>
      <div class="fr-state ${loading ? 'loading' : 'wait'}">${loading ? '分析中' : '排队中'}</div>
    </div>`;
  }

  if (f.status !== 'ok') {
    return `<div class="file-row${on}" data-i="${i}" data-fail="1">
      <span style="flex:none;width:15px"></span>
      <div class="fr-main">
        <div class="fr-name">${escapeHtml(f.name)}</div>
        <div class="fr-meta">${escapeHtml(f.error || '处理失败')}</div>
      </div>
      <div class="fr-bpm"><b>—</b></div>
      <div class="fr-state bad">失败</div>
    </div>`;
  }

  const plan = planOf(f.params);
  // 被范围截断是个要紧信息：用户设了 170，实际出 96，得让他一眼看见
  const note = plan.clamped
    ? `${f.params.mapping} · 实际 ${plan.actualSpm.toFixed(0)} spm`
    : f.params.mapping;

  return `<div class="file-row${on}" data-i="${i}">
    <input type="checkbox" data-check="${i}"${f.checked ? ' checked' : ''}>
    <div class="fr-main">
      <div class="fr-name">${escapeHtml(f.name)}</div>
      <div class="fr-meta">${formatTime(f.duration)} · ${note}</div>
    </div>
    <div class="fr-bpm">
      <b>${f.params.sourceBpm.toFixed(1)} BPM</b>
      <span>×${plan.ratio.toFixed(3)}</span>
    </div>
    <div class="fr-state ${plan.clamped ? 'wait' : 'ok'}">${plan.clamped ? '已截断' : '就绪'}</div>
  </div>`;
}

function renderBatchCard() {
  const card = $('batchCard');
  const okList = state.files.filter((f) => f.status === 'ok');
  const checked = okList.filter((f) => f.checked);

  card.hidden = okList.length === 0;
  if (card.hidden) return;

  $('batchSum').textContent = checked.length
    ? `已选 ${checked.length} 首`
    : '还没勾选任何文件';

  const btn = $('batchExportBtn');
  btn.disabled = checked.length === 0;
  const label = btn.querySelector('.btn-label');
  if (label) {
    label.textContent = checked.length > 1 ? `生成选中的 ${checked.length} 首` : '生成选中的文件';
  }
}

function clearList() {
  stopAll();
  state.files = [];
  state.current = -1;
  state.track = null;
  resetPlayer();
  $('panel').hidden = true;
  renderFileList();
  setDropzoneBusy(0);
  toast('列表已清空。磁盘上的缓存文件还在，需要的话去「缓存与数据」里清。', 'info');
}

/* 把当前这首的**可调参数**抄给其它已就绪的歌。
 * 刻意不抄 sourceBpm（每首歌自己的属性）和裁剪区间（各首时长不同）——
 * 抄的是「我要什么」，不是「这首歌是什么」。 */
function applyToAll() {
  const src = state.files[state.current];
  if (!src || src.status !== 'ok') return;
  snapshotCurrent();          // 先把编辑器里的最新值收进 src.params

  const p = src.params;
  let n = 0;
  state.files.forEach((f, i) => {
    if (f.status !== 'ok' || i === state.current) return;
    f.params = {
      ...f.params,
      spm: p.spm,
      mapping: p.mapping,
      minRatio: p.minRatio,
      maxRatio: p.maxRatio,
      metroGain: p.metroGain,
      previewMetro: p.previewMetro,
      exportMetro: p.exportMetro,
      keepPitch: p.keepPitch,
    };
    n += 1;
  });

  renderFileList();
  toast(
    n ? `已把「${p.spm} spm · ${p.mapping}」应用到另外 ${n} 首` : '没有其它已就绪的文件',
    'info'
  );
}

/* ------------------------------------------------------------ 试听节拍声
 *
 * 「精确试听」和导出走服务端混音；「即时试听」用浏览器本地变速，服务端插不进手，
 * 所以 click 在浏览器里用 Web Audio 实时合成。位置换算：原曲第 t 秒在成品里是
 * t ÷ 倍率 秒。调度用「提前量 + 绝对时间」：每 40ms 巡检一次，把接下来该响的
 * click 按 AudioContext 绝对时钟排好，不受 currentTime 刷新粒度影响、不会越走越偏。
 */
/* 提前排程时间窗（秒）。必须远大于主线程可能被卡住的时长：窗口太小的后果不是
 * "响歪"，而是那一声被判定成"已错过"直接丢掉（后台标签页按 1 次/秒巡检会丢大半）。
 * 给到 2 秒：主线程再卡两秒，已排进队的 click 也照响不误。 */
const CLICK_LOOKAHEAD = 2.0;
const CLICK_TICK_MS = 40;       // 巡检间隔（毫秒），实际由自校正计时器驱动
const CLICK_STALE_SEC = 0.15;   // 已经过去超过这么久就不再补响
/* 媒体时钟的"稳定等待"。开播/拖动后那一瞬间 currentTime 不稳，拿它推算位置
 * 会把队里的 click 整体挪错（实测开播第一声早响约 100ms）。此窗口内先不排程。 */
const CLICK_SETTLE_MS = 250;
const CLICK_FREQ = 1400;        // 与后端 config.METRONOME_FREQ 保持一致
const CLICK_ENV_SEC = 0.05;     // click 时长，与后端 build_click_track 的模板一致

let audioCtx = null;
let clickNext = 0;              // 下一个待排程的 click 下标
let clickCache = { ratio: 0, mapping: '', times: null };
let pendingClicks = [];         // 已排程但还没响完的振荡器，停止时要一起掐掉
let pulseTimer = null;          // 播放条上「节拍声闪一下」的计时器
let pulseQueue = [];            // 待闪的时刻（performance.now 毫秒，升序）
let clickBus = null;            // 所有 click 共用的音量节点
let clickTimer = null;          // 自校正巡检计时器
let clickTickAt = 0;            // 下一次巡检的「应有」时刻（performance.now 毫秒）
let clicksSettleUntil = 0;      // 这个时刻之前不排程（媒体时钟还没稳）
let clicksNeedResync = false;   // 时钟稳下来之后要先重新对准一次

function previewMetronomeOn() {
  return $('previewMetro').checked;
}

function ensureAudioCtx() {
  const AC = window.AudioContext || window.webkitAudioContext;
  if (!AC) return null;
  if (!audioCtx) audioCtx = new AC();
  if (audioCtx.state === 'suspended') audioCtx.resume();
  return audioCtx;
}

/* 必须在**用户点击的处理函数里**调用：音频上下文要由用户手势启动，
 * 否则浏览器可能把它留在 suspended 状态 —— 表现就是「振荡器创建了却一声不响」。 */
function unlockAudio() {
  const ctx = ensureAudioCtx();
  if (ctx && ctx.state !== 'running' && typeof ctx.resume === 'function') {
    ctx.resume().catch(() => { /* 极少数情况下被拒绝，后面的 tick 还会再试 */ });
  }
  return ctx;
}

/* click 的位置 = **原曲时间轴**上的拍点，不预先除以倍率。浏览器实际变速与标称值
 * 有细微出入，预先换算的差错会随播放累积（越往后节拍越乱）。位置用 beats 本身，
 * "这一拍何时响"由实测播放线 beatClockFit() 给出，标称倍率只在样本不足时兜底。
 * 只有换算方式（1:1/1:2）变化才需重算。 */
function clickTimes() {
  const c = clickCache;
  if (c.times && c.mapping === state.mapping) return c.times;

  const beats = WAVE.beats || [];
  const times = beats.slice();
  if (state.mapping === '1:2') {
    for (let i = 0; i + 1 < beats.length; i += 1) {
      times.push((beats[i] + beats[i + 1]) / 2);
    }
    times.sort((a, b) => a - b);
  }
  clickCache = { ratio: 1, mapping: state.mapping, times };
  return times;
}

function invalidateClickTimes() {
  clickCache = { ratio: 0, mapping: '', times: null };
}

/* click 包络曲线，逐点复刻服务端 build_click_track 的模板：exp(-70t)·sin(2π·1400·t)。
 * 振荡由 oscillator 提供，这里只给 exp(-70t)。早先的"快冲快衰减"版本整段能量
 * 只有成品一半，听感是「试听里几乎没有节拍声」。 */
let clickCurveBuf = null;
let clickCurveRate = 0;

function clickCurve(ctx) {
  if (clickCurveBuf && clickCurveRate === ctx.sampleRate) return clickCurveBuf;
  const n = Math.max(2, Math.round(ctx.sampleRate * CLICK_ENV_SEC));
  const buf = new Float32Array(n);
  for (let i = 0; i < n; i += 1) buf[i] = Math.exp(-70 * (i / ctx.sampleRate));
  clickCurveBuf = buf;
  clickCurveRate = ctx.sampleRate;
  return buf;
}

/* 所有 click 共用一个音量节点（对应成品的 volume={metronome_gain}）。
 * 排程提前量有 2 秒，若每声各带音量，拖滑块要等 2 秒才听得出变化。 */
function clickOutput(ctx) {
  if (!clickBus || clickBus.context !== ctx) {
    clickBus = ctx.createGain();
    clickBus.gain.value = Math.max(0, state.metroGain);
    clickBus.connect(ctx.destination);
  }
  return clickBus;
}

/* ------------------------------------------------ 音乐通路（对齐的关键） */
/* 音乐走 <audio>、节拍声走 Web Audio 是两条独立输出通路，浏览器不保证谁先到喇叭，
 * 排得再准也会错开。修法：把音乐也接进音频图，两条声音共用同一套时钟与输出缓冲，
 * 偏移从结构上消失。注意 createMediaElementSource 对同一元素只能调一次（会抛错），
 * 故结果缓存；接入失败退回原生输出，至少保证有声音。 */
let musicSource = null;      // 音乐接入音频图的入口（同时也是「已接入」的标记）
let musicBus = null;         // 音乐总音量，保持 1.0，音量仍旧由 liveAudio.volume 决定
let fadeTimer = null;        // 试听渐入的定时器（见 startMusicFade）

function musicAttached() {
  return !!musicSource;
}

function attachMusicToGraph() {
  const ctx = ensureAudioCtx();
  if (!ctx) return false;
  if (musicSource) return true;
  // 上下文没跑起来就接，音乐会被浏览器直接吞掉（静音），先等它 running
  if (ctx.state !== 'running') return false;
  try {
    musicSource = ctx.createMediaElementSource(liveAudio);
    musicBus = ctx.createGain();
    musicBus.gain.value = 1;
    musicSource.connect(musicBus);
    musicBus.connect(ctx.destination);
    return true;
  } catch (e) {
    musicSource = null;      // 退回原生输出
    musicBus = null;
    return false;
  }
}

/* 试听的开头渐入：拉媒体元素自己的音量，不走音频图。
 * 音频图得等上下文 running 才接得上（首次点击常常来不及），靠它的增益自动化
 * 会出现「第一次点没效果」的薛定谔行为；volume 是同步赋值的，一定生效。
 * 就算此时音乐已接进音频图也不冲突 —— volume 作用在图的上游。 */
function startMusicFade(sec) {
  clearMusicFade();
  if (sec <= 0) return;
  const t0 = performance.now();
  liveAudio.volume = 0;
  fadeTimer = setInterval(() => {
    const k = (performance.now() - t0) / (sec * 1000);
    if (k >= 1) { clearMusicFade(); return; }   // 淡完顺手把定时器收掉
    liveAudio.volume = k;
  }, 50);
}

/* 停止/暂停时复位，否则下次会从半路音量接着播，听着像声音突然小了。 */
function clearMusicFade() {
  if (fadeTimer !== null) {
    clearInterval(fadeTimer);
    fadeTimer = null;
  }
  liveAudio.volume = 1;
}

/* 音乐接进音频图后原生输出那条路就不走了，音频上下文被系统挂起时音乐会直接哑掉。
 * 发现挂起就顺手救一次；5 秒内不重复尝试。 */
let ctxRescueAt = 0;

function keepMusicAlive() {
  if (!musicSource || !audioCtx || liveAudio.paused) return;
  if (audioCtx.state !== 'suspended') return;
  const now = performance.now();
  if (now - ctxRescueAt < 5000) return;
  ctxRescueAt = now;
  audioCtx.resume().catch(() => { /* 手势外可能被拒，下次点播放会恢复 */ });
}

function scheduleClick(when) {
  const ctx = ensureAudioCtx();
  if (!ctx) return;
  const osc = ctx.createOscillator();
  const env = ctx.createGain();     // 包络（曲线已归一化，见 clickCurve）
  osc.type = 'sine';
  osc.frequency.value = CLICK_FREQ;
  env.gain.setValueCurveAtTime(clickCurve(ctx), when, CLICK_ENV_SEC);
  osc.connect(env);
  env.connect(clickOutput(ctx));
  osc.start(when);
  osc.stop(when + CLICK_ENV_SEC + 0.002);
  pendingClicks.push(osc);
  pulseBeat(when - ctx.currentTime);
}

/* 停止时把已排在队里但还没响的 click 掐掉，否则停止后还会零星蹦出几声。
 * 闪动队列对应同一批 click，一起清。 */
function stopScheduledClicks() {
  for (const osc of pendingClicks) {
    try { osc.stop(0); } catch (e) { /* 已经响完了，忽略 */ }
  }
  pendingClicks = [];
  clearTimeout(pulseTimer);
  pulseTimer = null;
  pulseQueue.length = 0;
}

/* 让「节拍声正在响」看得见：每响一下闪一次。排程是提前 2 秒成批做的，
 * 必须按「哪一声什么时候响」逐个排队，一次一个计时器的话一批只闪得动第一声。 */
function pulseBeat(delaySec) {
  pulseQueue.push(performance.now() + Math.max(0, delaySec) * 1000);
  pumpPulse();
}

function pumpPulse() {
  const el = $('beatPulse');
  if (!el || el.hidden) {          // 小点收起来了，排的队也没意义
    pulseQueue.length = 0;
    return;
  }
  if (pulseTimer !== null) return;  // 已经有计时器在等下一声

  const now = performance.now();
  if (pulseQueue.length && pulseQueue[0] <= now) {
    while (pulseQueue.length && pulseQueue[0] <= now) pulseQueue.shift();  // 同一刻的只闪一次
    el.classList.remove('on');
    void el.offsetWidth;                        // 强制重排，让动画能重新播放
    el.classList.add('on');
  }
  if (!pulseQueue.length) return;
  const wait = Math.max(0, pulseQueue[0] - performance.now());
  pulseTimer = setTimeout(() => { pulseTimer = null; pumpPulse(); }, wait);
}

/* ---- 实测播放线：把「音乐播到哪一秒」和「ctx 时钟走到哪」直接挂上钩 ---- */
/* 旧做法每周期拿一次 currentTime 读数给当批 click 定位，一次读数的抖动会整批
 * 烙进那批 click，批间互相错开（听感：节拍忽快忽慢）。新做法：持续采样，
 * 最小二乘拟合 mediaT = a·ctxT + b，所有 click 查同一条平滑的线；斜率 a 是
 * 实测变速倍率，顺带吸收浏览器的实际变速偏差，不再随播放累积。 */
const BEAT_FIT_WINDOW = 8;   // 拟合窗口（秒）：太短压不住抖动，太长跟不上变化
const BEAT_FIT_MIN = 25;     // 样本数下限（40ms 一采，约 1 秒），不够先用旧公式过渡
let beatSamples = [];        // [ctx时刻, 媒体位置(原曲秒)]，按时间升序

function beatSamplePush(ctxT, mediaT) {
  beatSamples.push([ctxT, mediaT]);
  while (beatSamples.length && ctxT - beatSamples[0][0] > BEAT_FIT_WINDOW) {
    beatSamples.shift();
  }
}

/* 最小二乘拟合。样本不足返回 ok:false，调用方退回标称倍率换算。 */
function beatClockFit() {
  const n = beatSamples.length;
  if (n < BEAT_FIT_MIN) return { ok: false };
  // 以最后一个样本为原点做中心化，避免 ctxT 数值大时的精度损失
  const x0 = beatSamples[n - 1][0];
  const y0 = beatSamples[n - 1][1];
  let sx = 0, sy = 0, sxx = 0, sxy = 0;
  for (const s of beatSamples) {
    const x = s[0] - x0;
    const y = s[1] - y0;
    sx += x; sy += y; sxx += x * x; sxy += x * y;
  }
  const d = n * sxx - sx * sx;
  if (Math.abs(d) < 1e-9) return { ok: false };
  const a = (n * sxy - sx * sy) / d;          // 实测变速倍率（原曲秒 / 实时秒）
  const c = (sy - a * sx) / n;                // 中心化后的截距
  return { ok: true, a, b: c + y0 - a * x0 }; // 还原：mediaT = a·ctxT + b
}

/* 重新对准：跳过已经播过的拍点。位置都在**原曲时间轴**上，直接比即可。 */
function resyncClicks() {
  const times = clickTimes();
  const pos = liveAudio.currentTime || 0;
  let i = 0;
  while (i < times.length && times[i] < pos) i += 1;
  clickNext = i;
}

/* 开播 / 拖动进度条之后调用：把已排出去的 click 全部作废，晾 CLICK_SETTLE_MS
 * 等媒体时钟稳下来再整体重新对准（不稳的读数会让 click 整体挪错位置）。 */
function armClickSettle() {
  stopScheduledClicks();
  beatSamples = [];                 // 开播/拖动后旧样本描述的是另一段播放，作废
  clicksSettleUntil = performance.now() + CLICK_SETTLE_MS;
  clicksNeedResync = true;
}

/* 倍率 / 换算方式一变，队里的 click 就作废了。提前量有 2 秒：只清不重排要空 2 秒，
 * 只重排不清会让最多 5 声打错位置。两个都做：清掉，立刻按新参数重新对准。 */
function requeueClicks() {
  stopScheduledClicks();
  beatSamples = [];                 // 倍率一变播放速度就变，旧样本的斜率作废
  resyncClicks();
}

function liveClickTick() {
  if (activeMode !== 'live' || !state.track) return;
  if (liveAudio.paused || !previewMetronomeOn()) return;
  const ctx = ensureAudioCtx();
  if (!ctx) return;

  // 音乐必须先接进同一张音频图，否则节拍声和音乐各走各的输出通路，必然错开
  if (!attachMusicToGraph()) return;

  // 开播 / 拖动之后媒体时钟还没稳，先不排程；等它稳下来再重新对准一次
  if (performance.now() < clicksSettleUntil) return;
  if (clicksNeedResync) {
    clicksNeedResync = false;
    resyncClicks();
  }

  // 采一对 (ctx时刻, 媒体位置) 样本，滚动拟合「音乐播到哪 = ctx 何时」这条直线
  beatSamplePush(ctx.currentTime, liveAudio.currentTime);
  const fit = beatClockFit();
  const times = clickTimes();
  const now = ctx.currentTime;

  while (clickNext < times.length) {
    /* 这一拍该什么时候响：优先查实测播放线（所有 click 共用同一条平滑的线）；
     * 样本不够时退回标称倍率换算，只用作开局头一秒的过渡。 */
    const when = fit.ok
      ? (times[clickNext] - fit.b) / fit.a
      : now + (times[clickNext] - liveAudio.currentTime) / (liveAudio.playbackRate || 1);
    const delay = when - now;
    // 过去太久的只能丢掉；刚过去一点点的照响 —— 宁可略晚一点，也不要整声消失
    if (delay < -CLICK_STALE_SEC) { clickNext += 1; continue; }
    if (delay > CLICK_LOOKAHEAD) break;
    scheduleClick(when);
    clickNext += 1;
  }
}

/* 自校正计时器。setInterval 只保证「至少隔这么久」，回调自身耗时和主线程卡顿
 * 会一点点累积成漂移；这里把巡检时刻钉在一条均匀的时间格上，落后了就重新对齐格点。 */
function clickTick() {
  clickTimer = null;
  keepMusicAlive();               // 音乐已接进音频图，上下文别让它哑掉
  liveClickTick();

  clickTickAt += CLICK_TICK_MS;
  const wait = clickTickAt - performance.now();
  if (wait < 0) {
    clickTickAt = performance.now();   // 落后了，把格点重新对到当前时刻
    clickTimer = setTimeout(clickTick, 0);
    return;
  }
  clickTimer = setTimeout(clickTick, wait);
}

function startClickTicker() {
  if (clickTimer !== null) return;
  clickTickAt = performance.now();
  clickTimer = setTimeout(clickTick, CLICK_TICK_MS);
}

startClickTicker();

/* ------------------------------------------------------------ 播放 */

/* 「同一时刻只允许一路出声」的守门人：音乐、节拍对齐、试听叠加的节拍声三者互斥。
 * 任何「开始播放」之前都必须先调一次 —— 它把当前在响的全部掐掉，并推进播放代次，
 * 让在途的异步起播作废（节拍片段要先在后端生成，那几秒里用户可能已经改了主意）。 */
let playToken = 0;

function haltAudio() {
  playToken++;
  stopScheduledClicks();
  try { liveAudio.pause(); } catch (e) { /* noop */ }
  try { metroAudio.pause(); } catch (e) { /* noop */ }
  clearMusicFade();               // 半截渐入要复位，否则音量会卡在中间值
  setMetroBtn(false);
  setActive(null);
}

function resetPlayer() {
  clearTimeout(seekDebounce);
  clearTimeout(bpmDebounce);
  stopScheduledClicks();
  invalidateClickTimes();
  clickNext = 0;
  clicksSettleUntil = 0;
  clicksNeedResync = false;
  setActive(null);
  try { liveAudio.pause(); } catch (e) { /* noop */ }
  try { metroAudio.pause(); } catch (e) { /* noop */ }
  clearMusicFade();
  liveAudio.removeAttribute('src');
  metroAudio.removeAttribute('src');
  delete liveAudio.dataset.src;
  delete metroAudio.dataset.src;
  $('nowPlaying').hidden = true;
  $('seek').value = 0;
  WAVE.peaks = null;
  WAVE.buckets = 0;
  WAVE.beats = [];
  WAVE.time = 0;
  WAVE.viewStart = 0;
  WAVE.pinned = false;
  WAVE.pinStart = 0;
  WAVE.metroStart = 0;
  beatInfo = { beat_count: 0, interval_cv: 0, detected_count: 0 };
  stopWave();
  setMetroBtn(false);
}

function ensureLiveSource() {
  const url = state.track.source_url;
  if (liveAudio.dataset.src !== url) {
    liveAudio.dataset.src = url;
    liveAudio.src = url;
  }
}

function livePitchNote() {
  return $('keepPitch').checked ? '音高不变' : '音高随速度变化';
}

function liveLabel(ratio) {
  return `×${ratio.toFixed(3)} · ${livePitchNote()}`
    + (previewMetronomeOn() ? ' · 含节拍声' : '');
}

/* 只改速率与提示，不打断播放。
 * 浏览器本地变速改 playbackRate 是连续的，所以即时试听能做到零延迟续播。 */
function syncLiveRate() {
  const plan = computePlan();
  liveAudio.playbackRate = plan.ratio;
  setPreservesPitch(liveAudio, $('keepPitch').checked);
  requeueClicks();     // 倍率一变，成品时间轴上的 click 位置整体挪了：清掉旧队列重新排
  showNowPlaying('live', liveLabel(plan.ratio));
}

async function playLive() {
  if (!state.track) return;
  const plan = computePlan();

  haltAudio();                    // 让出播放权：节拍对齐、上一路试听、半截渐入一起停
  unlockAudio();                  // 趁着这次点击，把音频上下文解锁掉

  const start = seekStartSeconds();
  // 只在「从选区起点开始放」时渐入。拖进度条到中间再播是跳着听，不该再淡一次。
  const wantFade = fadeSeconds();
  const fade = wantFade > 0 && Math.abs(start - clipRange().start) < 0.3 ? wantFade : 0;
  if (previewMetronomeOn()) {     // 要叠节拍声，就把音乐接进同一张音频图（对不上的根因就在这）
    attachMusicToGraph();
  }
  if (fade > 0) liveAudio.volume = 0;   // 先压到静音，等真播起来了再往上拉

  ensureLiveSource();
  setPreservesPitch(liveAudio, $('keepPitch').checked);
  liveAudio.playbackRate = plan.ratio;

  if (Math.abs(liveAudio.currentTime - start) > 0.4) {
    try { liveAudio.currentTime = start; } catch (e) { /* 元数据未就绪时忽略 */ }
  }

  try {
    await liveAudio.play();
  } catch (err) {
    clearMusicFade();              // 没播起来，别把音量留在 0
    toast('浏览器阻止了播放，再点一次试试', 'error');
    return;
  }
  if (fade > 0) startMusicFade(fade);

  armClickSettle();               // 等媒体时钟稳下来再对准，从当前位置往后的 click 才对得上
  setActive('live');
  WAVE.pinned = false;            // 整首歌 → 视窗跟着播放滚动
  WAVE.windowSec = waveWindow();
  WAVE.time = liveAudio.currentTime || 0;
  updateWaveView();
  startWave();

  showNowPlaying('live', liveLabel(plan.ratio));
}

/* 裁剪卡片的试听：正在试听就停，否则**从段首**开始放（先把进度条归位）。
 * 必须归位：playLive 是按「进度条那一点」起播的，而进度条播过一次或拖过之后
 * 就停在中间 —— 那时点它会从半截开始放，既听不到这段的开头（用户点它本就是想
 * 听开头接得顺不顺），渐入也会被当成「跳着听」而跳过。想从中间接着听走播放条。
 * 其它方面走同一套播放链路：变速、节拍声开关、播到选区末尾自动停，全部一致。 */
function toggleClipPreview() {
  if (!state.track) return;
  if (activeMode === 'live' && !liveAudio.paused) { stopAll(); return; }
  syncSeekBar(clipRange().start);
  playLive();
}

/* 改步频 / 换算开关时，让正在播放的声音自动跟上 */
function autoUpdatePlaying() {
  if (!state.track || !activeMode) return;
  // 只剩即时试听一种播放源：它走浏览器本地变速，改速率是连续的，零延迟
  if (activeMode === 'live') syncLiveRate();
}

function stopAll() {
  clearTimeout(seekDebounce);
  haltAudio();                    // 停声、推进代次、恢复按钮与播放条状态都在里面
  const t = seekStartSeconds();
  try { liveAudio.currentTime = t; } catch (e) { /* noop */ }
  WAVE.time = t;
  WAVE.pinned = false;
  if (state.track) WAVE.windowSec = waveWindow();
  updateWaveView();
  stopWave();
}

/* ------------------------------------------------------------ 听节拍对齐 */

function setMetroBtn(playing) {
  const btn = $('metroBtn');
  const label = btn.querySelector('.btn-label');
  if (label) label.textContent = playing ? '停止节拍' : '听节拍对齐';
  btn.classList.toggle('primary', playing);
}

function metroLabel(data) {
  return `从 ${formatTime(data.start)} 起 ${data.length.toFixed(0)} 秒 · ${data.bpm.toFixed(1)} BPM`
    + ` · ${data.beats_in_window} 个拍点${data.cached ? ' · 命中缓存' : ''}`;
}

async function toggleMetronome() {
  if (!state.track) return;

  if (!metroAudio.paused) {          // 正在放 → 再点一次就是停
    stopAll();
    return;
  }

  const btn = $('metroBtn');
  const r = clipRange();
  // 对齐试听同样受选区约束：起点夹进区间，长度不超出区间
  const start = Math.min(seekStartSeconds(), Math.max(r.start, r.end - 1));
  const d = state.track.defaults;
  const length = Math.min((d && d.metronome_length) || 15, Math.max(1, r.end - start));
  // 就在发请求之前让出播放权：音乐立刻停，不用等后端把片段生成出来（首次要好几秒）。
  // 记下代次，生成期间用户若点了停止或换了播放源，这次结果就作废，别把用户的操作盖掉。
  haltAudio();
  const token = playToken;
  setBtnLoading(btn, true, '生成中');

  try {
    const res = await fetch('/api/metronome', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        file_id: state.track.file_id,
        source_bpm: state.sourceBpm,
        start,
        length,
      }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `生成失败（HTTP ${res.status}）`);
    if (token !== playToken) return;   // 生成期间用户点了停止/换了播放源 → 本次作废

    if (metroAudio.dataset.src !== data.url) {
      metroAudio.dataset.src = data.url;
      metroAudio.src = data.url;
      metroAudio.load();
    }
    metroAudio.currentTime = 0;
    await metroAudio.play();

    setActive('metro');
    WAVE.pinned = true;
    WAVE.metroStart = data.start;
    WAVE.windowSec = data.length;
    WAVE.pinStart = data.start;
    WAVE.time = data.start;
    updateWaveView();
    startWave();

    showNowPlaying('metro', metroLabel(data));

    toast(
      `已叠加 ${data.beats_in_window} 个拍点的节拍声 —— click 和鼓点重合就是对的`
      + (data.cached ? '（命中缓存）' : ''),
      'info',
    );
  } catch (err) {
    toast(err.message || '节拍预览失败', 'error');
  } finally {
    setBtnLoading(btn, false);
    setMetroBtn(!metroAudio.paused);
  }
}

/* ------------------------------------------------------------ 导出 */

async function doExport() {
  if (!state.track) return;
  const btn = $('exportBtn');
  setBtnLoading(btn, true, '生成中');

  const clip = clipRange();
  const clipping = clipActive();

  try {
    const res = await fetch('/api/export', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        file_id: state.track.file_id,
        source_bpm: state.sourceBpm,
        target_spm: state.spm,
        mapping: state.mapping,
        min_ratio: state.minRatio,
        max_ratio: state.maxRatio,
        metronome: $('exportMetro').checked,
        metronome_gain: state.metroGain,
        // 选了区间就只生成这一段；没选就按整首
        start: clip.start,
        length: clipping ? clip.length : null,
        fade_in: fadeSeconds(),
      }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `生成失败（HTTP ${res.status}）`);
    renderExportResult(data);
    toast(`生成完成，用时 ${data.elapsed} 秒`, 'info');
  } catch (err) {
    toast(err.message || '生成失败', 'error');
  } finally {
    setBtnLoading(btn, false);
  }
}

function renderExportResult(d) {
  const clampNote = d.clamped
    ? `<div class="row"><span>注意</span><b>已被范围截断，实际 ${d.actual_spm} spm</b></div>`
    : '';
  const metroNote = d.metronome
    ? `<div class="row"><span>节拍声</span><b>已叠加 · 音量 ${Math.round((d.metronome_gain || 0) * 100)}%`
      + ` · ${d.clipped ? '这段' : '全曲'} ${d.click_count || 0} 声</b></div>`
    : '';
  const clipNote = d.clipped
    ? `<div class="row"><span>裁剪</span><b>${formatTime(d.clip_start)} → `
      + `${formatTime(d.clip_start + d.clip_length)}（共 ${formatTime(d.clip_length)}）</b></div>`
    : '';
  const fadeNote = d.fade_sec > 0
    ? `<div class="row"><span>开头渐入</span><b>前 ${d.fade_sec.toFixed(1)} 秒从静音淡上来</b></div>`
    : '';
  const inDur = d.clip_length == null ? d.source_duration : d.clip_length;

  $('exportResult').innerHTML = `
    <div class="result">
      <div class="title">已生成 · ${escapeHtml(d.filename)}</div>
      <div class="row"><span>倍率</span><b>×${d.ratio.toFixed(3)}（${d.mapping}）</b></div>
      <div class="row"><span>实际步频</span><b>${d.actual_spm} spm</b></div>
      ${clipNote}
      ${fadeNote}
      <div class="row"><span>时长</span><b>${formatTime(inDur)} → ${formatTime(d.output_duration)}</b></div>
      ${metroNote}
      ${clampNote}
      <audio controls preload="metadata" src="${escapeHtml(d.url)}"></audio>
      <div style="margin-top:14px">
        <a class="btn primary" href="${escapeHtml(d.download_url)}">下载到本地</a>
      </div>
    </div>
  `;
}

/* ------------------------------------------------------------ 批量导出
 *
 * 分两段：① 逐首调 /api/export 生成成品（后端完整渲染，音质好）；
 * ② 按开关打包 zip 或逐个下载。逐首串行 —— 并发只会抢 CPU 拖长总时长，
 * 串行还能让进度条真的动起来。
 */

/* 生成一首成品。参数**全部取自这一首自己的 params**，与编辑器当前状态无关，
 * 导出中途切歌、改参数都不会串味。 */
async function exportOne(entry) {
  const p = entry.params;
  const clipLen = p.clip.end == null ? null : Math.max(0, p.clip.end - p.clip.start);

  const res = await fetch('/api/export', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      file_id: entry.file_id,
      source_bpm: p.sourceBpm,
      target_spm: p.spm,
      mapping: p.mapping,
      min_ratio: p.minRatio,
      max_ratio: p.maxRatio,
      metronome: p.exportMetro,
      metronome_gain: p.metroGain,
      start: p.clip.start,
      length: clipLen,
      fade_in: p.clip.fade ? Number(p.clip.fadeSec) || 0 : 0,
    }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
  return data;
}

async function batchExport() {
  // 上传还没跑完就导出的话，勾选集里少算还没处理完的歌，用户会以为丢了几首
  if (state.busy) {
    toast('还有文件在处理中，等列表里没有「排队中 / 分析中」再导出', 'warn');
    return;
  }
  const picked = state.files.filter((f) => f.status === 'ok' && f.checked);
  if (!picked.length) {
    toast('先勾选要导出的文件', 'error');
    return;
  }

  snapshotCurrent();          // 正在编辑的那首可能刚改过，先落盘再读

  const btn = $('batchExportBtn');
  setBtnLoading(btn, true, '生成中');
  $('batchProgress').hidden = false;
  $('batchResult').innerHTML = '';

  const done = [];
  const failed = [];

  for (let i = 0; i < picked.length; i += 1) {
    const entry = picked[i];
    $('bpFill').style.width = `${Math.round((i / picked.length) * 100)}%`;
    $('bpText').textContent = `正在生成第 ${i + 1} / ${picked.length} 首：${entry.name}`;
    try {
      done.push(await exportOne(entry));
    } catch (err) {
      failed.push({ name: entry.name, error: err.message || '生成失败' });
    }
    // 让出一帧，进度条才会真的走；否则整段同步跑完只闪最后一下
    await new Promise((r) => setTimeout(r, 0));
  }

  $('bpFill').style.width = '100%';
  $('bpText').textContent =
    `完成：成功 ${done.length} 首${failed.length ? `，失败 ${failed.length} 首` : ''}`;

  renderBatchResult(done, failed);
  setBtnLoading(btn, false);

  if (!done.length) return;

  if ($('batchZip').checked) {
    await packageAndDownload(done);
    return;
  }

  // 逐个下载：浏览器对连续下载有节流，必须留间隔，一口气全点会被拦掉
  for (const item of done) {
    const a = document.createElement('a');
    a.href = item.download_url;
    a.download = item.filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    await new Promise((r) => setTimeout(r, 400));
  }
  toast(`已开始下载 ${done.length} 个文件`, 'info');
}

async function packageAndDownload(items) {
  $('bpText').textContent = '正在打包…';
  try {
    const res = await fetch('/api/package', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        items: items.map((d) => ({
          name: d.url.split('/').pop(),   // OUTPUT_DIR 里的实际文件名
          as_name: d.filename,            // 打包后显示的名字（带步频、_beat 后缀）
        })),
        zip_name: `runbeat_${items.length}首`,
      }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `打包失败（HTTP ${res.status}）`);

    // 先落最终文案再触发下载，否则进度条会永远停在「正在打包…」
    const done = `已打包 ${data.count} 首（${formatBytes(data.bytes)}），开始下载`;
    $('bpText').textContent = done;
    location.href = data.download_url;
    toast(done, 'info');
  } catch (err) {
    $('bpText').textContent = `打包失败：${err.message || '未知错误'}`;
    toast(err.message || '打包失败', 'error');
  }
}

function renderBatchResult(done, failed) {
  if (!done.length && !failed.length) return;

  const rows = done
    .map(
      (d) => `<div class="row"><span>${escapeHtml(d.filename)}</span>`
        + `<b>×${d.ratio.toFixed(3)} · ${Math.round(d.actual_spm)} spm`
        + ` · ${formatTime(d.output_duration)}</b></div>`
    )
    .join('');
  const errs = failed
    .map(
      (f) => `<div class="row"><span>${escapeHtml(f.name)}</span>`
        + `<b style="color:var(--danger)">${escapeHtml(f.error)}</b></div>`
    )
    .join('');

  $('batchResult').innerHTML = `
    <div class="result">
      <div class="title">已生成 ${done.length} 首${failed.length ? `，${failed.length} 首失败` : ''}</div>
      ${rows}
      ${errs}
    </div>
  `;
}

/* ------------------------------------------------------------ 事件绑定 */

$('dropzone').addEventListener('click', () => $('fileInput').click());
$('changeFileBtn').addEventListener('click', () => $('fileInput').click());
$('topAddBtn').addEventListener('click', () => $('fileInput').click());

$('fileInput').addEventListener('change', (e) => {
  if (e.target.files && e.target.files.length) handleFiles(e.target.files);
  e.target.value = '';
});

/* ---- 文件列表：整行委托点击。点在勾选框上 = 勾选，点行的其它位置 = 切到那一首编辑。 */
$('fileList').addEventListener('click', (e) => {
  const row = e.target.closest('.file-row');
  if (!row) return;
  const idx = Number(row.dataset.i);
  const entry = state.files[idx];
  if (!entry) return;

  if (e.target.matches('input[type="checkbox"]')) {
    entry.checked = e.target.checked;
    renderFileList();
    return;
  }
  if (row.dataset.fail) {
    toast(entry.error || '这个文件没能处理成功', 'error');
    return;
  }
  if (row.dataset.wait) {
    toast(entry.status === 'loading' ? '这首正在处理，马上就好' : '这首还在排队，前面处理完就轮到它', 'info');
    return;
  }
  switchTo(idx);
});

$('applyAllBtn').addEventListener('click', applyToAll);
$('clearListBtn').addEventListener('click', clearList);
$('batchExportBtn').addEventListener('click', batchExport);

['dragenter', 'dragover'].forEach((ev) => {
  $('dropzone').addEventListener(ev, (e) => {
    e.preventDefault();
    $('dropzone').classList.add('over');
  });
});
['dragleave', 'drop'].forEach((ev) => {
  $('dropzone').addEventListener(ev, (e) => {
    e.preventDefault();
    $('dropzone').classList.remove('over');
  });
});
$('dropzone').addEventListener('drop', (e) => {
  const dropped = e.dataTransfer && e.dataTransfer.files;
  if (dropped && dropped.length) handleFiles(dropped);
});

window.addEventListener('dragover', (e) => e.preventDefault());
window.addEventListener('drop', (e) => e.preventDefault());

$('bpmInput').addEventListener('input', () => {
  const value = parseFloat($('bpmInput').value);
  if (isFinite(value) && value > 20 && value <= 400) {
    state.sourceBpm = value;
    renderBpmChips();
    renderBeatHead();
    renderStatus();
    // 每敲一个数字就重算拍点会把后端打满，等手停下再算
    clearTimeout(bpmDebounce);
    bpmDebounce = setTimeout(() => loadBeats(value), 350);
  }
});

$('spmRange').addEventListener('input', () => {
  state.spm = Number($('spmRange').value);
  $('spmValue').textContent = state.spm;
  renderMappingHint();
  renderStatus();
  autoUpdatePlaying();
});

$('mappingGroup').addEventListener('click', (e) => {
  const btn = e.target.closest('button[data-mapping]');
  if (!btn) return;
  state.mapping = btn.dataset.mapping;
  document.querySelectorAll('#mappingGroup button').forEach((b) => {
    b.classList.toggle('active', b === btn);
  });
  renderMappingHint();
  renderStatus();
  autoUpdatePlaying();
});

function readRatioInputs() {
  const lo = parseFloat($('minRatio').value);
  const hi = parseFloat($('maxRatio').value);
  if (isFinite(lo) && lo > 0) state.minRatio = Math.min(lo, HARD_MAX_RATIO);
  if (isFinite(hi) && hi > 0) state.maxRatio = Math.min(hi, HARD_MAX_RATIO);
  renderStatus();
}

$('minRatio').addEventListener('input', readRatioInputs);
$('maxRatio').addEventListener('input', readRatioInputs);

$('resetRatioBtn').addEventListener('click', () => {
  const d = state.track.defaults;
  state.minRatio = d.min_ratio;
  state.maxRatio = d.max_ratio;
  $('minRatio').value = d.min_ratio;
  $('maxRatio').value = d.max_ratio;
  renderStatus();
});

$('playLiveBtn').addEventListener('click', playLive);
$('stopBtn').addEventListener('click', stopAll);
$('exportBtn').addEventListener('click', doExport);
$('metroBtn').addEventListener('click', toggleMetronome);

/* ---- 裁剪区间：拖手柄 / 直接填时间 */

let clipDrag = null;

const clipCanvas = $('clipCanvas');

clipCanvas.addEventListener('pointerdown', (e) => {
  if (!state.track || !WAVE.peaks) return;
  e.preventDefault();
  const x = clipLocalX(e);
  clipDrag = {
    kind: clipHandleAt(x) || 'new',
    from: clipXToTime(x),
    moved: false,
    pointerId: e.pointerId,
  };
  try { clipCanvas.setPointerCapture(e.pointerId); } catch (err) { /* 忽略 */ }
});

clipCanvas.addEventListener('pointermove', (e) => {
  if (!clipDrag || e.pointerId !== clipDrag.pointerId) return;
  e.preventDefault();
  const t = clipXToTime(clipLocalX(e));
  const r = clipRange();

  if (clipDrag.kind === 'start') {
    clipDrag.moved = true;
    setClip(Math.min(t, r.end - MIN_CLIP_SEC), r.end, false);
  } else if (clipDrag.kind === 'end') {
    clipDrag.moved = true;
    setClip(r.start, Math.max(t, r.start + MIN_CLIP_SEC), false);
  } else {
    // 位移超过 3 像素才算「在选一段」，否则当成误触
    const W = clipCanvas.clientWidth || 1;
    const total = trackDuration() || 1;
    if (Math.abs(t - clipDrag.from) * (W / total) > 3) clipDrag.moved = true;
    if (clipDrag.moved) setClip(Math.min(clipDrag.from, t), Math.max(clipDrag.from, t), false);
  }
});

function endClipDrag(e) {
  if (!clipDrag || e.pointerId !== clipDrag.pointerId) return;
  const drag = clipDrag;
  clipDrag = null;
  try { clipCanvas.releasePointerCapture(e.pointerId); } catch (err) { /* 忽略 */ }

  if (drag.kind === 'new' && !drag.moved) {
    syncClipUI();                       // 只是点了一下，不当作选择
    return;
  }
  const r = clipRange();
  setClip(r.start, r.end, true);        // 松手才吸附到鼓点
  // 拖的是哪一头就从哪一头接着听：拖起点 → 新起点，拖终点 → 终点前 5 秒
  // （拉一个新区间当成从起点开始听）
  repositionAfterClip(drag.kind === 'end' ? 'end' : 'start');
}

clipCanvas.addEventListener('pointerup', endClipDrag);
clipCanvas.addEventListener('pointercancel', endClipDrag);

$('clipStart').addEventListener('change', () => {
  if (!state.track) return;
  const t = parseClipTime($('clipStart').value);
  if (t === null) { syncClipUI(); return; }      // 填的不是时间 → 退回原样
  const r = clipRange();
  setClip(Math.min(t, r.end - MIN_CLIP_SEC), r.end, true);
  repositionAfterClip('start');                  // 起点变了 → 从新起点开始听
});

$('clipEnd').addEventListener('change', () => {
  if (!state.track) return;
  const t = parseClipTime($('clipEnd').value);
  if (t === null) { syncClipUI(); return; }
  const r = clipRange();
  setClip(r.start, Math.max(t, r.start + MIN_CLIP_SEC), true);
  repositionAfterClip('end');                    // 终点变了 → 跳到终点前 5 秒，听收尾
});

$('clipResetBtn').addEventListener('click', () => {
  if (!state.track) return;
  setClip(0, trackDuration(), false);
  repositionAfterClip('start');
});

$('clipFade').addEventListener('change', () => {
  syncFadeUI();
  // 播放中途取消勾选：渐入可能只跑了一半，把音量拉回原值
  if (!$('clipFade').checked) clearMusicFade();
});

$('clipFadeSec').addEventListener('change', () => {
  const v = Number($('clipFadeSec').value);
  if (!Number.isFinite(v) || v <= 0) $('clipFadeSec').value = '1';
  else if (v > FADE_MAX) $('clipFadeSec').value = String(FADE_MAX);
});

$('clipPreviewBtn').addEventListener('click', toggleClipPreview);

$('seek').addEventListener('input', () => {
  if (!state.track) return;
  const t = seekStartSeconds();
  $('seekTime').textContent = formatTime(t);
  WAVE.time = t;
  if (WAVE.source === 'live') {
    try { liveAudio.currentTime = t; } catch (e) { /* noop */ }
    armClickSettle();        // 位置跳了：排出去的队列作废，等媒体时钟稳下来再重新对准
  }
  updateWaveView();
  syncScopeUI();
  drawWave();
});

// 松手后再重新生成，避免拖动过程中把后端打满
$('seek').addEventListener('change', () => {
  clearTimeout(seekDebounce);
  if (WAVE.source === 'metro') {
    // 节拍片段不能就地拖，只能按新位置重新生成
    stopAll();
    seekDebounce = setTimeout(() => toggleMetronome(), 250);
  }
});

$('keepPitch').addEventListener('change', () => {
  setPreservesPitch(liveAudio, $('keepPitch').checked);
  if (activeMode === 'live') syncLiveRate();
});

/* ---- 节拍声开关与音量（试听与导出共用同一个音量，滑块只放一个） */

function updateMetroVolRow() {
  const pct = Math.round(state.metroGain * 100);
  $('metroVolRow').hidden = !($('previewMetro').checked || $('exportMetro').checked);
  $('metroGainText').textContent = pct + '%';
  $('exportMetroHint').textContent = $('exportMetro').checked
    ? `成品会叠一层节拍声（音量 ${pct}%，在上方「试听」里调），下载文件名带 _beat 后缀。`
    : '';
}

$('previewMetro').addEventListener('change', () => {
  updateMetroVolRow();
  syncBeatPulse();                 // 关掉时小点要立刻收起来
  unlockAudio();                   // 勾选本身就是一次点击，顺手把音频上下文解锁掉
  if (previewMetronomeOn()) {
    attachMusicToGraph();          // 播放中途勾上也要接进同一张音频图，否则这一路照样错开
  }
  if (activeMode === 'live') {
    // 开关一动就即时生效：开着就重新对准，关掉就把已排程的 click 掐掉
    if (previewMetronomeOn()) {
      requeueClicks();
    } else {
      stopScheduledClicks();
    }
    showNowPlaying('live', liveLabel(liveAudio.playbackRate || 1));
  }
});

$('exportMetro').addEventListener('change', updateMetroVolRow);

$('metroGain').addEventListener('input', () => {
  state.metroGain = Number($('metroGain').value) / 100;
  // 共用音量节点，改一下立刻生效（排程提前了 2 秒，落到节点上才不用等）
  if (clickBus) clickBus.gain.value = Math.max(0, state.metroGain);
  updateMetroVolRow();
  if (activeMode === 'live' && previewMetronomeOn()) {
    showNowPlaying('live', liveLabel(liveAudio.playbackRate || 1));
  }
});

/* 播放源共用一条进度条：位置统一换算回原曲时间轴，但进度条本身**相对选区**。 */
function syncSeekBar(originalTime) {
  const r = clipRange();
  const t = Math.max(r.start, Math.min(r.end, originalTime || 0));
  const frac = r.length > 0 ? (t - r.start) / r.length : 0;
  $('seek').value = Math.round(frac * 1000);
  $('seekTime').textContent = formatTime(t);
}

liveAudio.addEventListener('timeupdate', () => {
  if (activeMode !== 'live' || !state.track) return;
  syncSeekBar(liveAudio.currentTime);
});

metroAudio.addEventListener('timeupdate', () => {
  if (activeMode !== 'metro' || !state.track) return;
  syncSeekBar(WAVE.metroStart + metroAudio.currentTime);
});

// 放到头了：先掐掉队里还没响的 click（提前量有 2 秒），否则曲子结束后还会零星蹦几声
liveAudio.addEventListener('ended', () => { stopScheduledClicks(); setActive(null); stopWave(); });
metroAudio.addEventListener('ended', () => { setActive(null); stopWave(); setMetroBtn(false); });

metroAudio.addEventListener('error', () => {
  if (activeMode === 'metro') toast('节拍预览加载失败，再点一次试试', 'error');
});

let resizeTimer = null;
window.addEventListener('resize', () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => { updateWaveView(); drawWave(); drawClipCanvas(); }, 120);
});

liveAudio.addEventListener('error', () => {
  if (activeMode === 'live') toast('音频播放出错，试试重新上传', 'error');
});

/* ------------------------------------------------------------ 缓存与数据 */

/* 类别元信息。kind 与后端 store.CLEARABLE 一一对应，顺序也照抄 ——
 * 「有哪些东西能被删」的出处只在后端，这里只负责显示文案。 */
const CACHE_KINDS = [
  { kind: 'previews', label: '试听片段缓存', dir: 'previews', safe: true,
    note: '下次试听时重新渲染' },
  { kind: 'tmp', label: '混音临时文件', dir: 'tmp', safe: true,
    note: '用完即删，通常本来就是空的' },
  { kind: 'analysis', label: '分析文件', dir: 'analysis', safe: true,
    note: '用来算拍点，可从原文件重算' },
  { kind: 'outputs', label: '输出成品', dir: 'outputs', safe: false,
    risk: 'warn', note: '已生成的下载链接会失效' },
  { kind: 'uploads', label: '上传的原文件', dir: 'uploads', safe: false,
    risk: 'bad', note: '删了要重新上传，当前页面那首歌会立即失效' },
];

const CACHE_SAFE = CACHE_KINDS.filter((k) => k.safe).map((k) => k.kind);

let cacheReport = null;                    // 最近一次 /api/storage 的结果
let cachePicked = new Set(CACHE_SAFE);     // 勾中的类别
let cacheBusy = false;

function formatBytes(n) {
  const b = Number(n) || 0;
  if (b < 1024) return `${Math.round(b)} B`;
  if (b < 1048576) return `${(b / 1024).toFixed(1)} KB`;
  if (b < 1073741824) return `${(b / 1048576).toFixed(1)} MB`;
  return `${(b / 1073741824).toFixed(2)} GB`;
}

function cacheRow(kind) {
  const items = (cacheReport && cacheReport.items) || [];
  return items.find((it) => it.kind === kind) || { kind, files: 0, bytes: 0 };
}

/* 当前真正会被删掉的类别：勾了、且有文件（空目录不往请求里塞）。 */
function cachePickedItems() {
  return CACHE_KINDS.filter((m) => cachePicked.has(m.kind) && cacheRow(m.kind).files > 0);
}

async function loadStorage() {
  try {
    const res = await fetch('/api/storage');
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    cacheReport = data;
  } catch (err) {
    cacheReport = null;
    $('cacheTotal').textContent = '读取失败';
    $('cacheSum').textContent = err.message || '读不到 data 目录的状态';
    return;
  }
  renderCachePanel();
}

function renderCachePanel() {
  if (!cacheReport) return;

  $('cacheTotal').textContent = `data/ 合计 ${formatBytes(cacheReport.total_bytes)}`;
  $('cacheSum').textContent = cacheReport.total_files
    ? `data/ 里现有 ${cacheReport.total_files} 个文件，共 ${formatBytes(cacheReport.total_bytes)}`
    : 'data/ 现在是干净的';

  const list = $('cacheList');
  list.innerHTML = '';
  for (const meta of CACHE_KINDS) {
    const it = cacheRow(meta.kind);
    const row = document.createElement('label');
    row.className = 'cache-item' + (meta.safe ? '' : ' risky');

    const box = document.createElement('input');
    box.type = 'checkbox';
    box.dataset.kind = meta.kind;
    // 空目录一律不勾：保证「勾 = 真的会被删」，否则计数对不上、还出现勾着但点不动的怪状态
    box.checked = it.files > 0 && cachePicked.has(meta.kind);
    box.disabled = cacheBusy || it.files === 0;
    box.addEventListener('change', () => {
      if (box.checked) cachePicked.add(meta.kind);
      else cachePicked.delete(meta.kind);
      updateCachePicked();
    });

    const body = document.createElement('div');
    body.className = 'cache-item-body';
    const name = document.createElement('div');
    name.className = 'cache-item-name';
    name.textContent = meta.label;
    const note = document.createElement('div');
    note.className = 'cache-item-note' + (meta.risk ? ' ' + meta.risk : '');
    note.textContent = `${meta.dir} · ${it.files} 个文件 · ${formatBytes(it.bytes)}`
      + (it.files ? ` · ${meta.note}` : '');
    body.append(name, note);

    row.append(box, body);
    list.append(row);
  }

  updateCachePicked();
}

function updateCachePicked() {
  const picked = cachePickedItems();
  const files = picked.reduce((s, m) => s + cacheRow(m.kind).files, 0);
  const bytes = picked.reduce((s, m) => s + cacheRow(m.kind).bytes, 0);

  $('cachePicked').textContent = picked.length
    ? `已选 ${files} 个文件 · ${formatBytes(bytes)}`
    : '还没选任何内容';

  const btn = $('cacheClearBtn');
  btn.disabled = cacheBusy || !picked.length;
  btn.textContent = picked.length ? `清理选中的 ${files} 个文件` : '清理选中的';
}

function toggleCachePanel(force) {
  const panel = $('cachePanel');
  const open = force === undefined ? panel.hidden : !!force;
  panel.hidden = !open;
  $('cacheToggleBtn').querySelector('.btn-label').textContent = open ? '收起' : '清理缓存';
  if (open) {
    cachePicked = new Set(CACHE_SAFE);    // 每次展开都回到「安全」那一档
    loadStorage();
  }
}

/* -------------------------------------------------- 二次确认：删除不可恢复，
 * 走真正的模态，且把将删什么、有多少、影响是什么逐条摆出来。 */

function openCacheModal() {
  const picked = cachePickedItems();
  if (!picked.length || cacheBusy) return;

  let files = 0;
  let bytes = 0;
  const list = $('cacheModalList');
  list.innerHTML = '';
  for (const meta of picked) {
    const it = cacheRow(meta.kind);
    files += it.files;
    bytes += it.bytes;

    const row = document.createElement('div');
    row.className = 'modal-row' + (meta.safe ? '' : ' risky');
    const name = document.createElement('span');
    name.textContent = meta.label + ' ';
    const dir = document.createElement('span');
    dir.className = 'dir';
    dir.textContent = meta.dir;
    name.append(dir);
    const num = document.createElement('span');
    num.className = 'num';
    num.textContent = `${it.files} 个 · ${formatBytes(it.bytes)}`;
    row.append(name, num);
    list.append(row);
  }

  $('cacheModalTitle').textContent = `确认清理这 ${picked.length} 项？`;
  $('cacheModalFree').textContent = `可释放 ${formatBytes(bytes)}`;

  const kinds = picked.map((m) => m.kind);
  const hasTrack = !!state.track;
  const warn = $('cacheModalWarn');
  let warnText = '';
  if (kinds.includes('uploads')) {
    warnText = hasTrack
      ? '勾了「上传的原文件」，当前页面正在编辑的那首歌会失效，页面会自动重置回初始状态。'
      : '勾了「上传的原文件」，之前上传过的文件都会被删掉，下次要用得重新上传。';
  } else if (kinds.includes('outputs')) {
    warnText = '「输出成品」删掉之后，之前生成的下载链接会立即失效。';
  }
  warn.hidden = !warnText;
  if (warnText) {
    warn.innerHTML = '';
    const mark = document.createElement('span');
    mark.className = 'mark';
    mark.textContent = '!';
    const text = document.createElement('span');
    text.textContent = warnText;
    warn.append(mark, text);
  }

  $('cacheModal').hidden = false;
}

function closeCacheModal() {
  $('cacheModal').hidden = true;
}

async function doClearCache() {
  const kinds = cachePickedItems().map((m) => m.kind);
  if (!kinds.length || cacheBusy) return;
  const hadUploads = kinds.includes('uploads');

  cacheBusy = true;
  $('cacheModalOk').disabled = true;
  $('cacheModalOk').textContent = '正在清理…';

  let ok = false;
  let files = 0;
  let bytes = 0;
  try {
    const res = await fetch('/api/cache/clear', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ kinds }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `清理失败（HTTP ${res.status}）`);
    ok = true;
    files = data.files || 0;
    bytes = data.bytes || 0;
  } catch (err) {
    toast(err.message || '清理失败', 'error');
  }

  cacheBusy = false;
  $('cacheModalOk').disabled = false;
  $('cacheModalOk').textContent = '确认清理';
  if (!ok) return;

  closeCacheModal();

  if (kinds.includes('outputs')) {
    // 成品已经没了，页面上那个下载链接点下去只会 404，顺手清掉
    $('exportResult').innerHTML = '';
    $('exportHint').textContent = '';
  }

  toast(
    `已清理 ${files} 个文件，释放 ${formatBytes(bytes)}`
      + (hadUploads && state.track ? '；当前这首歌已失效，页面即将重置' : ''),
    'info',
  );

  // uploads 被清空 + 正编辑着一首歌 = 那首歌彻底作废。整页重载而非缝缝补补，
  // 否则会留下「波形还在、一点播放就 410」的半死不活状态。没加载歌时刷新数字即可。
  if (hadUploads && state.track) {
    setTimeout(() => location.reload(), 1200);
    return;
  }

  await loadStorage();
}

$('cacheToggleBtn').addEventListener('click', () => toggleCachePanel());
$('cacheCancel').addEventListener('click', () => toggleCachePanel(false));
$('cachePickSafe').addEventListener('click', () => { cachePicked = new Set(CACHE_SAFE); renderCachePanel(); });
$('cachePickAll').addEventListener('click', () => { cachePicked = new Set(CACHE_KINDS.map((m) => m.kind)); renderCachePanel(); });
$('cachePickNone').addEventListener('click', () => { cachePicked = new Set(); renderCachePanel(); });
$('cacheClearBtn').addEventListener('click', openCacheModal);
$('cacheModalCancel').addEventListener('click', closeCacheModal);
$('cacheModalOk').addEventListener('click', doClearCache);

// 点遮罩和按 Esc 都能退出确认框 —— 但绝不会「点外面就默认执行」
$('cacheModal').addEventListener('click', (e) => {
  if (e.target === $('cacheModal')) closeCacheModal();
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && !$('cacheModal').hidden) closeCacheModal();
});

/* ------------------------------------------------------------ 启动自检 */

fetch('/api/health')
  .then((r) => r.json())
  .then((d) => {
    if (d.output !== '.mp3') {
      toast('当前 ffmpeg 没有 mp3 编码器，文件将以 m4a 输出', 'info');
    }
  })
  .catch(() => { /* 忽略 */ });

// 顺带把 data/ 的占用拉回来，「缓存与数据」的标题栏一进页面就有数字
loadStorage();

// 没勾「渐进切入」时，时长框一开始就该是灰的
syncFadeUI();

/* 调试出口：把内部状态与渲染函数挂到 window，
   便于本地预览、截图与排查问题（不影响正常使用）。 */
window.__runbeat = {
  state,
  WAVE,
  beatInfo,
  computePlan,
  initUI,
  renderStatus,
  renderBpmChips,
  renderBeatHead,
  renderVerdict,
  renderMappingHint,
  renderExportResult,
  showNowPlaying,
  loadWaveform,
  loadBeats,
  drawWave,
  updateWaveView,
  toggleMetronome,
  playLive,
  stopAll,
  currentOriginalTime,
  doExport,
  clickTimes,
  resyncClicks,
  previewMetronomeOn,
  updateMetroVolRow,
  attachMusicToGraph,
  musicAttached,
  // 裁剪区间（测试脚本要能直接驱动选区）
  clipRange,
  clipActive,
  setClip,
  syncClipUI,
  drawClipCanvas,
  snapToBeat,
  clipAnchorTime,
  repositionAfterClip,
  toggleClipPreview,
  fadeSeconds,
  syncFadeUI,
  previewVolume: () => liveAudio.volume,
  // 缓存与数据（测试脚本要能直接驱动面板，不想只靠点按钮）
  CACHE_KINDS,
  formatBytes,
  loadStorage,
  renderCachePanel,
  toggleCachePanel,
  openCacheModal,
  closeCacheModal,
  doClearCache,
  cacheSnapshot: () => ({
    report: cacheReport,
    picked: [...cachePicked],
    busy: cacheBusy,
  }),
};
