import os
from threading import Lock

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

app = FastAPI()

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class CarInput(BaseModel):
    make: str
    model: str
    color: str
    year: int


class Car(BaseModel):
    id: int
    make: str
    model: str
    color: str
    year: int


# ---------------------------------------------------------------------------
# In-memory store
# ---------------------------------------------------------------------------

_cars: list[dict] = []
_next_id = 1
_lock = Lock()

# ---------------------------------------------------------------------------
# CRUD endpoints
# ---------------------------------------------------------------------------

@app.get("/api/cars", response_model=list[Car])
def list_cars():
    with _lock:
        return list(_cars)


@app.post("/api/cars", response_model=Car, status_code=201)
def create_car(car: CarInput):
    global _next_id
    with _lock:
        new_car = {"id": _next_id, **car.model_dump()}
        _next_id += 1
        _cars.append(new_car)
    return new_car


@app.get("/api/cars/{car_id}", response_model=Car)
def get_car(car_id: int):
    with _lock:
        for c in _cars:
            if c["id"] == car_id:
                return c
    raise HTTPException(status_code=404, detail="Car not found")


@app.put("/api/cars/{car_id}", response_model=Car)
def update_car(car_id: int, car: CarInput):
    with _lock:
        for i, c in enumerate(_cars):
            if c["id"] == car_id:
                _cars[i] = {"id": car_id, **car.model_dump()}
                return _cars[i]
    raise HTTPException(status_code=404, detail="Car not found")


@app.delete("/api/cars/{car_id}", status_code=204)
def delete_car(car_id: int):
    with _lock:
        for i, c in enumerate(_cars):
            if c["id"] == car_id:
                _cars.pop(i)
                return
    raise HTTPException(status_code=404, detail="Car not found")


# ---------------------------------------------------------------------------
# Serve React SPA from ./static
# ---------------------------------------------------------------------------

app.mount("/", StaticFiles(directory="static", html=True), name="static")

# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    hostname = os.environ.get("WENDY_DEVICE_HOSTNAME") or os.environ.get(
        "WENDY_HOSTNAME", "localhost"
    )
    print(f"Starting on {hostname}:{{{{.PORT}}}}")
    uvicorn.run(app, host="0.0.0.0", port={{.PORT}})
