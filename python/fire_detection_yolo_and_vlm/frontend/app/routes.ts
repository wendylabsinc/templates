import { type RouteConfig, index, route } from "@react-router/dev/routes";

export default [
  index("routes/detection.tsx"),
  route("conversations/:id", "routes/conversation.tsx"),
  route("cameras/:id", "routes/camera.tsx"),
] satisfies RouteConfig;
