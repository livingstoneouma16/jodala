import { createContext, useContext, useState, useCallback, useEffect } from 'react'
import { api, getToken, setToken } from '../api/client'

const AuthContext = createContext(null)

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null)
  const [loading, setLoading] = useState(true)

  const loadMe = useCallback(async () => {
    if (!getToken()) {
      setUser(null)
      setLoading(false)
      return
    }
    try {
      const me = await api.get('/auth/me')
      setUser(me)
    } catch {
      setToken(null)
      setUser(null)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { loadMe() }, [loadMe])

  const login = useCallback(async (username, password, totpCode) => {
    const payload = { username, password }
    if (totpCode) payload.totp_code = totpCode
    const result = await api.post('/auth/login', payload)
    if (result.require_2fa) return result
    setToken(result.access_token)
    setUser(result.user)
    return result
  }, [])

  const logout = useCallback(() => {
    setToken(null)
    setUser(null)
  }, [])

  return (
    <AuthContext.Provider value={{ user, loading, login, logout, refresh: loadMe }}>
      {children}
    </AuthContext.Provider>
  )
}

export function useAuth() {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used within AuthProvider')
  return ctx
}
