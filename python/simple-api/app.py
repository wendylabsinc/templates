import os
from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()

hostname = (
    os.environ.get("WENDY_DEVICE_HOSTNAME")
    or os.environ.get("WENDY_HOSTNAME")
    or "localhost"
)


class Item(BaseModel):
    name: str
    price: float


@app.on_event("startup")
async def startup_event():
    print(f"Server running on {hostname}:{{.PORT}}", flush=True)


@app.get("/")
async def root():
    print("Received request: GET /", flush=True)
    return {"message": "hello-world"}


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/items", status_code=201)
async def create_item(item: Item):
    print(f"Received request: POST /items - {item.name}", flush=True)
    return {"id": 1, "name": item.name, "price": item.price}


if __name__ == "__main__":
    import socket
    import uvicorn

    # A pre-bound dual-stack socket keeps USB IPv6 and IPv4 LAN access working.
    if socket.has_dualstack_ipv6():
        with socket.create_server(
            ("::", {{.PORT}}), family=socket.AF_INET6, dualstack_ipv6=True
        ) as listener:
            uvicorn.run(app, fd=listener.fileno())
    else:
        uvicorn.run(app, host="0.0.0.0", port={{.PORT}})
