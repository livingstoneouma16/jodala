import { useEffect, useState } from 'react'
import { api } from '../api/client'
import StatusStamp from './StatusStamp'

const STATUSES = ['pending', 'approved', 'rejected', 'active', 'completed', 'written_off']

function money(n) {
  return `KSh ${Number(n || 0).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

export default function LoanList() {
  const [loans, setLoans] = useState([])
  const [search, setSearch] = useState('')
  const [status, setStatus] = useState('')
  const [page, setPage] = useState(1)
  const [pages, setPages] = useState(1)
  const [total, setTotal] = useState(0)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [loading, setLoading] = useState(true)
  const [busyId, setBusyId] = useState(null)

  useEffect(() => {
    const handle = setTimeout(load, 250)
    return () => clearTimeout(handle)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [search, status, page])

  async function load() {
    setLoading(true)
    try {
      const data = await api.get('/loans/api', { search, status, page, per_page: 15 })
      setLoans(data.loans)
      setPages(data.pages)
      setTotal(data.total)
      setError('')
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  async function act(loanId, action, body) {
    setBusyId(loanId)
    setError(''); setNotice('')
    try {
      const result = await api.post(`/loans/api/${loanId}/${action}`, body || {})
      setNotice(result.message)
      await load()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusyId(null)
    }
  }

  return (
    <>
      <div className="page-header">
        <div>
          <p className="page-eyebrow">Loan register</p>
          <h1>Loans</h1>
        </div>
      </div>

      {error && <div className="banner-error">{error}</div>}
      {notice && <div className="banner-success">{notice}</div>}

      <div className="ledger-card">
        <div className="ledger-toolbar">
          <input
            className="search-input"
            placeholder="Search loan number…"
            value={search}
            onChange={(e) => { setPage(1); setSearch(e.target.value) }}
          />
          <select className="filter-select" value={status} onChange={(e) => { setPage(1); setStatus(e.target.value) }}>
            <option value="">All statuses</option>
            {STATUSES.map((s) => <option key={s} value={s}>{s.replace('_', ' ')}</option>)}
          </select>
          <span style={{ marginLeft: 'auto', fontSize: 12, color: 'var(--stamp-neutral)', fontFamily: 'var(--font-mono)' }}>
            {total} loan{total === 1 ? '' : 's'}
          </span>
        </div>

        {loading ? (
          <div className="ledger-empty">Loading…</div>
        ) : loans.length === 0 ? (
          <div className="ledger-empty">No loans match this search.</div>
        ) : (
          <table className="ledger-table">
            <thead>
              <tr>
                <th>Loan No</th>
                <th>Borrower</th>
                <th>Product</th>
                <th>Principal</th>
                <th>Outstanding</th>
                <th>Status</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {loans.map((l) => (
                <tr key={l.id}>
                  <td className="num">{l.loan_number}</td>
                  <td>{l.member_name}</td>
                  <td>{l.product_name}</td>
                  <td className="num">{money(l.principal_amount)}</td>
                  <td className="num">{money(l.outstanding_balance)}</td>
                  <td><StatusStamp status={l.status} /></td>
                  <td>
                    <LoanActions loan={l} busy={busyId === l.id} onAct={(action, body) => act(l.id, action, body)} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}

        <div className="pagination">
          <span className="pagination-info">Page {page} of {pages}</span>
          <button disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>&larr; Prev</button>
          <button disabled={page >= pages} onClick={() => setPage((p) => p + 1)}>Next &rarr;</button>
        </div>
      </div>
    </>
  )
}

function LoanActions({ loan, busy, onAct }) {
  if (busy) return <span style={{ fontSize: 12, color: 'var(--stamp-neutral)' }}>Working…</span>

  if (loan.status === 'pending') {
    return (
      <div style={{ display: 'flex', gap: 6 }}>
        <button className="btn btn-secondary btn-sm" onClick={() => onAct('approve')}>Approve</button>
        <button
          className="btn btn-secondary btn-sm"
          onClick={() => {
            const reason = window.prompt('Reason for rejection?')
            if (reason !== null) onAct('reject', { reason })
          }}
        >
          Reject
        </button>
      </div>
    )
  }

  if (loan.status === 'approved') {
    return <button className="btn btn-primary btn-sm" onClick={() => onAct('disburse')}>Disburse</button>
  }

  return <span style={{ fontSize: 12, color: 'var(--stamp-neutral)' }}>—</span>
}
