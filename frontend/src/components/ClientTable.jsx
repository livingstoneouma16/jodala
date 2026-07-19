import { useEffect, useState } from 'react'
import { api } from '../api/client'
import StatusStamp from './StatusStamp'

export default function ClientTable() {
  const [clients, setClients] = useState([])
  const [search, setSearch] = useState('')
  const [status, setStatus] = useState('')
  const [page, setPage] = useState(1)
  const [pages, setPages] = useState(1)
  const [total, setTotal] = useState(0)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    const handle = setTimeout(load, 250) // debounce search typing
    return () => clearTimeout(handle)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [search, status, page])

  async function load() {
    setLoading(true)
    try {
      const data = await api.get('/clients/api', { search, status, page, per_page: 15 })
      setClients(data.clients)
      setPages(data.pages)
      setTotal(data.total)
      setError('')
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  return (
    <>
      <div className="page-header">
        <div>
          <p className="page-eyebrow">Client register</p>
          <h1>Clients</h1>
        </div>
      </div>

      {error && <div className="banner-error">{error}</div>}

      <div className="ledger-card">
        <div className="ledger-toolbar">
          <input
            className="search-input"
            placeholder="Search by name, phone, client no, national ID…"
            value={search}
            onChange={(e) => { setPage(1); setSearch(e.target.value) }}
          />
          <select className="filter-select" value={status} onChange={(e) => { setPage(1); setStatus(e.target.value) }}>
            <option value="">All statuses</option>
            <option value="active">Active</option>
            <option value="suspended">Suspended</option>
            <option value="blacklisted">Blacklisted</option>
            <option value="inactive">Inactive</option>
          </select>
          <span style={{ marginLeft: 'auto', fontSize: 12, color: 'var(--stamp-neutral)', fontFamily: 'var(--font-mono)' }}>
            {total} client{total === 1 ? '' : 's'}
          </span>
        </div>

        {loading ? (
          <div className="ledger-empty">Loading…</div>
        ) : clients.length === 0 ? (
          <div className="ledger-empty">No clients match this search.</div>
        ) : (
          <table className="ledger-table">
            <thead>
              <tr>
                <th>Client No</th>
                <th>Name</th>
                <th>Phone</th>
                <th>Region</th>
                <th>Occupation</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {clients.map((c) => (
                <tr key={c.id}>
                  <td className="num">{c.client_number}</td>
                  <td>{c.full_name}</td>
                  <td className="num">{c.phone}</td>
                  <td>{c.region || '—'}</td>
                  <td>{c.occupation || '—'}</td>
                  <td><StatusStamp status={c.status} /></td>
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
