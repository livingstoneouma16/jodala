# Jodala Microfinance — v3 frontend (React)

React rewrite of the UI, served by Flask at `/v3` alongside the existing
server-rendered pages at `/`, `/loans`, `/members`, etc. Both front ends hit
the same JSON API, so either can be used during the migration.

## Components
- `Dashboard` — portfolio stats, 12-month disbursed/collected trend, due-today and overdue lists
- `LoanList` — searchable/filterable loan register with approve/reject/disburse actions
- `LoanForm` — loan application with a live quote preview (calls `/loans/api/quote`)
- `MemberTable` — searchable/filterable member register

## Develop
```
npm install
npm run dev        # http://localhost:5173, proxies API calls to Flask on :5000
```
Run the Flask backend separately (`python app.py`) while developing.

## Build for production
```
npm run build       # outputs to dist/, base path baked in as /v3/
```
Flask serves `dist/` at `/v3/*` via `core/routes/v3.py` — rebuild and restart
Flask (or just refresh, since it reads from disk) after any frontend change.
The `dist/` folder in this repo is a build already; delete it and rebuild
whenever you change `src/`.
