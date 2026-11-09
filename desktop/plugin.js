// YouBench: Hermes Desktop half. A sidebar page with the live leaderboard, plus a status-bar
// chip while a benchmark runs. Data comes from this package's own backend (dashboard/plugin_api.py)
// through ctx.rest; updates arrive as `plugin.youbench.run.updated` events, with polling as fallback.
import { atom, Button, host, ROUTES_AREA, SIDEBAR_NAV_AREA, STATUSBAR_AREAS, useValue } from '@hermes/plugin-sdk'
import { useEffect } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'youbench'
const PATH = '/youbench'
const $state = atom({ loading: true, error: null, data: null })

const CSS = `
.yb{height:100%;overflow:auto;padding:28px 32px 48px;color:var(--ui-text-primary);font-size:13px}
.yb h1{font-size:22px;font-weight:650;margin:0;letter-spacing:-.01em}
.yb h1 em{font-style:normal;color:var(--ui-accent)}
.yb .sub{color:var(--ui-text-tertiary);margin:4px 0 22px}
.yb h2{font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:.08em;color:var(--ui-text-tertiary);margin:26px 0 10px}
.yb-cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px}
.yb-card{border:1px solid var(--ui-stroke-secondary);border-radius:10px;padding:12px 14px;min-width:0}
.yb-card .k{font-size:11px;text-transform:uppercase;letter-spacing:.07em;color:var(--ui-text-tertiary)}
.yb-card .v{font-size:16px;font-weight:600;margin-top:4px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.yb-card .d{font-size:11px;color:var(--ui-text-tertiary);margin-top:2px}
.yb table{width:100%;border-collapse:collapse;border:1px solid var(--ui-stroke-secondary);border-radius:10px;overflow:hidden}
.yb th{font-size:11px;font-weight:500;text-transform:uppercase;letter-spacing:.06em;color:var(--ui-text-tertiary);text-align:left;padding:8px 12px;border-bottom:1px solid var(--ui-stroke-secondary)}
.yb td{padding:9px 12px;border-bottom:1px solid var(--ui-stroke-secondary);font-variant-numeric:tabular-nums;vertical-align:middle}
.yb tr:last-child td{border-bottom:0}
.yb code{font-family:var(--font-mono,ui-monospace,monospace);font-size:12px}
.yb-rank{width:28px;color:var(--ui-text-tertiary)}
.yb-top td{background:color-mix(in srgb,var(--ui-accent) 9%,transparent)}
.yb-bar{display:inline-block;width:90px;height:6px;border-radius:3px;background:var(--ui-stroke-secondary);margin-right:8px;vertical-align:middle;overflow:hidden}
.yb-bar i{display:block;height:100%;background:var(--ui-accent);border-radius:3px;transition:width .6s ease}
.yb-chip{display:inline-flex;align-items:center;gap:5px;padding:1px 8px;border-radius:999px;font-size:11px;font-weight:600;border:1px solid var(--ui-stroke-secondary);color:var(--ui-text-secondary)}
.yb-chip.pass{color:#3ccf91;border-color:color-mix(in srgb,#3ccf91 45%,transparent)}
.yb-chip.fail{color:#ff5c6c;border-color:color-mix(in srgb,#ff5c6c 45%,transparent)}
.yb-chip.run{color:var(--ui-accent);border-color:color-mix(in srgb,var(--ui-accent) 45%,transparent)}
.yb-chip.run::before{content:'';width:6px;height:6px;border-radius:50%;background:currentColor;animation:ybpulse 1s infinite}
@keyframes ybpulse{50%{opacity:.25}}
.yb-why{color:var(--ui-text-tertiary);font-size:12px;max-width:440px}
.yb-empty{border:1px dashed var(--ui-stroke-secondary);border-radius:10px;padding:22px;color:var(--ui-text-tertiary);line-height:1.6}
.yb-empty b{color:var(--ui-text-secondary)}
.yb-verdict{margin-top:14px;padding:12px 14px;border-radius:10px;border:1px solid color-mix(in srgb,var(--ui-accent) 40%,transparent);background:color-mix(in srgb,var(--ui-accent) 7%,transparent)}
.yb-statusbar{display:flex;align-items:center;gap:6px;height:100%;padding:0 6px;font-size:11px;color:var(--ui-text-tertiary);cursor:pointer;background:none;border:0}
.yb-statusbar .dot{width:6px;height:6px;border-radius:50%;background:var(--ui-accent);animation:ybpulse 1s infinite}
`

const money = v => (v == null ? 'n/a' : v === 0 ? 'free' : v < 0.1 ? `$${v.toFixed(4)}` : `$${v.toFixed(2)}`)
const secs = v => (v == null ? 'n/a' : v < 120 ? `${Math.round(v)}s` : `${(v / 60).toFixed(1)}m`)
const pct = v => (v == null ? 'n/a' : `${Math.round(v * 100)}%`)
const DONE = new Set(['graded', 'failed', 'lost'])

function makeLoader(ctx) {
  let inflight = false
  return async function load() {
    if (inflight) return
    inflight = true
    try {
      const data = await ctx.rest('/state')
      $state.set({ loading: false, error: null, data })
    } catch (err) {
      $state.set({ loading: false, error: String(err?.message || err), data: $state.get().data })
    } finally {
      inflight = false
    }
  }
}

function isRunning(data) {
  return Boolean(data?.board && !data.board.all_done)
}

function Card({ k, v, d }) {
  return jsxs('div', { className: 'yb-card', children: [
    jsx('div', { className: 'k', children: k }),
    jsx('div', { className: 'v', title: v, children: v }),
    d ? jsx('div', { className: 'd', children: d }) : null
  ] })
}

function Chip({ r }) {
  if (DONE.has(r.status)) {
    return r.passed
      ? jsx('span', { className: 'yb-chip pass', children: 'PASS' })
      : jsx('span', { className: 'yb-chip fail', children: r.status === 'lost' ? 'LOST' : 'FAIL' })
  }
  const label = r.status === 'grading' ? 'grading' : r.status === 'running' ? 'running' : 'queued'
  return jsx('span', { className: `yb-chip ${r.status === 'queued' ? '' : 'run'}`, children: label })
}

function Leaderboard({ board }) {
  const rows = board.rows || []
  return jsxs('table', { children: [
    jsx('thead', { children: jsxs('tr', { children: ['#', 'Model', 'Passed', 'Quality', 'Cost / task', 'Avg time', 'Steps']
      .map(h => jsx('th', { children: h }, h)) }) }),
    jsx('tbody', { children: rows.map((r, i) => jsxs('tr', { className: i === 0 && r.done ? 'yb-top' : '', children: [
      jsx('td', { className: 'yb-rank', children: i === 0 && r.done ? '🏆' : i + 1 }),
      jsx('td', { children: jsx('code', { children: r.model }) }),
      jsxs('td', { children: [
        jsx('span', { className: 'yb-bar', children: jsx('i', { style: { width: `${Math.round((r.pass_rate || 0) * 100)}%` } }) }),
        `${r.passed}/${r.done}${r.done < r.total ? ` of ${r.total}` : ''} · ${pct(r.pass_rate)}`
      ] }),
      jsx('td', { children: r.avg_score == null ? 'n/a' : `${r.avg_score.toFixed(1)}/10` }),
      jsx('td', { children: money(r.cost_per_task) }),
      jsx('td', { children: secs(r.avg_seconds) }),
      jsx('td', { children: r.avg_steps == null ? 'n/a' : Math.round(r.avg_steps) })
    ] }, r.model)) })
  ] })
}

function Attempts({ board }) {
  const cases = board.cases || {}
  return jsxs('table', { children: [
    jsx('thead', { children: jsxs('tr', { children: ['Task', 'Model', 'Result', 'Cost', 'Time', 'Why']
      .map(h => jsx('th', { children: h }, h)) }) }),
    jsx('tbody', { children: (board.results || []).map(r => jsxs('tr', { children: [
      jsx('td', { children: cases[r.case_id]?.name || r.case_id }),
      jsx('td', { children: jsx('code', { children: r.model }) }),
      jsx('td', { children: jsx(Chip, { r }) }),
      jsx('td', { children: DONE.has(r.status) ? money(r.cost_usd) : '' }),
      jsx('td', { children: DONE.has(r.status) ? secs(r.seconds) : '' }),
      jsx('td', { className: 'yb-why', children: (r.reason || r.error || '').slice(0, 220) })
    ] }, `${r.case_id}|${r.model}`)) })
  ] })
}

function Cases({ cases }) {
  if (!cases.length) {
    return jsxs('div', { className: 'yb-empty', children: [
      jsx('b', { children: 'No saved tasks yet.' }), jsx('br', {}),
      'Ask Hermes to do a real task in a git project. When it finishes, type ',
      jsx('code', { children: '/youbench save --check "your test command"' }),
      '. Save a few, then type ', jsx('code', { children: '/youbench run <model> <model>' }), '.'
    ] })
  }
  return jsxs('table', { children: [
    jsx('thead', { children: jsxs('tr', { children: ['Case', 'Task', 'Graded by', 'Original']
      .map(h => jsx('th', { children: h }, h)) }) }),
    jsx('tbody', { children: cases.map(c => jsxs('tr', { children: [
      jsx('td', { children: jsx('code', { children: c.name }) }),
      jsx('td', { className: 'yb-why', children: c.prompt }),
      jsx('td', { children: c.check ? jsx('code', { children: c.check }) : 'judge' }),
      jsx('td', { children: c.orig_model ? `${c.orig_model} · ${secs(c.orig_seconds)}` : 'n/a' })
    ] }, c.id)) })
  ] })
}

function Page({ load }) {
  const { loading, error, data } = useValue($state)
  useEffect(() => { load() }, [])
  const board = data?.board
  const cases = data?.cases || []
  const best = board?.rows?.find(r => r.model === board.best)
  const value = board?.rows?.find(r => r.model === board.value)
  const base = board?.baseline
  const running = isRunning(data)

  return jsxs('div', { className: 'yb', children: [
    jsxs('div', { style: { display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: 12 }, children: [
      jsxs('div', { children: [
        jsxs('h1', { children: ['You', jsx('em', { children: 'Bench' })] }),
        jsx('div', { className: 'sub', children: 'Which model is best for your work? Measured on your own tasks, replayed from their exact starting files.' })
      ] }),
      jsx(Button, { size: 'sm', variant: 'outline', onClick: load, children: loading ? 'Loading…' : 'Refresh' })
    ] }),
    error ? jsx('div', { className: 'yb-empty', children: `Backend unavailable: ${error}. Enable the youbench plugin (hermes plugins enable youbench) and restart Hermes.` }) : null,
    jsxs('div', { className: 'yb-cards', children: [
      jsx(Card, { k: 'Saved tasks', v: String(cases.length), d: 'your real work' }),
      jsx(Card, { k: 'Best for you', v: best ? best.model : running ? 'running…' : 'n/a', d: best ? `${pct(best.pass_rate)} passed` : '' }),
      jsx(Card, { k: 'Best value', v: value ? value.model : 'n/a', d: value ? `${money(value.cost_per_task)} per task` : '' }),
      jsx(Card, { k: 'Your original runs', v: base?.models?.length ? money(base.cost_per_task) : 'n/a', d: base?.models?.join(', ') || '' })
    ] }),
    board ? jsxs('div', { children: [
      jsx('h2', { children: running ? 'Leaderboard · live' : 'Leaderboard' }),
      jsx(Leaderboard, { board }),
      !running && best ? jsxs('div', { className: 'yb-verdict', children: [
        jsx('b', { children: `🏆 ${best.model}` }), ` is the best model for your work (${pct(best.pass_rate)} of your tasks passed).`,
        value && value.model !== best.model ? ` Best value: ${value.model} at ${money(value.cost_per_task)} per task.` : '',
        ' Switch with ', jsx('code', { children: `/model ${(value || best).model}` }), '.'
      ] }) : null,
      jsx('h2', { children: 'Every attempt' }),
      jsx(Attempts, { board })
    ] }) : null,
    jsx('h2', { children: 'Your saved tasks' }),
    jsx(Cases, { cases })
  ] })
}

function StatusChip() {
  const { data } = useValue($state)
  if (!isRunning(data)) return null
  const results = data.board.results || []
  const done = results.filter(r => DONE.has(r.status)).length
  return jsxs('button', { type: 'button', className: 'yb-statusbar', onClick: () => host.navigate(PATH), children: [
    jsx('span', { className: 'dot' }), `YouBench ${done}/${results.length}`
  ] })
}

export default {
  id: ID,
  name: 'YouBench',
  description: 'Leaderboard of models replaying your own saved Hermes tasks.',
  register(ctx) {
    const style = document.createElement('style')
    style.textContent = CSS
    document.head.append(style)
    ctx.onDispose(() => style.remove())

    const load = makeLoader(ctx)
    ctx.onEvent('plugin.youbench.run.updated', () => load())
    ctx.setInterval(() => { if (isRunning($state.get().data)) load() }, 4000)
    load()

    ctx.registerMany([
      { id: 'page', area: ROUTES_AREA, data: { path: PATH }, render: () => jsx(Page, { load }) },
      { id: 'nav', area: SIDEBAR_NAV_AREA, data: { path: PATH, label: 'YouBench', codicon: 'graph' } },
      { id: 'status', area: STATUSBAR_AREAS.right, order: 120, render: () => jsx(StatusChip, {}) }
    ])
  }
}
