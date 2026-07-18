import { useEffect, useState } from 'react'
import { api } from '../api/client'

function money(n) {
  return `KSh ${Number(n || 0).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

export default function Repayments() {
  const [repayments, setRepayments] = useState([])
  const [page, setPage] = useState(1)
  const [pages, setPages] = useState(1)
  const [total, setTotal] = useState(0)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [loading, setLoading] = useState(true)
  const [showRecord, setShowRecord] = useState(false)

  useEffect(() => { load() }, [page])

  async function load() {
    setLoading(true)
    try {
      const data = await api.get('/repayments/api', { page, per_page: 15 })
      setRepayments(data.repayments)
      setPages(data.pages)
      setTotal(data.total)
      setError('')
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  async function record(payload) {
    setError(''); setNotice('')
    try {
      const result = await api.post('/repayments/api', payload)
      setNotice(`${result.message} — receipt ${result.repayment.receipt_number}`)
      setShowRecord(false)
      setPage(1)
      await load()
    } catch (err) {
      setError(err.message)
    }
  }

  return (
    <>
      <div className="page-header">
        <div>
          <p className="page-eyebrow">Repayment ledger</p>
          <h1>Repayments</h1>
        </div>
        <button className="btn btn-primary" onClick={() => setShowRecord(true)}>Record repayment</button>
      </div>

      {error && <div className="banner-error">{error}</div>}
      {notice && <div className="banner-success">{notice}</div>}

      <div className="ledger-card">
        <div className="ledger-toolbar">
          <span style={{ fontSize: 12, color: 'var(--stamp-neutral)', fontFamily: 'var(--font-mono)' }}>
            {total} repayment{total === 1 ? '' : 's'}
          </span>
        </div>

        {loading ? (
          <div className="ledger-empty">Loading…</div>
        ) : repayments.length === 0 ? (
          <div className="ledger-empty">No repayments recorded yet.</div>
        ) : (
          <table className="ledger-table">
            <thead>
              <tr>
                <th>Receipt No</th>
                <th>Loan No</th>
                <th>Amount</th>
                <th>Principal</th>
                <th>Interest</th>
                <th>Method</th>
                <th>Date</th>
              </tr>
            </thead>
            <tbody>
              {repayments.map((r) => (
                <tr key={r.id}>
                  <td className="num">{r.receipt_number}</td>
                  <td className="num">{r.loan_number}</td>
                  <td className="num">{money(r.amount)}</td>
                  <td className="num">{money(r.principal_portion)}</td>
                  <td className="num">{money(r.interest_portion)}</td>
                  <td>{r.payment_method}</td>
                  <td>{r.payment_date}</td>
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

      {showRecord && (
        <RecordRepaymentModal onClose={() => setShowRecord(false)} onSubmit={record} />
      )}
    </>
  )
}

function RecordRepaymentModal({ onClose, onSubmit }) {
  const [loans, setLoans] = useState([])
  const [form, setForm] = useState({
    loan_id: '', amount: '', payment_method: 'cash', reference_number: '', payment_date: '', notes: '',
  })
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    async function load() {
      const data = await api.get('/loans/api', { status: 'active', per_page: 200 })
      setLoans(data.loans)
    }
    load()
  }, [])

  function update(field, value) {
    setForm((f) => ({ ...f, [field]: value }))
  }

  async function handleSubmit(e) {
    e.preventDefault()
    setError('')
    setSubmitting(true)
    try {
      await onSubmit({
        loan_id: Number(form.loan_id),
        amount: Number(form.amount),
        payment_method: form.payment_method,
        reference_number: form.reference_number || undefined,
        payment_date: form.payment_date || undefined,
        notes: form.notes || undefined,
      })
    } catch (err) {
      setError(err.message)
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal-panel" onClick={(e) => e.stopPropagation()}>
        <h3>Record repayment</h3>
        {error && <div className="banner-error">{error}</div>}
        <form onSubmit={handleSubmit}>
          <div className="field">
            <label htmlFor="rp-loan">Loan</label>
            <select id="rp-loan" value={form.loan_id} onChange={(e) => update('loan_id', e.target.value)} required>
              <option value="">Select a loan…</option>
              {loans.map((l) => (
                <option key={l.id} value={l.id}>
                  {l.loan_number} — {l.member_name} (outstanding {money(l.outstanding_balance)})
                </option>
              ))}
            </select>
          </div>
          <div className="field-row">
            <div className="field">
              <label htmlFor="rp-amount">Amount</label>
              <input
                id="rp-amount"
                type="number"
                min="0"
                step="0.01"
                value={form.amount}
                onChange={(e) => update('amount', e.target.value)}
                required
              />
            </div>
            <div className="field">
              <label htmlFor="rp-method">Method</label>
              <select id="rp-method" value={form.payment_method} onChange={(e) => update('payment_method', e.target.value)}>
                <option value="cash">Cash</option>
                <option value="mpesa">M-Pesa</option>
                <option value="bank">Bank</option>
              </select>
            </div>
          </div>
          <div className="field-row">
            <div className="field">
              <label htmlFor="rp-ref">Reference number</label>
              <input id="rp-ref" value={form.reference_number} onChange={(e) => update('reference_number', e.target.value)} />
            </div>
            <div className="field">
              <label htmlFor="rp-date">Payment date</label>
              <input id="rp-date" type="date" value={form.payment_date} onChange={(e) => update('payment_date', e.target.value)} />
            </div>
          </div>
          <div className="field">
            <label htmlFor="rp-notes">Notes</label>
            <textarea id="rp-notes" rows={2} value={form.notes} onChange={(e) => update('notes', e.target.value)} />
          </div>
          <div className="modal-actions">
            <button type="button" className="btn btn-secondary" onClick={onClose}>Cancel</button>
            <button type="submit" className="btn btn-primary" disabled={submitting}>
              {submitting ? 'Recording…' : 'Record repayment'}
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}
