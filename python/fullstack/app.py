from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

app = FastAPI()


@app.get('/api/hello')
def hello():
    return {'message': 'Hello from Wendy!'}


@app.get('/health')
def health():
    return {'status': 'ok'}


app.mount('/', StaticFiles(directory='static', html=True), name='frontend')
