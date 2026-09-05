import express from 'express'

const app = express()

app.get('/api/hello', (_req, res) => res.json({ message: 'Hello from Wendy!' }))
app.get('/health', (_req, res) => res.json({ status: 'ok' }))
app.use(express.static('static'))

app.listen({{.PORT}}, '0.0.0.0', () => console.log('Listening on port {{.PORT}}'))
