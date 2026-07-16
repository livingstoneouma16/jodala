import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer } from 'recharts'
import { api } from '../api/client'

function money(n) {
  return `KSh ${Number(n || 0).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

export default function Dashboard() {
  const [stats, setStats] = useState(null)
  const [trend, setTrend] = useState([])
  const [dueToday, setDueToday] = useState([])
  const [overdue, setOverdue] = useState([])
  const [error, setError] = useState('')

  useEffect(() => {
    async function load() {
      try {
        const [s, t, d, o] = await Promise.all([
          api.get('/dashboard/stats'),
          api.get('/dashboard/loan-trend'),
          api.get('/dashboard/due-today'),
          api.get('/dashboard/overdue-loans'),
        ])
        setStats(s); setTrend(t); setDueToday(d); setOverdue(o)
      } catch (err) {
        setError(err.message)
      }
    }
    load()
  }, [])

  if (error) return <div className="banner-error">{error}</div>
  if (!stats) return <p style={{ color: 'var(--stamp-neutral)' }}>Loading ledger…</p>

  return (
    <>
      <div className="page-header">
        <div>
          <p className="page-eyebrow">Overview</p>
          <h1>Dashboard</h1>
        </div>
      </div>

      <div className="stat-grid">
        <StatCard label="Active loans" value={stats.active_loans} />
        <StatCard label="Pending applications" value={stats.pending_loans} />
        <StatCard label="Outstanding portfolio" value={money(stats.total_outstanding)} />
        <StatCard label="Portfolio at risk" value={`${stats.par}%`} sub={`${stats.overdue_loans} loans overdue`} />
        <StatCard label="Collected this month" value={money(stats.monthly_collections)} />
        <StatCard label="Monthly profit" value={money(stats.monthly_profit)} />
        <StatCard label="Total members" value={stats.total_members} />
        <StatCard label="Member savings" value={money(stats.total_savings)} />
      </div>

      <div className="ledger-card" style={{ padding: '20px 20px 8px', marginBottom: 24 }}>
        <h3 style={{ fontFamily: 'var(--font-mono)', fontSize: 12, textTransform: 'uppercase', letterSpacing: '0.06em', color: 'var(--forest-700)', marginBottom: 12 }}>
          Disbursed vs. collected &mdash; last 12 months
        </h3>
        <ResponsiveContainer width="100%" height={240}>
          <LineChart data={trend} margin={{ top: 4, right: 12, left: 0, bottom: 4 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="var(--rule)" />
            <XAxis dataKey="month" tick={{ fontSize: 11, fontFamily: 'var(--font-mono)' }} />
            <YAxis tick={{ fontSize: 11, fontFamily: 'var(--font-mono)' }} />
            <Tooltip formatter={(v) => money(v)} />
            <Line type="monotone" dataKey="disbursed" stroke="#1b4332" strokeWidth={2} dot={false} name="Disbursed" />
            <Line type="monotone" dataKey="collected" stroke="#a9761f" strokeWidth={2} dot={false} name="Collected" />
          </LineChart>
        </ResponsiveContainer>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 20 }}>
        <div className="ledger-card">
          <div className="ledger-toolbar"><strong>Due today</strong></div>
          {dueToday.length === 0 ? (
            <div className="ledger-empty">Nothing due today.</div>
          ) : (
            <table className="ledger-table">
              <thead><tr><th>Loan</th><th>Borrower</th><th>Amount</th></tr></thead>
              <tbody>
                {dueToday.map((r, i) => (
                  <tr key={i}>
                    <td className="num">{r.loan_number}</td>
                    <td>{r.borrower}</td>
                    <td className="num">{money(r.amount_due)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>

        <div className="ledger-card">
          <div className="ledger-toolbar"><strong>Overdue</strong></div>
          {overdue.length === 0 ? (
            <div className="ledger-empty">No overdue loans. Well kept books.</div>
          ) : (
            <table className="ledger-table">
              <thead><tr><th>Loan</th><th>Borrower</th><th>Days</th><th>Outstanding</th></tr></thead>
              <tbody>
                {overdue.map((r, i) => (
                  <tr key={i}>
                    <td className="num">{r.loan_number}</td>
                    <td>{r.borrower}</td>
                    <td className="num">{r.overdue_days}</td>
                    <td className="num">{money(r.outstanding)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>

      <p style={{ marginTop: 24 }}>
        <Link to="/loans/apply" className="btn btn-secondary">Start a new loan application &rarr;</Link>
      </p>
    </>
  )
}

function StatCard({ label, value, sub }) {
  return (
    <div className="stat-card">
      <div className="stat-label">{label}</div>
      <div className="stat-value">{value}</div>
      {sub && <div className="stat-sub">{sub}</div>}
    </div>
  )
}
