import { useEffect, useMemo, useRef, useState } from "react"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { useSensing } from "@/hooks/use-sensing-stream"

/** Map a 0..1 value to an RGB string along a blue→green→yellow→red ramp. */
function heat(v: number): string {
  const x = Math.max(0, Math.min(1, v))
  const r = Math.round(255 * Math.min(1, Math.max(0, x * 2 - 0.5)))
  const g = Math.round(255 * Math.min(1, Math.max(0, x < 0.5 ? x * 2 : 2 - x * 2)))
  const b = Math.round(255 * Math.min(1, Math.max(0, 1 - x * 2)))
  return `rgb(${r},${g},${b})`
}

export default function WaterfallPage() {
  const { frame } = useSensing()
  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  const [link, setLink] = useState<string>("")

  const links = useMemo(() => Object.keys(frame?.waterfall ?? {}), [frame])

  useEffect(() => {
    if (links.length > 0 && !links.includes(link)) setLink(links[0])
  }, [links, link])

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const matrix = frame?.waterfall?.[link]
    const ctx = canvas.getContext("2d")
    if (!ctx) return
    ctx.clearRect(0, 0, canvas.width, canvas.height)
    if (!matrix || matrix.length === 0) return

    const rows = matrix.length // time samples
    const cols = matrix[0].length // subcarriers
    // Normalize across the whole matrix.
    let lo = Infinity
    let hi = -Infinity
    for (const row of matrix)
      for (const v of row) {
        if (v < lo) lo = v
        if (v > hi) hi = v
      }
    const span = hi - lo || 1

    const cw = canvas.width / rows
    const ch = canvas.height / cols
    for (let t = 0; t < rows; t++) {
      const row = matrix[t]
      for (let c = 0; c < cols; c++) {
        ctx.fillStyle = heat((row[c] - lo) / span)
        ctx.fillRect(t * cw, c * ch, Math.ceil(cw), Math.ceil(ch))
      }
    }
  }, [frame, link])

  return (
    <div className="flex flex-col gap-4 p-4 md:gap-6 md:p-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold tracking-tight">CSI Waterfall</h1>
        <Select value={link} onValueChange={(v) => setLink(v ?? "")}>
          <SelectTrigger className="w-64">
            <SelectValue placeholder="Select a sensor link" />
          </SelectTrigger>
          <SelectContent>
            {links.map((l) => (
              <SelectItem key={l} value={l} className="font-mono">
                {l}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-sm font-medium">Subcarrier amplitude</CardTitle>
          <CardDescription>
            Time runs left→right; subcarriers stack top→bottom. Color = amplitude (blue low → red high).
          </CardDescription>
        </CardHeader>
        <CardContent>
          {links.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              Waiting for CSI data from a sensor…
            </p>
          ) : (
            <canvas
              ref={canvasRef}
              width={640}
              height={256}
              className="w-full rounded-md border bg-black"
            />
          )}
        </CardContent>
      </Card>
    </div>
  )
}
