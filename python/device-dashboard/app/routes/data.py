from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.lib.db import get_db

router = APIRouter()


class CarInput(BaseModel):
    make: str
    model: str
    color: str
    year: int


@router.get("/cars")
def list_cars():
    db = get_db()
    rows = db.execute("SELECT * FROM cars ORDER BY id").fetchall()
    db.close()
    return [dict(r) for r in rows]


@router.post("/cars", status_code=201)
def create_car(car: CarInput):
    db = get_db()
    cur = db.execute(
        "INSERT INTO cars (make, model, color, year) VALUES (?, ?, ?, ?)",
        (car.make, car.model, car.color, car.year),
    )
    db.commit()
    row = db.execute("SELECT * FROM cars WHERE id = ?", (cur.lastrowid,)).fetchone()
    db.close()
    return dict(row)


@router.get("/cars/{car_id}")
def get_car(car_id: int):
    db = get_db()
    row = db.execute("SELECT * FROM cars WHERE id = ?", (car_id,)).fetchone()
    db.close()
    if not row:
        raise HTTPException(404, "Car not found")
    return dict(row)


@router.put("/cars/{car_id}")
def update_car(car_id: int, car: CarInput):
    db = get_db()
    db.execute(
        "UPDATE cars SET make=?, model=?, color=?, year=?, updated_at=datetime('now') WHERE id=?",
        (car.make, car.model, car.color, car.year, car_id),
    )
    db.commit()
    row = db.execute("SELECT * FROM cars WHERE id = ?", (car_id,)).fetchone()
    db.close()
    if not row:
        raise HTTPException(404, "Car not found")
    return dict(row)


@router.delete("/cars/{car_id}", status_code=204)
def delete_car(car_id: int):
    db = get_db()
    cur = db.execute("DELETE FROM cars WHERE id = ?", (car_id,))
    db.commit()
    db.close()
    if cur.rowcount == 0:
        raise HTTPException(404, "Car not found")
