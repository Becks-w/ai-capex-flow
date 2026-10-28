/* AI 订单与债务图谱 — 前端。读取 data/graph.json（或 ?snapshot=YYYY-MM-DD 的存档）。 */
(function () {
  'use strict';
  const KINDS = ['order', 'investment', 'debt', 'lease', 'guarantee', 'vendor_financing'];
  const KIND_ZH = { order: '订单', investment: '投资', debt: '债务', lease: '租赁融资', guarantee: '担保兜底', vendor_financing: '供应商融资' };
  const KIND_DIR = { order: ['下单（采购）', '收到订单'], investment: ['对外投资', '获投'], debt: ['借入（欠谁）', '贷出（谁欠它）'],
    lease: ['租入 / 融资租赁', '出租 / 出资'], guarantee: ['提供担保', '获担保'], vendor_financing: ['提供供应商融资', '获供应商融资'] };
  const STATUS_ZH = { rumored: '传闻', announced: '已宣布', signed: '已签约', completed: '已完成', amended: '已修订', cancelled: '已取消' };
  const TIER_ZH = { confirmed: '一手来源', reported: '媒体核实', candidate: '候选' };
  const SRC_TIER = { 1: 'SEC 文件', 2: '公司公告', 3: '媒体' };
  const INSTR_ZH = { compute_contract: '算力合同', colocation: '托管租赁', chip_purchase: '芯片采购', power: '电力协议', equipment: '设备采购',
    primary: '一级股权', secondary: '二级股权', acquisition: '收购', warrant: '认股权证', convertible: '可转债', jv_equity: '合资股权',
    bond: '债券', term_loan: '定期贷款', ddtl: '延迟提款贷款', revolver: '循环信贷', abs: '资产证券化', gpu_backed_loan: 'GPU 抵押贷款', bridge: '过桥贷款',
    gpu_lease: 'GPU 租赁', site_lease: '场地租赁', lease_guarantee: '租赁担保', credit_support: '信用支持', offtake_guarantee: '承购担保', chip_financing: '芯片融资', other: '' };
  const GROUP_ORDER = ['model_lab', 'hyperscaler', 'neocloud', 'datacenter', 'energy', 'chip', 'equipment', 'financier', 'spv', 'other'];
  const KIND_COLOR = k => getComputedStyle(document.documentElement).getPropertyValue('--k-' + k).trim() || '#999';

  const $ = s => document.querySelector(s);
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const state = { data: null, ents: new Map(), kinds: new Set(KINDS), q: '', group: '', min: 0, period: '', tier: 'counted', topN: 50,
    view: 'graph', sel: null, hover: null, flagEdge: null, snapshot: null, expanded: new Set() };

  // ---------- 格式 ----------
  function fmtUSD(v, html) {
    // 中文计数：万亿 / 亿 / 万 美元。1.6 万亿、5,460 亿、91 亿、5.7 亿、5,000 万
    if (v == null || !(v > 0)) return null;
    let n, u;
    if (v >= 1e12) { n = v / 1e12; u = '万亿'; }
    else if (v >= 1e8) { n = v / 1e8; u = '亿'; }
    else { n = v / 1e4; u = '万'; }
    let s;
    if (u === '万亿') s = n >= 10 ? n.toFixed(1) : n.toFixed(2).replace(/0$/, '');
    else if (n >= 100) s = Math.round(n).toLocaleString('en-US');
    else s = (Math.abs(n - Math.round(n)) < 0.05 ? n.toFixed(0) : n.toFixed(1));
    return html ? `${s}<small>${u}美元</small>` : `${s} ${u}美元`;
  }
  function ago(iso) {
    if (!iso) return '未知';
    const t = new Date(iso.replace(' ', 'T') + (iso.endsWith('Z') || iso.includes('+') ? '' : 'Z')).getTime();
    if (isNaN(t)) return iso;
    const m = Math.round((Date.now() - t) / 60000);
    if (m < 1) return '刚刚'; if (m < 60) return m + ' 分钟前'; const h = Math.round(m / 60); if (h < 48) return h + ' 小时前';
    const d = Math.round(h / 24); if (d < 60) return d + ' 天前'; return iso.slice(0, 10);
  }
  const fmtDate = s => s ? s.slice(0, 10) : '日期未知';
  const entName = id => state.ents.get(id)?.name || id;
  const entGroup = id => state.ents.get(id)?.group || 'other';
  const groupColor = g => state.data.groups[g]?.color || '#6f7f99';
  const groupZh = g => state.data.groups[g]?.zh || g;

  // ---------- 数据加载 ----------
  async function load() {
    const m = /[?&]snapshot=(\d{4}-\d{2}-\d{2})/.exec(location.search);
    state.snapshot = m ? m[1] : null;
    const url = state.snapshot ? `data/archive/${state.snapshot}.json` : `data/graph.json?t=${Date.now()}`;
    const r = await fetch(url, { cache: 'no-store' });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const d = await r.json();
    state.data = d;
    state.ents = new Map(d.entities.map(e => [e.id, e]));
    d.edges.forEach(e => { e.amount_usd = e.amount_usd || null; });
    if (state.snapshot) { const b = $('#snapBanner'); b.hidden = false; b.innerHTML = `<b>存档</b> 这是 ${state.snapshot} 的数据快照，不会自动刷新。 <a href="${location.pathname}">回到最新</a>`; }
    initControls();
    renderAll();
    const h = /^#n-([A-Za-z0-9_]+)/.exec(location.hash);
    if (h && state.ents.has(h[1].toUpperCase())) select(h[1].toUpperCase(), false);
  }

  // ---------- 过滤 ----------
  function counted(e) { return !e.cancelled; }
  function edgesFiltered() {
    const d = state.data; const q = state.q.trim().toLowerCase();
    const since = state.period && !/^\d{4}$/.test(state.period) ? new Date(Date.now() - (+state.period) * 86400000).toISOString().slice(0, 10) : null;
    return d.edges.filter(e => {
      if (!counted(e)) return false;
      if (state.tier === 'confirmed' && e.tier !== 'confirmed') return false;
      if (!state.kinds.has(e.kind)) return false;
      if (state.min && !(e.amount_usd >= state.min)) return false;
      if (since && (!e.announced || e.announced < since)) return false;
      if (/^\d{4}$/.test(state.period) && (e.announced || '').slice(0, 4) !== state.period) return false;
      if (state.group && entGroup(e.from_entity) !== state.group && entGroup(e.to_entity) !== state.group) return false;
      if (q) {
        const hay = [e.from_entity, e.to_entity, entName(e.from_entity), entName(e.to_entity), state.ents.get(e.from_entity)?.name_zh, state.ents.get(e.to_entity)?.name_zh,
          state.ents.get(e.from_entity)?.ticker, state.ents.get(e.to_entity)?.ticker, e.title_zh, e.title_en, e.capacity, e.instrument].join(' ').toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });
  }
  function nodeTotals(edges) {
    const tot = new Map();
    edges.forEach(e => { const v = e.amount_usd || 0; tot.set(e.from_entity, (tot.get(e.from_entity) || 0) + v); tot.set(e.to_entity, (tot.get(e.to_entity) || 0) + v); });
    return tot;
  }
  function graphData() {
    let edges = edgesFiltered();
    const tot = nodeTotals(edges);
    let ids = [...tot.keys()];
    if (state.topN) {
      const ranked = ids.filter(id => !state.ents.get(id)?.pooled).sort((a, b) => (tot.get(b) - tot.get(a)) || a.localeCompare(b)).slice(0, state.topN);
      const keep = new Set(ranked);
      if (state.sel) keep.add(state.sel);
      edges = edges.filter(e => keep.has(e.from_entity) && keep.has(e.to_entity) || (keep.has(e.from_entity) && state.ents.get(e.to_entity)?.pooled) || (keep.has(e.to_entity) && state.ents.get(e.from_entity)?.pooled));
      ids = [...new Set(edges.flatMap(e => [e.from_entity, e.to_entity]))];
    }
    return { edges, ids, tot };
  }

  // ---------- 控件 ----------
  function initControls() {
    const d = state.data;
    $('#kindChips').innerHTML = KINDS.map(k => `<button class="chip" data-k="${k}" aria-pressed="true" style="--c:${KIND_COLOR(k)}"><i class="ln"></i>${KIND_ZH[k]} <span class="n">${d.stats.by_kind[k]?.count ?? 0}</span></button>`).join('');
    $('#kindChips').querySelectorAll('.chip').forEach(b => b.addEventListener('click', () => {
      const k = b.dataset.k; if (state.kinds.has(k)) state.kinds.delete(k); else state.kinds.add(k);
      b.setAttribute('aria-pressed', state.kinds.has(k)); renderAll();
    }));
    const g = $('#fGroup'); GROUP_ORDER.filter(k => d.groups[k]).forEach(k => { const o = document.createElement('option'); o.value = k; o.textContent = groupZh(k); g.appendChild(o); });
    g.addEventListener('change', () => { state.group = g.value; renderAll(); });
    $('#fMin').addEventListener('change', ev => { state.min = +ev.target.value; renderAll(); });
    $('#fPeriod').addEventListener('change', ev => { state.period = ev.target.value; renderAll(); });
    $('#fTier').addEventListener('change', ev => { state.tier = ev.target.value; renderAll(); });
    let qt; $('#q').addEventListener('input', ev => { clearTimeout(qt); qt = setTimeout(() => { state.q = ev.target.value; renderAll(); }, 150); });
    $('#topN').querySelectorAll('.tab').forEach(b => b.addEventListener('click', () => { state.topN = +b.dataset.n; $('#topN').querySelectorAll('.tab').forEach(x => x.setAttribute('aria-selected', x === b)); renderGraph(); }));
    $('#tabGraph').addEventListener('click', () => setView('graph'));
    $('#tabTable').addEventListener('click', () => setView('table'));
    $('#liveBtn').addEventListener('click', () => { const p = $('#livePop'); p.hidden = !p.hidden; $('#liveBtn').setAttribute('aria-expanded', !p.hidden); });
    document.addEventListener('click', ev => { if (!ev.target.closest('.live')) $('#livePop').hidden = true; });
    document.addEventListener('keydown', ev => { if (ev.key === 'Escape') { $('#livePop').hidden = true; if (state.sel) select(null); } });
    $('#legend').innerHTML = KINDS.map(k => `<span><i class="ln" style="--c:${KIND_COLOR(k)}"></i>${KIND_ZH[k]}</span>`).join('') +
      GROUP_ORDER.filter(k => d.groups[k]).map(k => `<span><i class="ring" style="--c:${groupColor(k)}"></i>${groupZh(k)}</span>`).join('') +
      `<span>圆大小 = 往来总额 · 线粗 = 金额 · 虚线 = 金额未披露 · 箭头指向收钱方</span>`;
  }
  function setView(v) { state.view = v; $('#tabGraph').setAttribute('aria-selected', v === 'graph'); $('#tabTable').setAttribute('aria-selected', v === 'table'); $('#stage').hidden = v !== 'graph'; $('#tableWrap').hidden = v !== 'table'; if (v === 'table') renderTable(); }

  // ---------- 顶部 ----------
  function renderTop() {
    const d = state.data; const bk = d.stats.by_kind;
    const other = ['lease', 'guarantee', 'vendor_financing'];
    const tiles = [['order', '订单'], ['investment', '投资'], ['debt', '债务']].map(([k, zh]) => `<div class="kpi" style="--k:${KIND_COLOR(k)}"><div class="k">${zh}</div><div class="v">${fmtUSD(bk[k].usd, true) || '—'}</div><div class="n">${bk[k].count} 笔${bk[k].undisclosed ? ` · 另 ${bk[k].undisclosed} 笔未披露金额` : ''}</div></div>`);
    const total = d.stats.total_usd || 1;
    const segs = KINDS.map(k => ({ k, v: bk[k].usd })).filter(s => s.v > 0);
    tiles.push(`<div class="kpi mix" style="--k:var(--line-2)"><div class="k">交易类型占比</div><div class="bar">${segs.map(s => `<i style="width:${(s.v / total * 100).toFixed(1)}%;background:${KIND_COLOR(s.k)}" title="${KIND_ZH[s.k]} ${fmtUSD(s.v)}"></i>`).join('')}</div><div class="lg">${segs.map(s => `<span><i class="sw" style="background:${KIND_COLOR(s.k)}"></i>${KIND_ZH[s.k]} <b>${(s.v / total * 100).toFixed(0)}%</b></span>`).join('')}</div><div class="n">${d.stats.edges} 笔交易 · ${d.stats.entities} 家公司${d.stats.candidates ? ` · ${d.stats.candidates} 条候选待核实` : ''}</div></div>`);
    $('#kpis').innerHTML = tiles.join('');
    const gen = d.generated_at; const stale = (Date.now() - new Date(gen).getTime()) > 8 * 3600000;
    $('#liveAgo').textContent = state.snapshot ? `存档 ${state.snapshot}` : `更新于 ${ago(gen)}`;
    $('#liveState').textContent = state.snapshot ? '快照' : 'LIVE';
    $('#liveDot').classList.toggle('stale', stale || !!state.snapshot);
    const m = d.monitor || {}; const srcs = Object.values(m.sources || {}).sort((a, b) => (a.tier - b.tier) || a.name.localeCompare(b.name));
    $('#livePop').innerHTML = `<div class="row"><span>页面数据生成</span><b>${ago(gen)}</b></div><div class="row"><span>待抽取文章</span><b>${m.pending_extraction ?? 0}</b></div><div class="row"><span>文章总数</span><b>${Object.values(m.article_counts || {}).reduce((a, b) => a + b, 0)}（已抽取 ${m.article_counts?.extracted || 0}）</b></div>
      <table><thead><tr><th>数据源</th><th>层</th><th>最近运行</th><th>取 / 新</th><th>状态</th></tr></thead><tbody>${srcs.map(s => `<tr><td>${esc(s.name)}</td><td>${s.tier}</td><td>${ago(s.last_run)}</td><td class="num">${s.fetched ?? 0} / ${s.added ?? 0}</td><td class="${s.ok ? 'ok' : 'err'}">${s.ok ? '正常' : esc(s.error || '失败')}</td></tr>`).join('')}</tbody></table>
      <p class="muted" style="margin:8px 0 0">层 1 = SEC EDGAR，层 2 = 公司新闻室 / 通讯社，层 3 = 媒体。每 3 小时运行一次；Mac 休眠时顺延。</p>`;
    $('#notes').textContent = d.notes?.zh || '';
  }

  // ---------- 图 ----------
  let sim = null, zoom = null, gRoot = null, nodeSel = null, linkSel = null, hitSel = null, nodePos = new Map();
  function renderGraph() {
    const { edges, ids, tot } = graphData();
    $('#count').textContent = `${edges.length} 笔交易 · ${ids.length} 家公司`;
    const svg = d3.select('#graph'); svg.selectAll('*').remove();
    const W = Math.max(600, $('#stage').clientWidth - 2), H = Math.max(460, Math.min(720, Math.round(W * 0.62)));
    svg.attr('viewBox', `0 0 ${W} ${H}`).attr('height', H);
    if (sim) sim.stop();
    if (!ids.length) { svg.append('text').attr('x', W / 2).attr('y', H / 2).attr('text-anchor', 'middle').attr('fill', 'var(--muted)').text('当前筛选下没有交易'); return; }
    const maxTot = d3.max(ids, id => tot.get(id)) || 1;
    const rS = d3.scaleSqrt().domain([0, maxTot]).range([5, 30]);
    const wS = d3.scaleSqrt().domain([0, d3.max(edges, e => e.amount_usd || 0) || 1]).range([1, 7]);
    const nodes = ids.map(id => { const p = nodePos.get(id); const e = state.ents.get(id) || {}; return { id, r: rS(tot.get(id) || 0), group: e.group || 'other', pooled: !!e.pooled, x: p ? p.x : W / 2 + (Math.random() - .5) * 200, y: p ? p.y : H / 2 + (Math.random() - .5) * 200 }; });
    const byId = new Map(nodes.map(n => [n.id, n]));
    const pairIdx = new Map();
    const links = edges.map(e => { const k = [e.from_entity, e.to_entity].sort().join('|'); const i = pairIdx.get(k) || 0; pairIdx.set(k, i + 1); return { source: e.from_entity, target: e.to_entity, e, idx: i, key: k }; });
    links.forEach(l => { l.n = pairIdx.get(l.key); });
    // 板块锚点：按环排列，同板块聚在一起
    const groups = GROUP_ORDER.filter(g => nodes.some(n => n.group === g));
    const anchor = new Map(); const R = Math.min(W, H) * 0.34;
    groups.forEach((g, i) => { const a = -Math.PI / 2 + i / groups.length * 2 * Math.PI; anchor.set(g, { x: W / 2 + R * Math.cos(a), y: H / 2 + R * Math.sin(a) * 0.85 }); });
    const defs = svg.append('defs');
    KINDS.forEach(k => defs.append('marker').attr('id', 'arr-' + k).attr('viewBox', '0 -4 8 8').attr('refX', 7).attr('refY', 0).attr('markerWidth', 8).attr('markerHeight', 8).attr('markerUnits', 'userSpaceOnUse').attr('orient', 'auto').append('path').attr('d', 'M0,-4L8,0L0,4').attr('fill', KIND_COLOR(k)));
    gRoot = svg.append('g');
    gRoot.append('g').selectAll('text').data(groups).join('text').attr('class', 'sector').attr('text-anchor', 'middle').attr('x', g => anchor.get(g).x).attr('y', g => anchor.get(g).y - R * 0.42 * (anchor.get(g).y < H / 2 ? 1 : -1.1)).text(g => groupZh(g));
    const gl = gRoot.append('g');
    linkSel = gl.selectAll('.link').data(links).join('path').attr('class', 'link').attr('stroke', d => KIND_COLOR(d.e.kind))
      .attr('stroke-width', d => d.e.amount_usd ? wS(d.e.amount_usd) : 1.2).attr('stroke-dasharray', d => d.e.amount_usd ? null : '3 4')
      .attr('opacity', .55).attr('marker-end', d => `url(#arr-${d.e.kind})`);
    hitSel = gl.selectAll('.linkhit').data(links).join('path').attr('class', 'linkhit')
      .on('mousemove', (ev, d) => showTip(dealTip(d.e), ev)).on('mouseleave', hideTip).on('click', (ev, d) => { ev.stopPropagation(); select(d.e.from_entity, true, d.e.id); });
    nodeSel = gRoot.append('g').selectAll('.node').data(nodes).join('g').attr('class', d => 'node' + (d.pooled ? ' pooled' : ''));
    nodeSel.append('circle').attr('r', d => d.r).attr('stroke', d => groupColor(d.group));
    const short = s => s.length > 22 ? s.slice(0, 21) + '…' : s;
    nodeSel.append('text').attr('class', d => 'lbl' + (d.r < 9 ? ' minor' : '')).attr('text-anchor', 'middle').attr('y', d => d.r + 12).text(d => short(entName(d.id)));
    nodeSel.append('text').attr('class', d => 'amt' + (d.r < 9 ? ' minor' : '')).attr('text-anchor', 'middle').attr('y', d => d.r + 23).text(d => d.r >= 11 ? (fmtUSD(tot.get(d.id)) || '') : '');
    nodeSel.on('mousemove', (ev, d) => { setHover(d.id); showTip(nodeTip(d.id, tot.get(d.id), edges), ev); }).on('mouseleave', () => { setHover(null); hideTip(); })
      .on('click', (ev, d) => { ev.stopPropagation(); select(d.id === state.sel ? null : d.id); })
      .call(d3.drag().on('start', (ev, d) => { if (!ev.active) sim.alphaTarget(.3).restart(); d.fx = d.x; d.fy = d.y; }).on('drag', (ev, d) => { d.fx = ev.x; d.fy = ev.y; }).on('end', (ev, d) => { if (!ev.active) sim.alphaTarget(0); d.fx = null; d.fy = null; }));
    svg.on('click', () => select(null));
    const path = d => {
      const s = d.source, t = d.target; const dx = t.x - s.x, dy = t.y - s.y, len = Math.hypot(dx, dy) || 1;
      const curve = (d.n > 1 ? (d.idx - (d.n - 1) / 2) * 26 : 0) + len * 0.08;
      const nx = -dy / len, ny = dx / len; const cx = (s.x + t.x) / 2 + nx * curve, cy = (s.y + t.y) / 2 + ny * curve;
      const l2 = Math.hypot(t.x - cx, t.y - cy) || 1, l1 = Math.hypot(cx - s.x, cy - s.y) || 1;
      return `M${s.x + (cx - s.x) / l1 * s.r},${s.y + (cy - s.y) / l1 * s.r}Q${cx},${cy} ${t.x - (t.x - cx) / l2 * (t.r + 6)},${t.y - (t.y - cy) / l2 * (t.r + 6)}`;
    };
    sim = d3.forceSimulation(nodes)
      .force('link', d3.forceLink(links).id(d => d.id).distance(d => 70 + d.source.r + d.target.r).strength(.25))
      .force('charge', d3.forceManyBody().strength(-260))
      .force('collide', d3.forceCollide().radius(d => d.r + 16).strength(.9))
      .force('gx', d3.forceX(d => anchor.get(d.group)?.x ?? W / 2).strength(.12))
      .force('gy', d3.forceY(d => anchor.get(d.group)?.y ?? H / 2).strength(.12))
      .alpha(nodePos.size ? .4 : 1)
      .on('tick', () => {
        nodes.forEach(n => { n.x = Math.max(n.r + 4, Math.min(W - n.r - 4, n.x)); n.y = Math.max(n.r + 4, Math.min(H - n.r - 28, n.y)); nodePos.set(n.id, { x: n.x, y: n.y }); });
        linkSel.attr('d', path); hitSel.attr('d', path); nodeSel.attr('transform', d => `translate(${d.x},${d.y})`);
      });
    zoom = d3.zoom().scaleExtent([.4, 4]).on('zoom', ev => gRoot.attr('transform', ev.transform));
    svg.call(zoom).on('dblclick.zoom', null);
    $('#zIn').onclick = () => svg.transition().call(zoom.scaleBy, 1.3);
    $('#zOut').onclick = () => svg.transition().call(zoom.scaleBy, 1 / 1.3);
    $('#zFit').onclick = () => svg.transition().call(zoom.transform, d3.zoomIdentity);
    applyHighlight();
  }
  function neighborhood(id, edges) { const s = new Set([id]); edges.forEach(e => { if (e.from_entity === id) s.add(e.to_entity); if (e.to_entity === id) s.add(e.from_entity); }); return s; }
  function applyHighlight() {
    if (!nodeSel) return;
    const focus = state.hover || state.sel;
    if (!focus) { nodeSel.classed('dim', false).classed('sel', false).classed('near', false); linkSel.classed('dim', false).classed('hi', false); return; }
    const { edges } = graphData(); const nb = neighborhood(focus, edges);
    nodeSel.classed('dim', d => !nb.has(d.id)).classed('sel', d => d.id === state.sel).classed('near', d => nb.has(d.id));
    linkSel.classed('dim', d => d.e.from_entity !== focus && d.e.to_entity !== focus).classed('hi', d => d.e.from_entity === focus || d.e.to_entity === focus);
    linkSel.filter(d => d.e.from_entity === focus || d.e.to_entity === focus).raise();
    nodeSel.filter(d => nb.has(d.id)).raise();
  }
  function setHover(id) { state.hover = id; applyHighlight(); }

  // ---------- 提示 ----------
  const tip = $('#tip');
  function showTip(html, ev) { tip.innerHTML = html; tip.hidden = false; const pad = 14, w = tip.offsetWidth, h = tip.offsetHeight; let x = ev.clientX + pad, y = ev.clientY + pad; if (x + w > innerWidth - 8) x = ev.clientX - w - pad; if (y + h > innerHeight - 8) y = ev.clientY - h - pad; tip.style.left = Math.max(4, x) + 'px'; tip.style.top = Math.max(4, y) + 'px'; }
  function hideTip() { tip.hidden = true; }
  function dealTip(e) { return `<div class="t">${esc(entName(e.from_entity))} → ${esc(entName(e.to_entity))}</div><div><span class="pill kind" style="--c:${KIND_COLOR(e.kind)}">${KIND_ZH[e.kind]}</span> <b>${fmtUSD(e.amount_usd) || '金额未披露'}</b> · ${fmtDate(e.announced)} · ${STATUS_ZH[e.status] || e.status} · ${TIER_ZH[e.tier]}</div><div class="m">${esc(e.title_zh || e.title_en || '')}</div>`; }
  function nodeTip(id, total, edges) { const e = state.ents.get(id); const n = edges.filter(x => x.from_entity === id || x.to_entity === id).length; return `<div class="t">${esc(e?.name || id)}${e?.name_zh && e.name_zh !== e.name ? ' · ' + esc(e.name_zh) : ''}</div><div class="m">${groupZh(e?.group)}${e?.ticker ? ' · ' + e.ticker : ''}</div><div>往来 <b>${fmtUSD(total) || '未披露'}</b> · ${n} 笔 · 点击查看明细</div>`; }

  // ---------- 选择 / 右侧面板 ----------
  function select(id, scroll = true, flagEdge = null) {
    state.sel = id; state.flagEdge = flagEdge; state.expanded.clear();
    history.replaceState(null, '', id ? '#n-' + id : location.pathname + location.search);
    applyHighlight(); renderSide();
    if (id && scroll && innerWidth <= 1080) $('#side').scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
  function renderSide() {
    const side = $('#side');
    if (!state.sel) { side.classList.remove('entity'); renderRanking(side); return; }
    side.classList.add('entity');
    const id = state.sel, e = state.ents.get(id) || { id, name: id, group: 'other' };
    const all = state.data.edges.filter(x => x.from_entity === id || x.to_entity === id);
    const live = all.filter(counted);
    const inflow = d3.sum(live.filter(x => x.to_entity === id), x => x.amount_usd || 0), outflow = d3.sum(live.filter(x => x.from_entity === id), x => x.amount_usd || 0);
    let html = `<div class="ehead"><div><span class="pill grp" style="--c:${groupColor(e.group)}">${groupZh(e.group)}</span><h2>${esc(e.name)}${e.name_zh && e.name_zh !== e.name ? `<span class="muted" style="font-size:13px;font-weight:500">${esc(e.name_zh)}</span>` : ''}</h2><div class="meta">${e.ticker ? `<span>${e.ticker}</span>` : ''}${e.country ? `<span>${e.country}</span>` : ''}${e.pooled ? '<span>汇总对手方，不参与排行</span>' : ''}</div></div><button class="close" id="closeSide" aria-label="关闭">✕</button></div>`;
    html += `<div class="stats3"><div class="stat" style="--c:var(--k-investment)"><div class="k">资金流入</div><div class="v">${fmtUSD(inflow) || '—'}</div></div><div class="stat" style="--c:var(--k-debt)"><div class="k">资金流出 / 承诺</div><div class="v">${fmtUSD(outflow) || '—'}</div></div><div class="stat"><div class="k">交易</div><div class="v">${live.length}</div></div></div>`;
    const sections = [];
    KINDS.forEach(k => {
      [['out', 0], ['in', 1]].forEach(([dir, di]) => {
        const rows = all.filter(x => x.kind === k && (dir === 'out' ? x.from_entity === id : x.to_entity === id)).sort((a, b) => (b.amount_usd || 0) - (a.amount_usd || 0) || (b.announced || '').localeCompare(a.announced || ''));
        if (!rows.length) return;
        const key = k + dir; const total = d3.sum(rows.filter(counted), x => x.amount_usd || 0);
        const show = state.expanded.has(key) ? rows : rows.slice(0, 3);
        sections.push(`<div class="sec"><h3><i class="ln" style="--c:${KIND_COLOR(k)}"></i>${KIND_DIR[k][di]}<span class="t">${fmtUSD(total) || '未披露'}</span></h3>${show.map(x => dealCard(x, dir === 'out' ? x.to_entity : x.from_entity)).join('')}${rows.length > show.length ? `<button class="more" data-x="${key}">显示全部 ${rows.length} 笔</button>` : ''}</div>`);
      });
    });
    const snaps = (state.data.snapshots || []).filter(s => s.entity_id === id);
    if (snaps.length) sections.push(`<div class="sec"><h3>披露数据（快照）</h3>${snaps.map(s => `<div class="snap"><span>${esc({ capex: '资本开支', rpo: 'RPO', backlog: '积压订单', total_debt: '总债务', compute_commitments: '算力承诺' }[s.metric] || s.metric)} <span class="muted">${esc(s.period || '')}</span><br><span class="muted">${esc(s.summary_zh || '')}</span></span><span class="v">${fmtUSD(s.value_usd) || esc(s.value_text || '')}</span></div>`).join('')}</div>`);
    html += sections.join('') || '<p class="note">当前没有已核实的交易。</p>';
    side.innerHTML = html;
    $('#closeSide').addEventListener('click', () => select(null));
    side.querySelectorAll('.more').forEach(b => b.addEventListener('click', () => { state.expanded.add(b.dataset.x); renderSide(); }));
    side.querySelectorAll('.ent').forEach(b => b.addEventListener('click', () => select(b.dataset.id)));
    if (state.flagEdge != null) { const el = side.querySelector(`[data-edge="${state.flagEdge}"]`); if (el) { el.classList.add('flag'); el.scrollIntoView({ block: 'center' }); } }
    side.scrollTop = 0;
  }
  function dealCard(x, cp) {
    const srcs = (x.sources || []).slice(0, 4).map(s => `<a class="src t${s.tier}" href="${esc(s.url)}" target="_blank" rel="noopener" title="${esc(s.title || s.url)}">${esc(s.publisher || SRC_TIER[s.tier])}${s.tier <= 2 ? ' ✓' : ''}</a>`).join('');
    const meta = [fmtDate(x.announced), `<span class="pill st ${x.status}">${STATUS_ZH[x.status] || x.status}</span>`, `<span class="pill t${x.tier === 'confirmed' ? 1 : x.tier === 'reported' ? 2 : 3}">${TIER_ZH[x.tier]}</span>`];
    if (x.instrument && INSTR_ZH[x.instrument]) meta.push(INSTR_ZH[x.instrument]);
    if (x.term) meta.push(esc(x.term)); if (x.capacity) meta.push(esc(x.capacity));
    if (x.cancelled) meta.push('<span class="pill st cancelled">不计入总额</span>');
    return `<div class="deal" data-edge="${x.id}"><div class="l1"><span class="pill kind" style="--c:${KIND_COLOR(x.kind)}">${KIND_ZH[x.kind]}</span><button class="ent" data-id="${esc(cp)}">${esc(entName(cp))}</button><span class="amt${x.amount_usd ? '' : ' unk'}">${fmtUSD(x.amount_usd) || '金额未披露'}</span></div><div class="t">${esc(x.title_zh || x.title_en || '')}</div><div class="m">${meta.join('<span>·</span>')}</div>${x.summary_zh ? `<details><summary>详情</summary>${esc(x.summary_zh)}${x.amount_text ? `<div class="muted">原文金额：${esc(x.amount_text)}</div>` : ''}</details>` : ''}<div class="srcs">${srcs}</div></div>`;
  }
  function renderRanking(side) {
    const edges = edgesFiltered(); const tot = nodeTotals(edges);
    const byKind = new Map();
    edges.forEach(e => { const v = e.amount_usd || 0; [e.from_entity, e.to_entity].forEach(id => { if (!byKind.has(id)) byKind.set(id, {}); byKind.get(id)[e.kind] = (byKind.get(id)[e.kind] || 0) + v; }); });
    const ranked = [...tot.keys()].filter(id => !state.ents.get(id)?.pooled).sort((a, b) => tot.get(b) - tot.get(a)).slice(0, 15);
    side.innerHTML = `<h2>最大敞口</h2><p class="note">点一家公司看它欠谁、谁欠它、所有订单与投资。条 = 交易类型构成。汇总对手方（债券市场、未具名银团）不参与排行。</p><div class="rank">${ranked.map((id, i) => { const mix = byKind.get(id) || {}; const t = tot.get(id) || 1; return `<button class="row" data-id="${esc(id)}"><span class="i">${i + 1}</span><span><span class="nm">${esc(entName(id))}</span><span class="mix">${KINDS.filter(k => mix[k]).map(k => `<i style="width:${(mix[k] / t * 100).toFixed(1)}%;background:${KIND_COLOR(k)}"></i>`).join('')}</span></span><span class="v">${fmtUSD(tot.get(id)) || '—'}</span></button>`; }).join('') || '<p class="note">没有数据</p>'}</div>`;
    side.querySelectorAll('.row').forEach(b => b.addEventListener('click', () => select(b.dataset.id)));
  }

  // ---------- 表 ----------
  function renderTable() {
    const edges = edgesFiltered().sort((a, b) => (b.amount_usd || 0) - (a.amount_usd || 0));
    $('#table tbody').innerHTML = edges.map(e => `<tr class="rowc" data-from="${esc(e.from_entity)}" data-edge="${e.id}"><td><span class="pill kind" style="--c:${KIND_COLOR(e.kind)}">${KIND_ZH[e.kind]}</span></td><td>${esc(entName(e.from_entity))}</td><td>${esc(entName(e.to_entity))}</td><td class="n r">${fmtUSD(e.amount_usd) || '<span class="muted" style="font-weight:400">未披露</span>'}</td><td>${fmtDate(e.announced)}</td><td><span class="pill st ${e.status}">${STATUS_ZH[e.status] || e.status}</span></td><td>${TIER_ZH[e.tier]}</td><td>${esc(e.title_zh || e.title_en || '')}</td><td class="r num">${(e.sources || []).length}</td></tr>`).join('');
    $('#table tbody').querySelectorAll('tr').forEach(tr => tr.addEventListener('click', () => select(tr.dataset.from, true, +tr.dataset.edge)));
  }

  // ---------- 动态流 / 候选 ----------
  function renderFeeds() {
    const d = state.data;
    const st = a => a.status === 'extracted' ? '<span class="st x">已入账</span>' : a.status === 'skipped' ? '<span class="st">无新事件</span>' : a.status === 'new' ? '<span class="st">待抽取</span>' : '';
    $('#newsList').innerHTML = (d.news || []).map(a => `<li><span class="d" title="${esc(a.published_at || '')}">${esc(fmtDate(a.published_at))}</span><span><a href="${esc(a.url)}" target="_blank" rel="noopener">${esc(a.title)}</a><span class="p">${esc(a.publisher || a.source_name || '')}</span></span>${st(a)}</li>`).join('') || '<li class="feed__empty">还没有新闻</li>';
    $('#filingList').innerHTML = (d.filings || []).map(a => `<li><span class="d">${esc(fmtDate(a.published_at))}</span><span><a href="${esc(a.url)}" target="_blank" rel="noopener">${esc(a.title)}</a></span>${st(a)}</li>`).join('') || '<li class="feed__empty">还没有申报文件</li>';
    $('#newsCount').textContent = (d.news || []).length ? `· 最近 ${(d.news || []).length} 条命中` : '';
    $('#filingCount').textContent = (d.filings || []).length ? `· 最近 ${(d.filings || []).length} 份` : '';
    const c = d.candidates || [];
    $('#cands').innerHTML = `<details><summary>待核实候选 ${c.length} 条 <span class="muted" style="font-weight:400">— 机器从标题或低置信来源抽出，未计入总额；等一手来源或正文确认后自动升级</span></summary><div class="list">${c.map(x => dealCard(x, x.to_entity).replace('<button class="ent"', `<span class="muted">${esc(entName(x.from_entity))} →</span> <button class="ent"`)).join('') || '<p class="note">没有候选</p>'}</div></details>`;
    $('#cands').querySelectorAll('.ent').forEach(b => b.addEventListener('click', () => select(b.dataset.id)));
  }

  function renderAll() { renderTop(); renderGraph(); if (state.view === 'table') renderTable(); renderSide(); renderFeeds(); }
  let rt; new ResizeObserver(() => { if (!state.data) return; clearTimeout(rt); rt = setTimeout(renderGraph, 200); }).observe($('#stage'));
  load().catch(err => { $('#kpis').innerHTML = `<div class="kpi"><div class="k">数据加载失败</div><div class="n">${esc(err.message)}</div></div>`; });
})();
