import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'

export default function Login() {
  const { login } = useAuth()
  const navigate = useNavigate()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [totpCode, setTotpCode] = useState('')
  const [needs2fa, setNeeds2fa] = useState(false)
  const [error, setError] = useState('')
  const [submitting, setSubmitting] = useState(false)

  async function handleSubmit(e) {
    e.preventDefault()
    setError('')
    setSubmitting(true)
    try {
      const result = await login(username, password, totpCode)
      if (result?.require_2fa) {
        setNeeds2fa(true)
      } else {
        navigate('/dashboard')
      }
    } catch (err) {
      setError(err.message || 'Invalid credentials')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="login-screen">
      <div className="login-card">
        <div className="login-eyebrow">Jodala Microfinance</div>
        <h1>Sign in</h1>
        <p style={{ color: 'var(--stamp-neutral)', fontSize: 13, marginTop: 4, marginBottom: 20 }}>
          Staff portal &mdash; v3
        </p>

        {error && <div className="banner-error">{error}</div>}

        <form onSubmit={handleSubmit}>
          <div className="field">
            <label htmlFor="username">Username or email</label>
            <input
              id="username"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              autoFocus
              required
            />
          </div>
          <div className="field">
            <label htmlFor="password">Password</label>
            <input
              id="password"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
            />
          </div>
          {needs2fa && (
            <div className="field">
              <label htmlFor="totp">Authenticator code</label>
              <input
                id="totp"
                value={totpCode}
                onChange={(e) => setTotpCode(e.target.value)}
                placeholder="6-digit code"
                autoFocus
                required
              />
            </div>
          )}
          <button className="btn btn-primary" style={{ width: '100%', marginTop: 8 }} disabled={submitting}>
            {submitting ? 'Signing in…' : needs2fa ? 'Verify' : 'Sign in'}
          </button>
        </form>
      </div>
    </div>
  )
}
