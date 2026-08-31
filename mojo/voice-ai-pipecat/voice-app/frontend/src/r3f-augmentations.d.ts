import "@react-three/fiber"
import type { Object3DNode, MaterialNode } from "@react-three/fiber"
import type { MeshLine, MeshLineMaterial } from "three.meshline"

declare module "@react-three/fiber" {
  interface ThreeElements {
    meshLine: Object3DNode<MeshLine, typeof MeshLine>
    meshLineMaterial: MaterialNode<MeshLineMaterial, typeof MeshLineMaterial>
  }
}
