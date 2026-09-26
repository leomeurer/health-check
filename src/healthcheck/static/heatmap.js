/* Gráfico mensal de SLA do painel (Apache ECharts).
 *
 * Arquivo separado do HTML para o painel funcionar com uma Content-Security-
 * Policy sem 'unsafe-inline' em script-src. Os dados vêm do bloco
 * <script type="application/json" id="sla-heatmap-data"> gerado pelo
 * report.py (não executável; o tojson escapa <, > e &).
 */
(function () {
  var el = document.getElementById('sla-heatmap');
  if (!window.echarts) return;  // fica a mensagem de fallback; o resto do painel é HTML estático
  el.innerHTML = '';

  var H = JSON.parse(document.getElementById('sla-heatmap-data').textContent);
  var STATES = {
    met:            { i: 0, label: 'Meta cumprida',        color: '#16a34a', text: '#ffffff' },
    missed:         { i: 1, label: 'Abaixo da meta',       color: '#dc2626', text: '#ffffff' },
    not_assessable: { i: 2, label: 'Não apurável (cobertura baixa)', color: '#e5e7eb', text: '#374151' },
    blocked:        { i: 3, label: 'Bloqueio Cloudflare',  color: '#9ca3af', text: '#ffffff' },
    measured:       { i: 4, label: 'Medido (sem meta)',    color: '#93c5fd', text: '#1e3a8a' }
  };
  var ORDER = ['met', 'missed', 'not_assessable', 'blocked'].concat(H.target_pct === null ? ['measured'] : []);

  // Legenda em HTML (quebra linha no celular); o visualMap do gráfico fica
  // oculto, mas usa as mesmas cores desta tabela.
  document.getElementById('sla-heatmap-legend').innerHTML = ORDER.map(function (k) {
    return '<span><i style="background:' + STATES[k].color + '"></i>' + STATES[k].label + '</span>';
  }).join('');

  function pct(v) {
    if (v === null) return '-';
    return v >= 99.995 ? '100%' : v.toLocaleString('pt-BR', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + '%';
  }

  // No celular as 12 colunas não cabem com o rótulo legível: mostra só os
  // últimos meses até o corrente (antes disso, em geral, nem há dados).
  var NARROW_PX = 700, NARROW_MONTHS = 3;
  var note = document.getElementById('sla-heatmap-note');
  var chart = echarts.init(el, null, { renderer: 'svg' });
  var lastMode = null;

  // --- Simulação: preenche o ano com valores fictícios, só para visualizar.
  // Determinística (mesma semente sempre), para o print não mudar a cada clique.
  var simulated = false, SIM = null;
  function mulberry32(a) {
    return function () {
      a |= 0; a = a + 0x6D2B79F5 | 0;
      var t = Math.imul(a ^ a >>> 15, 1 | a);
      t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t;
      return ((t ^ t >>> 14) >>> 0) / 4294967296;
    };
  }
  function simulatedCells() {
    var rnd = mulberry32(H.year), cells = [], target = H.target_pct === null ? 98 : H.target_pct;
    H.systems.forEach(function (_, y) {
      for (var x = 0; x < 12; x++) {
        var r = rnd(), state, up, cov;
        if (r < 0.04)      { state = 'blocked'; }
        else if (r < 0.10) { state = 'not_assessable'; up = 99 + rnd();          cov = 55 + rnd() * 39; }
        else if (r < 0.19) { state = 'missed';         up = target - 0.3 - rnd() * 3; cov = 98 + rnd() * 2; }
        else               { state = 'met';            up = target + 0.1 + rnd() * (100 - target - 0.1); cov = 97 + rnd() * 3; }
        if (H.target_pct === null && state !== 'blocked' && state !== 'not_assessable') state = 'measured';
        var rounds = 43200, measured = state === 'blocked' ? 0 : Math.round(rounds * cov / 100);
        cells.push({
          x: x, y: y, state: state,
          uptime: state === 'blocked' ? null : Math.min(100, up),
          coverage: state === 'blocked' ? 0 : cov,
          measured: measured,
          failed: state === 'blocked' ? 0 : Math.round(measured * (1 - Math.min(100, up) / 100)),
          blocked: state === 'blocked' ? rounds : 0
        });
      }
    });
    return cells;
  }
  function cells() { return simulated ? (SIM || (SIM = simulatedCells())) : H.cells; }

  var card = document.getElementById('sla-heatmap-card');
  var btn = document.getElementById('sla-heatmap-sim');
  var banner = document.getElementById('sla-heatmap-banner');
  var title = document.getElementById('sla-heatmap-title');
  var realTitle = title.textContent;
  btn.hidden = false;
  btn.addEventListener('click', function () {
    simulated = !simulated;
    btn.setAttribute('aria-pressed', String(simulated));
    btn.textContent = simulated ? 'Voltar aos dados reais' : 'Simular ano preenchido';
    banner.hidden = !simulated;
    card.classList.toggle('is-sim', simulated);
    title.textContent = simulated ? realTitle + ' (SIMULAÇÃO)' : realTitle;
    render(true);
  });

  function render(force) {
    var narrow = el.clientWidth < NARROW_PX;
    if (narrow === lastMode && !force) { chart.resize(); return; }
    lastMode = narrow;
    var first = narrow ? Math.max(0, H.current_month - NARROW_MONTHS + 1) : 0;
    var last = narrow ? H.current_month : 11;
    var months = H.months.slice(first, last + 1);
    var data = [];
    cells().forEach(function (c, idx) {
      if (c.x < first || c.x > last) return;
      var st = STATES[c.state];
      data.push({ value: [c.x - first, c.y, st.i, idx], label: { color: st.text } });
    });
    note.hidden = !narrow;
    note.textContent = narrow ? 'Tela estreita: mostrando ' + months[0] + '–' + months[months.length - 1] + '. Veja o ano inteiro numa tela maior.' : '';
    drawChart(months, first, data, narrow);
  }

  function drawChart(months, first, data, narrow) {
  chart.setOption({
    textStyle: { fontFamily: '-apple-system, "Segoe UI", Roboto, Arial, sans-serif' },
    grid: { left: 8, right: 16, top: 8, bottom: 8, containLabel: true },
    xAxis: {
      type: 'category', data: months, position: 'top',
      axisLine: { show: false }, axisTick: { show: false },
      axisLabel: {
        color: '#6b7280', fontSize: 11, interval: 0,
        formatter: function (m, i) { return i + first === H.current_month ? '{cur|' + m + '}' : m; },
        rich: { cur: { color: '#1f2430', fontWeight: 700, fontSize: 11 } }
      }
    },
    yAxis: {
      type: 'category', data: H.systems, inverse: true,
      axisLine: { show: false }, axisTick: { show: false },
      axisLabel: { color: '#1f2430', fontSize: narrow ? 11 : 12 }
    },
    visualMap: {
      type: 'piecewise', dimension: 2, show: false,
      pieces: ORDER.map(function (k) { return { value: STATES[k].i, label: STATES[k].label, color: STATES[k].color }; })
    },
    tooltip: {
      backgroundColor: '#ffffff', borderColor: '#e2e4e9', textStyle: { color: '#1f2430', fontSize: 12.5 },
      formatter: function (p) {
        var c = cells()[p.data.value[3]];
        var st = STATES[c.state];
        var linhas = [
          simulated ? '<strong style="color:#b45309">SIMULAÇÃO — dado fictício</strong>' : null,
          '<strong>' + echarts.format.encodeHTML(H.system_names[c.y]) + '</strong>',
          H.months[c.x] + '/' + H.year + ' · ' + st.label
        ];
        if (c.state !== 'blocked') {
          linhas.push('Uptime nas rodadas medidas: <strong>' + pct(c.uptime) + '</strong>');
          linhas.push('Cobertura: ' + pct(c.coverage) + ' · ' + c.measured + ' rodada(s) medida(s), ' + c.failed + ' com falha');
        }
        if (c.blocked) linhas.push(c.blocked + ' rodada(s) sem medição (bloqueio Cloudflare)');
        return linhas.filter(Boolean).join('<br>');
      }
    },
    series: [{
      type: 'heatmap', data: data,
      label: {
        show: true, fontSize: narrow ? 10 : 11, fontWeight: 600,
        formatter: function (p) {
          var c = cells()[p.data.value[3]];
          if (c.state === 'blocked') return 'bloq.';
          return pct(c.uptime) + (c.state === 'not_assessable' ? '*' : '');
        }
      },
      itemStyle: { borderColor: '#ffffff', borderWidth: 2, borderRadius: 3 },
      emphasis: { itemStyle: { borderColor: '#1f2430', borderWidth: 1 } }
    }]
  }, true);
  chart.resize();
  }

  render();
  var t;
  window.addEventListener('resize', function () { clearTimeout(t); t = setTimeout(render, 90); });
})();
