import { useEffect, useState } from 'react'
import { api } from '../api/client'
import StatusStamp from './StatusStamp'

function money(n) {
  return `KSh ${Number(n || 0).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

export default function SavingsAccounts() {
  const [accounts, setAccounts] = useState([])
  const [search, setSearch] = useState('')
  const [page, setPage] = useState(1)
  const [pages, setPages] = useState(1)
  const [total, setTotal] = useState(0)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [loading, setLoading] = useState(true)
  const [showOpen, setShowOpen] = useState(false)
  const [activeAccount, setActiveAccount] = useState(null)

  useEffect(() => {
    const handle = setTimeout(load, 250)
    return () => clearTimeout(handle)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [search, page])

  async function load() {
    setLoading(true)
    try {
      const data = await api.get('/savings/api/accounts', { search, page, per_page: 15 })
      setAccounts(data.accounts)
      setPages(data.pages)
      setTotal(data.total)
      setError('')
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  async function openAccount(payload) {
    setError(''); setNotice('')
    try {
      const result = await api.post('/savings/api/accounts', payload)
      setNotice(result.message)
      setShowOpen(false)
      await load()
    } catch (err) {
      setError(err.message)
    }
  }

  async function viewAccount(id) {
    setError('')
    try {
      const data = await api.get(`/savings/api/accounts/${id}`)
      setActiveAccount(data)
    } catch (err) {
      setError(err.message)
    }
  }

  async function transact(kind, amount, method) {
    setError(''); setNotice('')
    try {
      const result = await api.post(`/savings/api/${kind}`, {
        account_id: activeAccount.id,
        amount: Number(amount),
        payment_method: method,
      })
      setNotice(result.message)
      await load()
      await viewAccount(activeAccount.id)
    } catch (err) {
      setError(err.message)
    }
  }

  return (
    <>
      <div className="page-header">
        <div>
          <p className="page-eyebrow">Savings register</p>
          <h1>Savings accounts</h1>
        </div>
        <button className="btn btn-primary" onClick={() => setShowOpen(true)}>Open account</button>
      </div>

      {error && <div className="banner-error">{error}</div>}
      {notice && <div className="banner-success">{notice}</div>}

      <div className="ledger-card">
        <div className="ledger-toolbar">
          <input
            className="search-input"
            placeholder="Search by account number or member name…"
            value={search}
            onChange={(e) => { setPage(1); setSearch(e.target.value) }}
          />
          <span style={{ marginLeft: 'auto', fontSize: 12, color: 'var(--stamp-neutral)', fontFamily: 'var(--font-mono)' }}>
            {total} account{total === 1 ? '' : 's'}
          </span>
        </div>

        {loading ? (
          <div className="ledger-empty">Loading…</div>
        ) : accounts.length === 0 ? (
          <div className="ledger-empty">No savings accounts match this search.</div>
        ) : (
          <table className="ledger-table">
            <thead>
              <tr>
                <th>Account No</th>
                <th>Member</th>
                <th>Product</th>
                <th>Balance</th>
                <th>Status</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {accounts.map((a) => (
                <tr key={a.id}>
                  <td className="num">{a.account_number}</td>
                  <td>{a.member_name}</td>
                  <td>{a.product_name}</td>
                  <td className="num">{money(a.balance)}</td>
                  <td><StatusStamp status={a.status} /></td>
                  <td>
                    <button className="btn btn-secondary btn-sm" onClick={() => viewAccount(a.id)}>Deposit / Withdraw</button>
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

      {showOpen && (
        <OpenAccountModal onClose={() => setShowOpen(false)} onSubmit={openAccount} />
      )}

      {activeAccount && (
        <AccountModal account={activeAccount} onClose={() => setActiveAccount(null)} onTransact={transact} />
      )}
    </>
  )
}

function OpenAccountModal({ onClose, onSubmit }) {
  const [members, setMembers] = useState([])
  const [products, setProducts] = useState([])
  const [form, setForm] = useState({ member_id: '', product_id: '', initial_deposit: '', payment_method: 'cash' })
  const [submitting, setSubmitting] = useState(false)

  useEffect(() => {
    async function load() {
      const [memberData, productData] = await Promise.all([
        api.get('/members/api', { per_page: 200, status: 'active' }),
        api.get('/savings/api/products'),
      ])
      setMembers(memberData.members)
      setProducts(productData)
    }
    load()
  }, [])

  function update(field, value) {
    setForm((f) => ({ ...f, [field]: value }))
  }

  async function handleSubmit(e) {
    e.preventDefault()
    setSubmitting(true)
    await onSubmit({
      member_id: Number(form.member_id),
      product_id: Number(form.product_id),
      initial_deposit: form.initial_deposit ? Number(form.initial_deposit) : 0,
      payment_method: form.payment_method,
    })
    setSubmitting(false)
  }

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal-panel" onClick={(e) => e.stopPropagation()}>
        <h3>Open savings account</h3>
        <form onSubmit={handleSubmit}>
          <div className="field">
            <label htmlFor="sa-member">Member</label>
            <select id="sa-member" value={form.member_id} onChange={(e) => update('member_id', e.target.value)} required>
              <option value="">Select a member…</option>
              {members.map((m) => (
                <option key={m.id} value={m.id}>{m.full_name} ({m.member_number})</option>
              ))}
            </select>
          </div>
          <div className="field">
            <label htmlFor="sa-product">Savings product</label>
            <select id="sa-product" value={form.product_id} onChange={(e) => update('product_id', e.target.value)} required>
              <option value="">Select a product…</option>
              {products.map((p) => (
                <option key={p.id} value={p.id}>{p.name}</option>
              ))}
            </select>
          </div>
          <div className="field">
            <label htmlFor="sa-deposit">Initial deposit (optional)</label>
            <input
              id="sa-deposit"
              type="number"
              min="0"
              step="0.01"
              value={form.initial_deposit}
              onChange={(e) => update('initial_deposit', e.target.value)}
            />
          </div>
          <div className="modal-actions">
            <button type="button" className="btn btn-secondary" onClick={onClose}>Cancel</button>
            <button type="submit" className="btn btn-primary" disabled={submitting}>
              {submitting ? 'Opening…' : 'Open account'}
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}

function AccountModal({ account, onClose, onTransact }) {
  const [amount, setAmount] = useState('')
  const [method, setMethod] = useState('cash')

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal-panel" onClick={(e) => e.stopPropagation()}>
        <h3>{account.account_number} &middot; {account.member_name}</h3>
        <p className="field-hint">Balance: {money(account.balance)}</p>

        <div className="field-row">
          <div className="field">
            <label htmlFor="txn-amount">Amount</label>
            <input id="txn-amount" type="number" min="0" step="0.01" value={amount} onChange={(e) => setAmount(e.target.value)} />
          </div>
          <div className="field">
            <label htmlFor="txn-method">Method</label>
            <select id="txn-method" value={method} onChange={(e) => setMethod(e.target.value)}>
              <option value="cash">Cash</option>
              <option value="mpesa">M-Pesa</option>
              <option value="bank">Bank</option>
            </select>
          </div>
        </div>

        <div className="modal-actions">
          <button className="btn btn-secondary" onClick={() => { onTransact('withdraw', amount, method); setAmount('') }} disabled={!amount}>
            Withdraw
          </button>
          <button className="btn btn-primary" onClick={() => { onTransact('deposit', amount, method); setAmount('') }} disabled={!amount}>
            Deposit
          </button>
        </div>

        <h4 style={{ marginTop: 20 }}>Recent transactions</h4>
        <table className="ledger-table">
          <thead>
            <tr>
              <th>Txn No</th>
              <th>Type</th>
              <th>Amount</th>
              <th>Balance after</th>
              <th>Date</th>
            </tr>
          </thead>
          <tbody>
            {(account.transactions || []).map((t) => (
              <tr key={t.id}>
                <td className="num">{t.transaction_number}</td>
                <td><StatusStamp status={t.transaction_type} /></td>
                <td className="num">{money(t.amount)}</td>
                <td className="num">{money(t.balance_after)}</td>
                <td>{t.transaction_date}</td>
              </tr>
            ))}
          </tbody>
        </table>

        <div className="modal-actions">
          <button className="btn btn-secondary" onClick={onClose}>Close</button>
        </div>
      </div>
    </div>
  )
}
