import { NavLink, Outlet } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'

const NAV = [
  { to: '/dashboard', label: 'Dashboard' },
  { to: '/loans', label: 'Loans' },
  { to: '/loans/apply', label: 'New application' },
  { to: '/members', label: 'Members' },
  { to: '/clients', label: 'Clients' },
  { to: '/savings', label: 'Savings' },
  { to: '/repayments', label: 'Repayments' },
]

export default function Layout() {
  const { user, logout } = useAuth()

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="sidebar-brand">
          Jodala
          <small>Microfinance &middot; v3</small>
        </div>
        <nav className="sidebar-nav">
          {NAV.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.to === '/loans'}
              className={({ isActive }) => `sidebar-link${isActive ? ' active' : ''}`}
            >
              {item.label}
            </NavLink>
          ))}
        </nav>
        <div className="sidebar-footer">
          <div className="sidebar-user">{user?.full_name}</div>
          <div className="sidebar-role">{user?.role?.replace('_', ' ')}</div>
          <button className="sidebar-logout" onClick={logout}>Sign out</button>
        </div>
      </aside>
      <main className="main">
        <Outlet />
      </main>
    </div>
  )
}
