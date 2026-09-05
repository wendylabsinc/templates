import { useEffect, useState } from "react"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Checkbox } from "@/components/ui/checkbox"
import { Alert, AlertDescription } from "@/components/ui/alert"
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
import { Trash2Icon, PlusIcon, PencilIcon, AlertCircleIcon } from "lucide-react"

interface Car {
  id: number
  make: string
  model: string
  color: string
  year: number
  created_at: string | null
  updated_at: string | null
}

function formatDate(d: string | null) {
  if (!d) return "—"
  try {
    return new Date(d + "Z").toLocaleString(undefined, {
      month: "short", day: "numeric", year: "numeric",
      hour: "2-digit", minute: "2-digit",
    })
  } catch { return d }
}

export default function PersistencePage() {
  const [cars, setCars] = useState<Car[]>([])
  const [selected, setSelected] = useState<Set<number>>(new Set())
  const [error, setError] = useState<string | null>(null)

  // Add dialog state
  const [addOpen, setAddOpen] = useState(false)
  const [addMake, setAddMake] = useState("")
  const [addModel, setAddModel] = useState("")
  const [addColor, setAddColor] = useState("#3b82f6")
  const [addYear, setAddYear] = useState(new Date().getFullYear())

  // Edit dialog state
  const [editOpen, setEditOpen] = useState(false)
  const [editCar, setEditCar] = useState<Car | null>(null)
  const [editMake, setEditMake] = useState("")
  const [editModel, setEditModel] = useState("")
  const [editColor, setEditColor] = useState("")
  const [editYear, setEditYear] = useState(0)

  const fetchCars = () =>
    fetch("/api/cars")
      .then((r) => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json() })
      .then((data) => { setCars(data); setError(null) })
      .catch((e) => setError(`Failed to load cars: ${e.message}`))

  useEffect(() => { fetchCars() }, [])

  const addCar = async () => {
    try {
      const r = await fetch("/api/cars", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ make: addMake, model: addModel, color: addColor, year: addYear }),
      })
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      setAddMake(""); setAddModel(""); setAddColor("#3b82f6"); setAddYear(new Date().getFullYear())
      setAddOpen(false)
      setError(null)
      fetchCars()
    } catch (e: unknown) {
      setError(`Failed to add car: ${e instanceof Error ? e.message : e}`)
    }
  }

  const updateCar = async () => {
    if (!editCar) return
    try {
      const r = await fetch(`/api/cars/${editCar.id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ make: editMake, model: editModel, color: editColor, year: editYear }),
      })
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      setEditOpen(false)
      setError(null)
      fetchCars()
    } catch (e: unknown) {
      setError(`Failed to update car: ${e instanceof Error ? e.message : e}`)
    }
  }

  const deleteSelected = async () => {
    try {
      for (const id of selected) {
        const r = await fetch(`/api/cars/${id}`, { method: "DELETE" })
        if (!r.ok && r.status !== 204) throw new Error(`HTTP ${r.status}`)
      }
      setSelected(new Set())
      setError(null)
      fetchCars()
    } catch (e: unknown) {
      setError(`Failed to delete: ${e instanceof Error ? e.message : e}`)
    }
  }

  const openEdit = (car: Car) => {
    setEditCar(car)
    setEditMake(car.make)
    setEditModel(car.model)
    setEditColor(car.color)
    setEditYear(car.year)
    setEditOpen(true)
  }

  const toggleSelect = (id: number) =>
    setSelected((prev) => { const n = new Set(prev); n.has(id) ? n.delete(id) : n.add(id); return n })

  const toggleAll = () =>
    selected.size === cars.length ? setSelected(new Set()) : setSelected(new Set(cars.map((c) => c.id)))

  return (
    <div className="flex flex-col gap-4 p-4 md:gap-6 md:p-6">
      <h1 className="text-2xl font-bold tracking-tight">Persistence</h1>

      {error && (
        <Alert variant="destructive">
          <AlertCircleIcon className="h-4 w-4" />
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}

      <Card>
        <CardHeader className="flex flex-row items-center justify-between">
          <div>
            <CardTitle className="text-sm font-medium">Cars</CardTitle>
            <CardDescription>
              {cars.length} record{cars.length !== 1 ? "s" : ""} in SQLite at <code className="rounded bg-muted px-1 py-0.5 text-xs">/data/cars.db</code>
            </CardDescription>
          </div>
          <div className="flex gap-2">
            {selected.size > 0 && (
              <AlertDialog>
                <AlertDialogTrigger render={<Button variant="destructive" size="sm" />}>
                  <Trash2Icon className="mr-1 h-4 w-4" />
                  Delete ({selected.size})
                </AlertDialogTrigger>
                <AlertDialogContent>
                  <AlertDialogHeader>
                    <AlertDialogTitle>Delete {selected.size} car{selected.size !== 1 ? "s" : ""}?</AlertDialogTitle>
                    <AlertDialogDescription>This action cannot be undone.</AlertDialogDescription>
                  </AlertDialogHeader>
                  <AlertDialogFooter>
                    <AlertDialogCancel>Cancel</AlertDialogCancel>
                    <AlertDialogAction onClick={deleteSelected}>Delete</AlertDialogAction>
                  </AlertDialogFooter>
                </AlertDialogContent>
              </AlertDialog>
            )}

            <AlertDialog open={addOpen} onOpenChange={setAddOpen}>
              <AlertDialogTrigger render={<Button size="sm" />}>
                <PlusIcon className="mr-1 h-4 w-4" />
                Add Car
              </AlertDialogTrigger>
              <AlertDialogContent>
                <AlertDialogHeader>
                  <AlertDialogTitle>Add Car</AlertDialogTitle>
                  <AlertDialogDescription>Add a new car to the database.</AlertDialogDescription>
                </AlertDialogHeader>
                <div className="grid gap-3 py-4">
                  <div className="grid grid-cols-4 items-center gap-3">
                    <Label className="text-right">Make</Label>
                    <Input className="col-span-3" value={addMake} onChange={(e) => setAddMake(e.target.value)} placeholder="Toyota" />
                  </div>
                  <div className="grid grid-cols-4 items-center gap-3">
                    <Label className="text-right">Model</Label>
                    <Input className="col-span-3" value={addModel} onChange={(e) => setAddModel(e.target.value)} placeholder="Camry" />
                  </div>
                  <div className="grid grid-cols-4 items-center gap-3">
                    <Label className="text-right">Year</Label>
                    <Input className="col-span-3" type="number" value={addYear} onChange={(e) => setAddYear(Number(e.target.value))} />
                  </div>
                  <div className="grid grid-cols-4 items-center gap-3">
                    <Label className="text-right">Color</Label>
                    <Input className="col-span-3 h-9 w-20 cursor-pointer p-1" type="color" value={addColor} onChange={(e) => setAddColor(e.target.value)} />
                  </div>
                </div>
                <AlertDialogFooter>
                  <AlertDialogCancel>Cancel</AlertDialogCancel>
                  <AlertDialogAction onClick={addCar} disabled={!addMake || !addModel}>Add</AlertDialogAction>
                </AlertDialogFooter>
              </AlertDialogContent>
            </AlertDialog>
          </div>
        </CardHeader>
        <CardContent>
          {cars.length === 0 ? (
            <p className="py-8 text-center text-sm text-muted-foreground">No cars yet. Click Add Car to get started.</p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="w-10">
                    <Checkbox checked={selected.size === cars.length && cars.length > 0} onCheckedChange={toggleAll} />
                  </TableHead>
                  <TableHead className="w-16">ID</TableHead>
                  <TableHead>Make</TableHead>
                  <TableHead>Model</TableHead>
                  <TableHead className="w-20">Year</TableHead>
                  <TableHead className="w-28">Color</TableHead>
                  <TableHead>Created</TableHead>
                  <TableHead>Updated</TableHead>
                  <TableHead className="w-10"></TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {cars.map((car) => (
                  <TableRow key={car.id}>
                    <TableCell>
                      <Checkbox checked={selected.has(car.id)} onCheckedChange={() => toggleSelect(car.id)} />
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
                    <TableCell className="text-xs text-muted-foreground">{formatDate(car.created_at)}</TableCell>
                    <TableCell className="text-xs text-muted-foreground">{formatDate(car.updated_at)}</TableCell>
                    <TableCell>
                      <Button variant="ghost" size="icon" className="h-8 w-8" onClick={() => openEdit(car)}>
                        <PencilIcon className="h-4 w-4" />
                      </Button>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      {/* Edit dialog (not attached to a trigger — controlled via state) */}
      <AlertDialog open={editOpen} onOpenChange={setEditOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Edit Car #{editCar?.id}</AlertDialogTitle>
            <AlertDialogDescription>Update the car details.</AlertDialogDescription>
          </AlertDialogHeader>
          <div className="grid gap-3 py-4">
            <div className="grid grid-cols-4 items-center gap-3">
              <Label className="text-right">Make</Label>
              <Input className="col-span-3" value={editMake} onChange={(e) => setEditMake(e.target.value)} />
            </div>
            <div className="grid grid-cols-4 items-center gap-3">
              <Label className="text-right">Model</Label>
              <Input className="col-span-3" value={editModel} onChange={(e) => setEditModel(e.target.value)} />
            </div>
            <div className="grid grid-cols-4 items-center gap-3">
              <Label className="text-right">Year</Label>
              <Input className="col-span-3" type="number" value={editYear} onChange={(e) => setEditYear(Number(e.target.value))} />
            </div>
            <div className="grid grid-cols-4 items-center gap-3">
              <Label className="text-right">Color</Label>
              <Input className="col-span-3 h-9 w-20 cursor-pointer p-1" type="color" value={editColor} onChange={(e) => setEditColor(e.target.value)} />
            </div>
          </div>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction onClick={updateCar} disabled={!editMake || !editModel}>Save</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}
