import { useEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'
import './style.css'

function App() {
  const [message, setMessage] = useState('Connecting…')
  const [error, setError] = useState(false)

  async function refresh() {
    setError(false)
    setMessage('Connecting…')
    try {
      const response = await fetch('/api/hello')
      if (!response.ok) throw new Error(`HTTP ${response.status}`)
      const data = await response.json()
      if (typeof data.message !== 'string') throw new Error('Missing message')
      setMessage(data.message)
    } catch {
      setError(true)
      setMessage('Could not reach your app. Try again in a moment.')
    }
  }

  useEffect(() => { void refresh() }, [])

  return (
    <main>
      <span className="brand">wendy<span className="dot">.</span></span>
      <p className="eyebrow">YOUR NEXT IDEA STARTS HERE</p>
      <h1>Hello, possibility.</h1>
      <p className="intro">A little app. A world of things to build.</p>
      <section aria-label="Message from your app">
        <span className={`status ${error ? 'error' : ''}`} aria-hidden="true" />
        <p role="status">{message}</p>
        <button onClick={() => void refresh()}>Refresh <span aria-hidden="true">↗</span></button>
      </section>
      <footer>Made with Wendy. Make it yours.</footer>
    </main>
  )
}

createRoot(document.getElementById('root')!).render(<App />)
