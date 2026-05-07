import { type RouteConfig, index, route } from "@react-router/dev/routes";

export default [
  index("routes/dashboard.tsx"),
  route("conversations/:id", "routes/conversation.tsx"),
  route("cameras/:id", "routes/camera.tsx"),
  route("detection", "routes/detection.tsx"),
] satisfies RouteConfig;
