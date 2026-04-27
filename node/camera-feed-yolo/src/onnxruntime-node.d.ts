// onnxruntime-node 1.20.x ships a tarball whose package.json points at
// `dist/index.d.ts` but doesn't actually include the file (fixed upstream in
// 1.21+). We pin to ~1.20.0 for ABI compat with dustynv's libonnxruntime, so
// re-export the types from onnxruntime-common — which is what dist/index.js
// effectively does at runtime.
declare module "onnxruntime-node" {
    export * from "onnxruntime-common";
}
