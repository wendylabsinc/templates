"use client"

import * as React from "react"
import { Canvas, useFrame, extend, useThree } from "@react-three/fiber"
import { Bloom, EffectComposer } from "@react-three/postprocessing"
import * as THREE from "three"
import { MeshLine, MeshLineMaterial } from "three.meshline"

extend({ MeshLine, MeshLineMaterial })

const MIC_COLOR = "#3B82F6"
const BOT_COLOR = "#50C878"

// Particle System component
function Particles({ count = 200, color = "#50C878" }) {
  const mesh = React.useRef<THREE.Points>(null!)
  const { viewport } = useThree()

  const positions = React.useMemo(() => {
    const pos = new Float32Array(count * 3)
    for (let i = 0; i < count; i++) {
      // Scale to viewport width
      pos[i * 3] = (Math.random() - 0.5) * viewport.width * 1.5
      pos[i * 3 + 1] = (Math.random() - 0.5) * viewport.height * 0.2
      pos[i * 3 + 2] = (Math.random() - 0.5) * 2
    }
    return pos
  }, [count, viewport.width, viewport.height])

  const speeds = React.useMemo(() => {
    return new Float32Array(count).map(() => Math.random() * 0.01 + 0.005)
  }, [count])

  const yOffsets = React.useMemo(() => {
    return new Float32Array(count).map(() => Math.random() * Math.PI * 2)
  }, [count])

  useFrame((state) => {
    const time = state.clock.getElapsedTime()
    const array = mesh.current.geometry.attributes.position.array as Float32Array
    // Bound wrap relative to viewport
    const bound = (viewport.width / 2) * 1.2
    for (let i = 0; i < count; i++) {
      array[i * 3] += speeds[i]
      if (array[i * 3] > bound) array[i * 3] = -bound
      array[i * 3 + 1] += Math.sin(time + yOffsets[i]) * 0.002
    }
    mesh.current.geometry.attributes.position.needsUpdate = true
  })

  return (
    <points ref={mesh}>
      <bufferGeometry>
        <bufferAttribute
          attach="attributes-position"
          args={[positions, 3]}
        />
      </bufferGeometry>
      <pointsMaterial size={0.06} color={new THREE.Color(color).multiplyScalar(2)} transparent opacity={0.6} blending={THREE.AdditiveBlending} />
    </points>
  )
}

// Lifestream Line component
function LifeLine({
  index,
  total,
  audioData,
  color = "#50C878",
}: {
  index: number
  total: number
  audioData: Uint8Array
  color?: string
}) {
  const materialRef = React.useRef<THREE.ShaderMaterial | null>(null)
  const { viewport } = useThree()
  const pointsCount = 80
  const smoothedAudio = React.useRef(0)

  const lineGeometry = React.useMemo(() => new MeshLine(), [])

  const initialY = React.useMemo(() => (Math.random() - 0.5) * 0.2, [])
  const drift = React.useMemo(() => Math.random() * Math.PI * 2, [])
  const speed = React.useMemo(() => 0.15 + Math.random() * 0.2, [])

  const widthCallback = React.useCallback((p: number) => {
    const taper = Math.sin(p * Math.PI)
    return taper * 0.25
  }, [])

  useFrame((state) => {
    const time = state.clock.getElapsedTime() * speed
    const currentPoints = new Float32Array(pointsCount * 3)

    let targetAudio = 0
    if (audioData && audioData.length > 0) {
      const sampleIdx = Math.floor((index / total) * audioData.length)
      targetAudio = (audioData[sampleIdx] || 0) / 255.0
    }

    smoothedAudio.current += (targetAudio - smoothedAudio.current) * 0.12
    const audioValue = smoothedAudio.current

    // Responsive width
    const lineWidth = viewport.width * 1.1
    const halfWidth = lineWidth / 2

    for (let i = 0; i < pointsCount; i++) {
      const p = i / (pointsCount - 1)
      const x = p * lineWidth - halfWidth
      const audioWeight = Math.sin(p * Math.PI)

      const y = initialY +
                Math.sin(x * 0.5 + time + drift) * 0.15 * audioWeight +
                (audioValue * 3.5) * Math.sin(x * 0.2 + time * 2) * audioWeight

      const z = Math.cos(x * 0.3 + time + drift) * 0.1 * audioWeight

      currentPoints[i * 3] = x
      currentPoints[i * 3 + 1] = y
      currentPoints[i * 3 + 2] = z
    }

    lineGeometry.setPoints(currentPoints, widthCallback)
  })

  return (
    <mesh>
      <primitive object={lineGeometry} attach="geometry" />
      <meshLineMaterial
        ref={materialRef}
        transparent
        depthTest={false}
        lineWidth={0.15}
        color={new THREE.Color(color).multiplyScalar(2)}
        opacity={0.5}
        blending={THREE.AdditiveBlending}
      />
    </mesh>
  )
}

function Scene({
  micAnalyser,
  botAnalyser,
  botSpeaking,
  lineCount,
  localMicLevel,
  localBotLevel,
}: {
  micAnalyser: AnalyserNode | null
  botAnalyser: AnalyserNode | null
  botSpeaking: boolean
  lineCount: number
  /** Local-mode RMS level (0..1) for the user mic. Drives the blue
   *  lines when there's no AnalyserNode (i.e. bot is in local mode,
   *  no audio reaches the browser). */
  localMicLevel?: number
  /** Local-mode RMS level (0..1) for the bot TTS. */
  localBotLevel?: number
}) {
  const micDataArray = React.useMemo(
    () => (micAnalyser ? new Uint8Array(micAnalyser.frequencyBinCount) : null),
    [micAnalyser],
  )
  const botDataArray = React.useMemo(
    () => (botAnalyser ? new Uint8Array(botAnalyser.frequencyBinCount) : null),
    [botAnalyser],
  )
  // Synthetic spectra we drive ourselves when there's no real
  // AnalyserNode. Two cases:
  //   * Bot in browser-mic mode → AnalyserNodes exist → unused.
  //   * Bot in local mode → no AnalyserNodes → we receive RMS levels
  //     over /api/audio-levels and shape them into a wandering
  //     sine pattern so the lines move organically.
  // We also keep the original "botSpeaking flag" fallback so the
  // emerald lines still animate even if /api/audio-levels isn't
  // connected (e.g. trusted-LAN bots on older builds).
  const syntheticMicData = React.useMemo(() => new Uint8Array(64), [])
  const syntheticBotData = React.useMemo(() => new Uint8Array(64), [])
  const syntheticDecay = React.useRef(0)

  useFrame((state) => {
    if (micAnalyser && micDataArray) micAnalyser.getByteFrequencyData(micDataArray)
    if (botAnalyser && botDataArray) botAnalyser.getByteFrequencyData(botDataArray)

    // ----- synthesize mic data from local level stream -----
    if (!micAnalyser) {
      const t = state.clock.getElapsedTime()
      const amp = Math.max(0, Math.min(1, localMicLevel ?? 0))
      for (let i = 0; i < syntheticMicData.length; i++) {
        const phase = t * 5 + i * 0.4
        const v = (Math.sin(phase) * 0.5 + 0.5) * (0.6 + Math.sin(t + i) * 0.4)
        // Boost the visual gain — raw int16 RMS is small even at
        // normal voice volume. Map ~0.1 RMS to a full-amplitude line.
        syntheticMicData[i] = Math.floor(v * Math.min(1, amp * 6) * 220)
      }
    }

    // ----- synthesize bot data from local level OR botSpeaking flag -----
    if (!botAnalyser) {
      const t = state.clock.getElapsedTime()
      let amp: number
      if (typeof localBotLevel === "number" && localBotLevel > 0) {
        // Have a live RMS stream — use it directly (with the same
        // visual-gain boost as the mic).
        amp = Math.min(1, localBotLevel * 6)
        // Keep the decay ref in sync so a transient drop to 0
        // doesn't flicker the lines off.
        syntheticDecay.current = Math.max(syntheticDecay.current, amp)
      } else {
        // Fall back to the binary botSpeaking flag with smoothing.
        const target = botSpeaking ? 1 : 0
        syntheticDecay.current += (target - syntheticDecay.current) * 0.1
        amp = syntheticDecay.current
      }
      // Always bleed decay slightly so paused TTS frames fade.
      syntheticDecay.current *= 0.92
      for (let i = 0; i < syntheticBotData.length; i++) {
        const phase = t * 4 + i * 0.35
        const v = (Math.sin(phase) * 0.5 + 0.5) * (0.6 + Math.sin(t + i) * 0.4)
        syntheticBotData[i] = Math.floor(v * amp * 220)
      }
    }
  })

  const perGroup = Math.max(1, Math.floor(lineCount / 2))
  const emptyData = React.useMemo(() => new Uint8Array(0), [])
  const micData = micDataArray ?? syntheticMicData
  const botData = botDataArray ?? syntheticBotData

  return (
    <>
      <color attach="background" args={["black"]} />

      {Array.from({ length: perGroup }).map((_, i) => (
        <LifeLine
          key={`mic-${i}`}
          index={i}
          total={perGroup}
          audioData={micData || emptyData}
          color={MIC_COLOR}
        />
      ))}

      {Array.from({ length: perGroup }).map((_, i) => (
        <LifeLine
          key={`bot-${i}`}
          index={i}
          total={perGroup}
          audioData={botData || emptyData}
          color={BOT_COLOR}
        />
      ))}

      <Particles count={400} color={BOT_COLOR} />

      <EffectComposer enableNormalPass={false}>
        <Bloom
          luminanceThreshold={0.4}
          mipmapBlur
          intensity={0.8}
          radius={0.4}
        />
      </EffectComposer>
    </>
  )
}

export function LifestreamVisualizer({
  micAnalyser,
  botAnalyser,
  botSpeaking = false,
  lineCount = 40,
  localMicLevel,
  localBotLevel,
}: {
  /** AnalyserNode for the user's microphone input. Lines rendered in blue. */
  micAnalyser: AnalyserNode | null
  /** AnalyserNode for the bot's TTS output, when one is available. Lines rendered in emerald. */
  botAnalyser: AnalyserNode | null
  /** Fallback signal: when there's no botAnalyser but the bot is producing
   *  TTS, drive the emerald lines with synthetic data so the visualizer
   *  still reflects bot speech. */
  botSpeaking?: boolean
  /** Total line count, split evenly between mic and bot groups. */
  lineCount?: number
  /** Local-mode mic RMS level from /api/audio-levels (0..1). Used to drive
   *  the blue lines when ``micAnalyser`` is null (i.e. the bot is in
   *  local mode and audio never reaches the browser). */
  localMicLevel?: number
  /** Local-mode bot RMS level (0..1). Used to drive the emerald lines
   *  when ``botAnalyser`` is null. */
  localBotLevel?: number
}) {
  return (
    <div className="absolute inset-0 bg-black">
      <Canvas
        camera={{ position: [0, 0, 8], fov: 50 }}
        gl={{
          antialias: false,
          toneMapping: THREE.NoToneMapping,
        }}
      >
        <Scene
          micAnalyser={micAnalyser}
          botAnalyser={botAnalyser}
          botSpeaking={botSpeaking}
          lineCount={lineCount}
          localMicLevel={localMicLevel}
          localBotLevel={localBotLevel}
        />
      </Canvas>
    </div>
  )
}
