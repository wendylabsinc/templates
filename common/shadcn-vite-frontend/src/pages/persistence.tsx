import { useEffect, useState, type FormEvent } from "react"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog"
import { Trash2Icon, PlusIcon } from "lucide-react"

interface Car {
  id: number
  make: string
  model: string
  color: string
  year: number
}

export default function PersistencePage() {
  const [cars, setCars] = useState<Car[]>([])
  const [make, setMake] = useState("")
  const [model, setModel] = useState("")
  const [color, setColor] = useState("#3b82f6")
  const [year, setYear] = useState(new Date().getFullYear())
  const [selected, setSelected] = useState<Set<number>>(new Set())

  const fetchCars = () =>
    fetch("/api/cars")
      .then((r) => r.json())
      .then(setCars)
      .catch(() => {})

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

  const deleteSelected = async () => {
    await Promise.all(
      Array.from(selected).map((id) =>
        fetch(`/api/cars/${id}`, { method: "DELETE" })
      )
    )
    setSelected(new Set())
    fetchCars()
  }

  const toggleSelect = (id: number) => {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const toggleAll = () => {
    if (selected.size === cars.length) setSelected(new Set())
    else setSelected(new Set(cars.map((c) => c.id)))
  }

  return (
    <div className="flex flex-col gap-4 p-4 md:gap-6 md:p-6">
      <h1 className="text-2xl font-bold tracking-tight">Persistence</h1>

      {/* Add car form */}
      <Card>
        <CardHeader>
          <CardTitle className="text-sm font-medium">Add Car</CardTitle>
          <CardDescription>
            Cars are stored in SQLite at <code className="rounded bg-muted px-1 py-0.5">/data/cars.db</code> — persisted across container restarts.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={addCar} className="flex flex-wrap items-end gap-3">
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="make">Make</Label>
              <Input id="make" value={make} onChange={(e) => setMake(e.target.value)} placeholder="Toyota" required className="w-32" />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="model">Model</Label>
              <Input id="model" value={model} onChange={(e) => setModel(e.target.value)} placeholder="Camry" required className="w-32" />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="year">Year</Label>
              <Input id="year" type="number" value={year} onChange={(e) => setYear(Number(e.target.value))} required className="w-24" />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="color">Color</Label>
              <Input id="color" type="color" value={color} onChange={(e) => setColor(e.target.value)} className="h-9 w-14 cursor-pointer p-1" />
            </div>
            <Button type="submit" size="sm">
              <PlusIcon className="mr-1 h-4 w-4" />
              Add
            </Button>
          </form>
        </CardContent>
      </Card>

      {/* Cars table */}
      <Card>
        <CardHeader className="flex flex-row items-center justify-between">
          <div>
            <CardTitle className="text-sm font-medium">Cars</CardTitle>
            <CardDescription>{cars.length} record{cars.length !== 1 ? "s" : ""} in database</CardDescription>
          </div>
          {selected.size > 0 && (
            <AlertDialog>
              <AlertDialogTrigger render={<Button variant="destructive" size="sm" />}>
                <Trash2Icon className="mr-1 h-4 w-4" />
                Delete ({selected.size})
              </AlertDialogTrigger>
              <AlertDialogContent>
                <AlertDialogHeader>
                  <AlertDialogTitle>Delete {selected.size} car{selected.size !== 1 ? "s" : ""}?</AlertDialogTitle>
                  <AlertDialogDescription>
                    This action cannot be undone. The selected cars will be permanently removed from the database.
                  </AlertDialogDescription>
                </AlertDialogHeader>
                <AlertDialogFooter>
                  <AlertDialogCancel>Cancel</AlertDialogCancel>
                  <AlertDialogAction onClick={deleteSelected}>Delete</AlertDialogAction>
                </AlertDialogFooter>
              </AlertDialogContent>
            </AlertDialog>
          )}
        </CardHeader>
        <CardContent>
          {cars.length === 0 ? (
            <p className="text-sm text-muted-foreground py-8 text-center">No cars yet. Add one above.</p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="w-10">
                    <input
                      type="checkbox"
                      checked={selected.size === cars.length && cars.length > 0}
                      onChange={toggleAll}
                      className="rounded"
                    />
                  </TableHead>
                  <TableHead className="w-16">ID</TableHead>
                  <TableHead>Make</TableHead>
                  <TableHead>Model</TableHead>
                  <TableHead className="w-20">Year</TableHead>
                  <TableHead className="w-28">Color</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {cars.map((car) => (
                  <TableRow key={car.id}>
                    <TableCell>
                      <input
                        type="checkbox"
                        checked={selected.has(car.id)}
                        onChange={() => toggleSelect(car.id)}
                        className="rounded"
                      />
                    </TableCell>
                    <TableCell className="font-mono text-muted-foreground">{car.id}</TableCell>
                    <TableCell className="font-medium">{car.make}</TableCell>
                    <TableCell>{car.model}</TableCell>
                    <TableCell>{car.year}</TableCell>
                    <TableCell>
                      <span className="flex items-center gap-2">
                        <span className="inline-block h-4 w-4 rounded-full border" style={{ backgroundColor: car.color }} />
                        <span className="font-mono text-xs text-muted-foreground">{car.color}</span>
                      </span>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
