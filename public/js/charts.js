// charts.js – Dynamic Chart.js dashboards for Hyperliquid Pro

/* global Chart */

// =======================
// Utility Functions
// =======================
function fetchJSON(url) {
  return fetch(url).then(r => {
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return r.json();
  });
}

function randomColor(alpha = 0.6) {
  const r = Math.floor(Math.random() * 255);
  const g = Math.floor(Math.random() * 255);
  const b = Math.floor(Math.random() * 255);
  return `rgba(${r},${g},${b},${alpha})`;
}

function createChart(ctx, type, data, options = {}) {
  if (!ctx) return null;
  
  // Apply high-tech styling to all charts
  const enhancedOptions = {
    responsive: true,
    maintainAspectRatio: false,
    animation: {
      duration: 800,
      easing: 'easeInOutQuart'
    },
    plugins: {
      legend: {
        display: true,
        position: 'top',
        labels: {
          color: '#f8fafc',
          font: {
            family: "'Rajdhani', sans-serif",
            size: 12,
            weight: '500'
          },
          padding: 15,
          usePointStyle: true
        }
      },
      tooltip: {
        backgroundColor: 'rgba(15, 23, 42, 0.95)',
        titleColor: '#00ffff',
        bodyColor: '#f8fafc',
        borderColor: '#00ffff',
        borderWidth: 1,
        cornerRadius: 8,
        titleFont: {
          family: "'Rajdhani', sans-serif",
          size: 14,
          weight: 'bold'
        },
        bodyFont: {
          family: "'Rajdhani', sans-serif",
          size: 12
        },
        callbacks: {
          title: function(context) {
            if (context[0] && context[0].label) {
              return context[0].label;
            }
            return '';
          },
          label: function(context) {
            const label = context.dataset.label || '';
            const value = context.parsed.y;
            if (value === null || value === undefined) return '';
            
            if (label.toLowerCase().includes('pnl') || label.toLowerCase().includes('profit')) {
              return `${label}: $${value.toFixed(2)}`;
            } else if (label.toLowerCase().includes('%') || label.toLowerCase().includes('rate')) {
              return `${label}: ${value.toFixed(1)}%`;
            } else {
              return `${label}: ${value.toFixed(2)}`;
            }
          }
        }
      }
    },
    scales: {
      x: {
        ticks: {
          color: '#94a3b8',
          font: {
            family: "'Rajdhani', sans-serif",
            size: 11
          }
        },
        grid: {
          color: 'rgba(51, 65, 85, 0.3)'
        }
      },
      y: {
        ticks: {
          color: '#94a3b8',
          font: {
            family: "'Rajdhani', sans-serif",
            size: 11
          }
        },
        grid: {
          color: 'rgba(51, 65, 85, 0.3)'
        }
      }
    },
    ...options
  };

  try {
    return new Chart(ctx, { type, data, options: enhancedOptions });
  } catch (error) {
    console.error('❌ Error creating chart:', error);
    return null;
  }
}

// =======================
// Dashboard Charts
// =======================
async function initDashboardCharts() {
  try {
    const [positions, pnlData] = await Promise.all([
      fetchJSON('/api/positions'),
      fetchJSON('/api/realized-pnl?days=14')
    ]);

    // Allocation Pie
    const allocCtx = document.getElementById('dashboard-allocation-chart');
    if (allocCtx && positions.length) {
      const labels = positions.map(p => p.symbol);
      const data = positions.map(p => Math.abs(p.positionValue || p.size * p.currentPrice));
      const bg = labels.map(() => randomColor());
      createChart(allocCtx, 'doughnut', {
        labels,
        datasets: [{ data, backgroundColor: bg }]
      }, {
        plugins: { legend: { position: 'bottom' } }
      });
    }

    // Daily P&L Bar
    const pnlCtx = document.getElementById('dashboard-pnl-chart');
    if (pnlCtx && pnlData.dailyBreakdown) {
      const dates = Object.keys(pnlData.dailyBreakdown).sort();
      const pnlVals = dates.map(d => pnlData.dailyBreakdown[d]);
      createChart(pnlCtx, 'bar', {
        labels: dates,
        datasets: [{
          label: 'PnL ($)',
          data: pnlVals,
          backgroundColor: pnlVals.map(v => v >= 0 ? 'rgba(34,197,94,0.6)' : 'rgba(239,68,68,0.6)')
        }]
      }, {
        scales: { y: { beginAtZero: true } }
      });
    }
  } catch (err) {
    console.error('Dashboard chart error', err);
  }
}

// =======================
// Positions Charts
// =======================
async function initPositionsCharts() {
  try {
    const positions = await fetchJSON('/api/positions');
    if (!positions.length) return;

    const distCtx = document.getElementById('positions-distribution-chart');
    if (distCtx) {
      const labels = positions.map(p => p.symbol);
      const data = positions.map(p => Math.abs(p.positionValue || p.size * p.currentPrice));
      createChart(distCtx, 'pie', {
        labels,
        datasets: [{ data, backgroundColor: labels.map(() => randomColor()) }]
      });
    }

    const perfCtx = document.getElementById('positions-performance-chart');
    if (perfCtx) {
      const labels = positions.map(p => p.symbol);
      const data = positions.map(p => p.pnlPercentage.toFixed(2));
      createChart(perfCtx, 'bar', {
        labels,
        datasets: [{ label: '% P&L', data, backgroundColor: data.map(v => v >= 0 ? 'rgba(34,197,94,0.6)' : 'rgba(239,68,68,0.6)') }]
      }, { scales: { y: { beginAtZero: true } } });
    }
  } catch (err) {
    console.error('Positions chart error', err);
  }
}

// =======================
// Trading Charts
// =======================
async function initTradingCharts() {
  try {
    const activity = await fetchJSON('/api/activity?days=1');
    if (!activity.length) return;

    // Volume by Hour
    const volCtx = document.getElementById('trading-volume-chart');
    if (volCtx) {
      const hours = Array.from({ length: 24 }, (_, h) => h);
      const volumeByHour = Array(24).fill(0);
      activity.forEach(a => {
        const date = new Date(a.time);
        const h = date.getHours();
        volumeByHour[h] += a.qty * a.price;
      });
      createChart(volCtx, 'bar', {
        labels: hours.map(h => `${h}:00`),
        datasets: [{ label: 'USD Volume', data: volumeByHour, backgroundColor: 'rgba(99,102,241,0.6)' }]
      }, { scales: { y: { beginAtZero: true } } });
    }

    // Order Distribution pie
    const distCtx = document.getElementById('trading-distribution-chart');
    if (distCtx) {
      const buys = activity.filter(a => a.side === 'BUY').length;
      const sells = activity.length - buys;
      createChart(distCtx, 'doughnut', {
        labels: ['Buys', 'Sells'],
        datasets: [{ data: [buys, sells], backgroundColor: ['rgba(34,197,94,0.6)', 'rgba(239,68,68,0.6)'] }]
      }, { plugins: { legend: { position: 'bottom' } } });
    }
  } catch (err) {
    console.error('Trading chart error', err);
  }
}

// =======================
// Webhook Charts (dummy until data streaming)
// =======================
function initWebhookCharts() {
  const ctx = document.getElementById('webhook-success-chart');
  if (!ctx) return;
  createChart(ctx, 'doughnut', {
    labels: ['Success', 'Error'],
    datasets: [{ data: [80, 20], backgroundColor: ['rgba(34,197,94,0.6)', 'rgba(239,68,68,0.6)'] }]
  });
}

// =======================
// Scanner Charts (placeholder demo)
// =======================
function initScannerCharts() {
  const perfCtx = document.getElementById('scanner-performance-chart');
  if (!perfCtx) return;
  const labels = Array.from({ length: 14 }, (_, i) => `Day ${i + 1}`);
  const data = labels.map(() => (Math.random() - 0.5) * 20);
  createChart(perfCtx, 'line', {
    labels,
    datasets: [{ label: 'Avg % Change', data, fill: false, borderColor: 'rgba(99,102,241,0.8)', tension: 0.3 }]
  }, { scales: { y: { beginAtZero: true } } });
}

// =======================
// Strategies Charts (placeholder demo)
// =======================
function initStrategyCharts() {
  const perfCtx = document.getElementById('strategy-performance-chart');
  if (!perfCtx) return;
  const labels = ['AI Signals', 'BB Strategy', 'Volume Momentum'];
  const returns = labels.map(() => (Math.random() * 150 - 50).toFixed(1));
  createChart(perfCtx, 'bar', {
    labels,
    datasets: [{ label: 'Total Return %', data: returns, backgroundColor: returns.map(v => v >= 0 ? 'rgba(34,197,94,0.6)' : 'rgba(239,68,68,0.6)') }]
  }, { scales: { y: { beginAtZero: true } } });
}

// =======================
// Initialisation
// =======================
function initAllCharts() {
  initDashboardCharts();
  initPositionsCharts();
  initTradingCharts();
  initWebhookCharts();
  initScannerCharts();
  initStrategyCharts();

  // refresh every minute
  setInterval(initAllCharts, 60 * 1000);
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initAllCharts);
} else {
  initAllCharts();
} 