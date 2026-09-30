import React from 'react'
import ReactDOM from 'react-dom/client'

import App from './App'
import LogsPage from './components/LogsPage'
import './index.css'

// No router dependency for one extra page — a hash switch is enough, and
// keeps this project's dependency list as small as it already is.
function Root() {
  const [hash, setHash] = React.useState(window.location.hash)
  React.useEffect(() => {
    const onChange = () => setHash(window.location.hash)
    window.addEventListener('hashchange', onChange)
    return () => window.removeEventListener('hashchange', onChange)
  }, [])
  return hash === '#/logs' ? <LogsPage /> : <App />
}

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <Root />
  </React.StrictMode>,
)
