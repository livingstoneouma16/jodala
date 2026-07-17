export default function StatusStamp({ status }) {
  if (!status) return null
  const key = String(status).toLowerCase().replace(/\s+/g, '_')
  return <span className={`stamp stamp-${key}`}>{status.replace(/_/g, ' ')}</span>
}
