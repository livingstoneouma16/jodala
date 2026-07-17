import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api/client'

function money(n) {
  return `KSh ${Number(n || 0).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

export default function LoanForm() {
  const navigate = useNavigate()
  const [products, setProducts] = useState([])
  const [members, setMembers] = useState([])
  const [form, setForm] = useState({
    product_id: '', borrower_type: 'member', member_id: '', principal_amount: '', term: '', purpose: '',
  })
  const [quote, setQuote] = useState(null)
  const [quoteError, setQuoteError] = useState('')
  const [submitError, setSubmitError] = useState('')
  const [submitting, setSubmitting] = useState(false)

  useEffect(() => {
    async function load() {
      const [productData, memberData] = await Promise.all([
        api.get('/settings/api/loan-products'),
        api.get('/members/api', { per_page: 200, status: 'active' }),
      ])
      setProducts(productData.filter((p) => p.is_active))
      setMembers(memberData.members)
    }
    load()
  }, [])

  const selectedProduct = products.find((p) => String(p.id) === String(form.product_id))

  // Live quote preview whenever the numbers that affect it change.
  useEffect(() => {
    setQuote(null)
    setQuoteError('')
    if (!form.product_id || !form.principal_amount || !form.term) return

    const handle = setTimeout(async () => {
      try {
        const result = await api.post('/loans/api/quote', {
          product_id: Number(form.product_id),
          principal_amount: Number(form.principal_amount),
          term: Number(form.term),
        })
        setQuote(result)
      } catch (err) {
        setQuoteError(err.message)
      }
    }, 350)
    return () => clearTimeout(handle)
  }, [form.product_id, form.principal_amount, form.term])

  function update(field, value) {
    setForm((f) => ({ ...f, [field]: value }))
  }

  async function handleSubmit(e) {
    e.preventDefault()
    setSubmitError('')
    setSubmitting(true)
    try {
      const result = await api.post('/loans/api', {
        product_id: Number(form.product_id),
        borrower_type: form.borrower_type,
        member_id: form.borrower_type === 'member' ? Number(form.member_id) : undefined,
        principal_amount: Number(form.principal_amount),
        term: Number(form.term),
        purpose: form.purpose,
      })
      navigate('/loans', { state: { justCreated: result.loan.loan_number } })
    } catch (err) {
      setSubmitError(err.message)
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <>
      <div className="page-header">
        <div>
          <p className="page-eyebrow">New entry</p>
          <h1>Loan application</h1>
        </div>
      </div>

      {submitError && <div className="banner-error">{submitError}</div>}

      <div className="ledger-card" style={{ padding: 24, maxWidth: 640 }}>
        <form onSubmit={handleSubmit}>
          <div className="field">
            <label htmlFor="product">Loan product</label>
            <select id="product" value={form.product_id} onChange={(e) => update('product_id', e.target.value)} required>
              <option value="">Select a product…</option>
              {products.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name} ({p.interest_rate}% {p.interest_type}, {p.min_term}–{p.max_term} {p.repayment_frequency})
                </option>
              ))}
            </select>
            {selectedProduct && (
              <p className="field-hint">
                Amount range {money(selectedProduct.min_amount)}–{money(selectedProduct.max_amount)}
              </p>
            )}
          </div>

          <div className="field">
            <label htmlFor="member">Member</label>
            <select id="member" value={form.member_id} onChange={(e) => update('member_id', e.target.value)} required>
              <option value="">Select a member…</option>
              {members.map((m) => (
                <option key={m.id} value={m.id}>{m.full_name} ({m.member_number})</option>
              ))}
            </select>
          </div>

          <div className="field-row">
            <div className="field">
              <label htmlFor="principal">Principal amount</label>
              <input
                id="principal"
                type="number"
                min="0"
                step="0.01"
                value={form.principal_amount}
                onChange={(e) => update('principal_amount', e.target.value)}
                required
              />
            </div>
            <div className="field">
              <label htmlFor="term">Term (periods)</label>
              <input
                id="term"
                type="number"
                min="1"
                value={form.term}
                onChange={(e) => update('term', e.target.value)}
                required
              />
            </div>
          </div>

          <div className="field">
            <label htmlFor="purpose">Purpose</label>
            <textarea id="purpose" rows={2} value={form.purpose} onChange={(e) => update('purpose', e.target.value)} />
          </div>

          {quoteError && <div className="banner-error">{quoteError}</div>}
          {quote && (
            <div className="quote-panel">
              <h3>Quote preview</h3>
              <div className="quote-grid">
                <QuoteItem label="Installment" value={money(quote.installment_amount)} />
                <QuoteItem label="Total interest" value={money(quote.total_interest)} />
                <QuoteItem label="Total repayable" value={money(quote.total_repayable)} />
                <QuoteItem label="Net disbursement" value={money(quote.net_disbursement)} />
              </div>
            </div>
          )}

          <button className="btn btn-primary" disabled={submitting || !quote}>
            {submitting ? 'Submitting…' : 'Submit application'}
          </button>
        </form>
      </div>
    </>
  )
}

function QuoteItem({ label, value }) {
  return (
    <div className="quote-item">
      <div className="quote-label">{label}</div>
      <div className="quote-value">{value}</div>
    </div>
  )
}
