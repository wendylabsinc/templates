import { useState, useEffect, type FormEvent } from "react"

interface Car {
  id: number
  make: string
  model: string
  color: string
  year: number
}

export default function App() {
  const [cars, setCars] = useState<Car[]>([])
  const [make, setMake] = useState("")
  const [model, setModel] = useState("")
  const [color, setColor] = useState("#3b82f6")
  const [year, setYear] = useState(new Date().getFullYear())

  const fetchCars = async () => {
    const res = await fetch("/api/cars")
    if (res.ok) setCars(await res.json())
  }

  useEffect(() => {
    fetchCars()
  }, [])

  const addCar = async (e: FormEvent) => {
    e.preventDefault()
    await fetch("/api/cars", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ make, model, color, year }),
    })
    setMake("")
    setModel("")
    setColor("#3b82f6")
    setYear(new Date().getFullYear())
    fetchCars()
  }

  const deleteCar = async (id: number) => {
    await fetch(`/api/cars/${id}`, { method: "DELETE" })
    fetchCars()
  }

  return (
    <div className="mx-auto max-w-4xl p-8">
      <h1 className="text-3xl font-bold tracking-tight mb-8">Cars</h1>

      <form onSubmit={addCar} className="mb-8 flex flex-wrap items-end gap-3">
        <div className="flex flex-col gap-1">
          <label className="text-sm font-medium">Make</label>
          <input
            className="rounded-md border px-3 py-2 text-sm"
            value={make}
            onChange={(e) => setMake(e.target.value)}
            placeholder="Toyota"
            required
          />
        </div>
        <div className="flex flex-col gap-1">
          <label className="text-sm font-medium">Model</label>
          <input
            className="rounded-md border px-3 py-2 text-sm"
            value={model}
            onChange={(e) => setModel(e.target.value)}
            placeholder="Camry"
            required
          />
        </div>
        <div className="flex flex-col gap-1">
          <label className="text-sm font-medium">Year</label>
          <input
            className="rounded-md border px-3 py-2 text-sm w-24"
            type="number"
            value={year}
            onChange={(e) => setYear(Number(e.target.value))}
            required
          />
        </div>
        <div className="flex flex-col gap-1">
          <label className="text-sm font-medium">Color</label>
          <input
            className="h-10 w-14 cursor-pointer rounded-md border p-1"
            type="color"
            value={color}
            onChange={(e) => setColor(e.target.value)}
          />
        </div>
        <button
          type="submit"
          className="rounded-md bg-neutral-900 px-4 py-2 text-sm font-medium text-white hover:bg-neutral-800"
        >
          Add Car
        </button>
      </form>

      {cars.length === 0 ? (
        <p className="text-sm text-neutral-500">No cars yet. Add one above.</p>
      ) : (
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b text-left">
              <th className="pb-2 font-medium">ID</th>
              <th className="pb-2 font-medium">Make</th>
              <th className="pb-2 font-medium">Model</th>
              <th className="pb-2 font-medium">Year</th>
              <th className="pb-2 font-medium">Color</th>
              <th className="pb-2 font-medium"></th>
            </tr>
          </thead>
          <tbody>
            {cars.map((car) => (
              <tr key={car.id} className="border-b">
                <td className="py-3">{car.id}</td>
                <td className="py-3">{car.make}</td>
                <td className="py-3">{car.model}</td>
                <td className="py-3">{car.year}</td>
                <td className="py-3">
                  <span className="flex items-center gap-2">
                    <span
                      className="inline-block h-4 w-4 rounded-full border"
                      style={{ backgroundColor: car.color }}
                    />
                    {car.color}
                  </span>
                </td>
                <td className="py-3">
                  <button
                    onClick={() => deleteCar(car.id)}
                    className="rounded-md border px-3 py-1 text-sm text-red-600 hover:bg-red-50"
                  >
                    Delete
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}
