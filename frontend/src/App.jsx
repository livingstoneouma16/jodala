import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import { AuthProvider } from './context/AuthContext'
import RequireAuth from './components/RequireAuth'
import Layout from './components/Layout'
import Login from './components/Login'
import Dashboard from './components/Dashboard'
import LoanList from './components/LoanList'
import LoanForm from './components/LoanForm'
import MemberTable from './components/MemberTable'
import SavingsAccounts from './components/SavingsAccounts'
import Repayments from './components/Repayments'

export default function App() {
  return (
    <BrowserRouter basename="/v3">
      <AuthProvider>
        <Routes>
          <Route path="/login" element={<Login />} />
          <Route
            path="/"
            element={
              <RequireAuth>
                <Layout />
              </RequireAuth>
            }
          >
            <Route index element={<Navigate to="dashboard" replace />} />
            <Route path="dashboard" element={<Dashboard />} />
            <Route path="loans" element={<LoanList />} />
            <Route path="loans/apply" element={<LoanForm />} />
            <Route path="members" element={<MemberTable />} />
            <Route path="savings" element={<SavingsAccounts />} />
            <Route path="repayments" element={<Repayments />} />
          </Route>
          <Route path="*" element={<Navigate to="/dashboard" replace />} />
        </Routes>
      </AuthProvider>
    </BrowserRouter>
  )
}
