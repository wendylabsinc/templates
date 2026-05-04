declare module "three.meshline" {
  import * as THREE from "three"

  export class MeshLine extends THREE.BufferGeometry {
    setPoints(points: Float32Array | number[], widthCallback?: (p: number) => number): void
  }

  export class MeshLineMaterial extends THREE.ShaderMaterial {
    constructor(parameters?: Record<string, unknown>)
    lineWidth: number
    color: THREE.Color
    opacity: number
    transparent: boolean
  }
}
