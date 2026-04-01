import express, { Request, Response } from "express";

const app = express();
const port = {{PORT}};
const hostname = process.env.WENDY_HOSTNAME || "0.0.0.0";

app.use(express.json());

app.get("/", (_req: Request, res: Response) => {
  console.log("Received request: GET /");
  res.json({ message: "hello-world" });
});

app.get("/health", (_req: Request, res: Response) => {
  res.json({ status: "ok" });
});

interface CreateItemBody {
  name: string;
  price: number;
}

interface ItemResponse {
  id: number;
  name: string;
  price: number;
}

app.post(
  "/items",
  (req: Request<{}, ItemResponse, CreateItemBody>, res: Response) => {
    console.log(`Received request: POST /items - ${req.body.name}`);
    const item: ItemResponse = {
      id: 1,
      name: req.body.name,
      price: req.body.price,
    };
    res.status(201).json(item);
  }
);

app.listen(port, () => {
  console.log(`Server running on http://${hostname}:${port}`);
});
