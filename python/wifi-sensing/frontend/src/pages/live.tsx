import { useEffect, useRef, useState } from "react"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { useSensing } from "@/hooks/use-sensing-stream"

function ConfidenceBar({ value }: { value: number }) {
  return (
    <div className="h-2 w-full rounded-full bg-muted">
      <div
        className="h-2 rounded-full bg-primary transition-all"
        style={{ width: `${Math.round(Math.max(0, Math.min(1, value)) * 100)}%` }}
      />
    </div>
  )
}

function Sparkline({ values }: { values: number[] }) {
  const w = 240
  const h = 48
  if (values.length < 2) return <svg width={w} height={h} className="w-full" />
  const max = 1
  const step = w / (values.length - 1)
  const pts = values
    .map((v, i) => `${i * step},${h - Math.max(0, Math.min(1, v / max)) * h}`)
    .join(" ")
  return (
    <svg viewBox={`0 0 ${w} ${h}`} className="w-full" preserveAspectRatio="none">
      <polyline points={pts} fill="none" stroke="currentColor" strokeWidth="2" className="text-primary" />
    </svg>
  )
}

export default function LivePage() {
  const { frame } = useSensing()
  const [motionHistory, setMotionHistory] = useState<number[]>([])
  const tsRef = useRef<number>(0)

  useEffect(() => {
    if (frame && frame.timestamp !== tsRef.current) {
      tsRef.current = frame.timestamp
      setMotionHistory((h) => [...h.slice(-59), frame.motion])
    }
  }, [frame])

  const occupied = frame?.occupied ?? false
  const motion = frame?.motion ?? 0

  return (
    <div className="flex flex-col gap-4 p-4 md:gap-6 md:p-6">
      <h1 className="text-2xl font-bold tracking-tight">Live</h1>
      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
        {/* Presence */}
        <Card>
          <CardHeader>
            <CardTitle className="text-sm font-medium">Presence</CardTitle>
            <CardDescription>Room occupancy</CardDescription>
          </CardHeader>
          <CardContent>
            <div className="flex items-center gap-3">
              <span
                className={`inline-block h-3 w-3 rounded-full ${occupied ? "bg-green-500" : "bg-muted-foreground/40"}`}
              />
              <span className="text-2xl font-semibold">
                {frame ? (occupied ? "Occupied" : "Empty") : "—"}
              </span>
            </div>
          </CardContent>
        </Card>

        {/* Motion */}
        <Card>
          <CardHeader>
            <CardTitle className="text-sm font-medium">Motion</CardTitle>
            <CardDescription>Movement intensity</CardDescription>
          </CardHeader>
          <CardContent className="space-y-2">
            <div className="text-2xl font-semibold">{(motion * 100).toFixed(0)}%</div>
            <div className="text-primary">
              <Sparkline values={motionHistory} />
            </div>
          </CardContent>
        </Card>

        {/* Breathing */}
        <Card>
          <CardHeader>
            <CardTitle className="text-sm font-medium">Breathing</CardTitle>
            <CardDescription>Respiration rate</CardDescription>
          </CardHeader>
          <CardContent className="space-y-2">
            <div className="text-2xl font-semibold">
              {frame?.breathing_bpm != null ? `${frame.breathing_bpm.toFixed(1)} BPM` : "—"}
            </div>
            {frame?.breathing_bpm == null ? (
              <p className="text-xs text-muted-foreground">
                {occupied ? "Hold still for a reading" : "No subject detected"}
              </p>
            ) : (
              <ConfidenceBar value={frame.breathing_conf} />
            )}
          </CardContent>
        </Card>

        {/* Heart rate */}
        <Card>
          <CardHeader>
            <div className="flex items-center justify-between">
              <CardTitle className="text-sm font-medium">Heart Rate</CardTitle>
              <Tooltip>
                <TooltipTrigger
                  render={<Badge variant="outline" className="cursor-help">Experimental</Badge>}
                />
                <TooltipContent className="max-w-56">
                  Heart rate from CSI is best-effort: it needs a stationary subject,
                  a clean signal, and good sensor placement.
                </TooltipContent>
              </Tooltip>
            </div>
            <CardDescription>Cardiac rate</CardDescription>
          </CardHeader>
          <CardContent className="space-y-2">
            <div className="text-2xl font-semibold">
              {frame?.heart_bpm != null ? `${frame.heart_bpm.toFixed(0)} BPM` : "—"}
            </div>
            {frame?.heart_bpm == null ? (
              <p className="text-xs text-muted-foreground">Low confidence</p>
            ) : (
              <ConfidenceBar value={frame.heart_conf} />
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  )
}
