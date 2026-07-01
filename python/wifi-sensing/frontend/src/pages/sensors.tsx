import { useState } from "react"
import { toast } from "sonner"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { Button } from "@/components/ui/button"
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
import { useSensing } from "@/hooks/use-sensing-stream"

export default function SensorsPage() {
  const { frame } = useSensing()
  const sensors = frame?.sensors ?? []
  const [calibrating, setCalibrating] = useState(false)

  async function calibrate() {
    setCalibrating(true)
    try {
      const res = await fetch("/api/calibrate", { method: "POST" })
      if (res.ok) {
        const body = await res.json()
        toast.success(`Calibrated (baseline ${Number(body.baseline).toExponential(2)})`)
      } else {
        const body = await res.json().catch(() => ({ detail: "Calibration failed" }))
        toast.error(body.detail ?? "Calibration failed")
      }
    } catch {
      toast.error("Calibration request failed")
    } finally {
      setCalibrating(false)
    }
  }

  return (
    <div className="flex flex-col gap-4 p-4 md:gap-6 md:p-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold tracking-tight">Sensors</h1>
        <AlertDialog>
          <AlertDialogTrigger
            render={<Button disabled={calibrating}>Calibrate empty room</Button>}
          />
          <AlertDialogContent>
            <AlertDialogHeader>
              <AlertDialogTitle>Calibrate the empty-room baseline?</AlertDialogTitle>
              <AlertDialogDescription>
                Make sure the room is empty and still. The current CSI variance becomes
                the reference for presence and motion detection.
              </AlertDialogDescription>
            </AlertDialogHeader>
            <AlertDialogFooter>
              <AlertDialogCancel>Cancel</AlertDialogCancel>
              <AlertDialogAction onClick={calibrate}>Calibrate</AlertDialogAction>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>
      </div>

      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Sensor (MAC)</TableHead>
            <TableHead>RSSI</TableHead>
            <TableHead>Channel</TableHead>
            <TableHead>Packets</TableHead>
            <TableHead>Malformed</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {sensors.length === 0 ? (
            <TableRow>
              <TableCell colSpan={5} className="text-center text-muted-foreground">
                No sensors detected. Point an ESP32 CSI sensor at this device's UDP port.
              </TableCell>
            </TableRow>
          ) : (
            sensors.map((s) => (
              <TableRow key={s.link_id}>
                <TableCell className="font-mono">{s.link_id}</TableCell>
                <TableCell>{s.rssi} dBm</TableCell>
                <TableCell>{s.channel}</TableCell>
                <TableCell>{s.packets}</TableCell>
                <TableCell>{s.malformed}</TableCell>
              </TableRow>
            ))
          )}
        </TableBody>
      </Table>
    </div>
  )
}
