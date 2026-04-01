import express, { Request, Response } from "express";
import path from "path";
import { fileURLToPath } from "url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

interface Car {
    id: number;
    make: string;
    model: string;
    color: string;
    year: number;
}

let nextId = 1;
const cars: Car[] = [];

const app = express();
app.use(express.json());

const PORT = {{.PORT}};
const WENDY_HOSTNAME = process.env.WENDY_HOSTNAME ?? "localhost";

// --- CRUD API ---

app.get("/api/cars", (_req: Request, res: Response) => {
    res.json(cars);
});

app.post("/api/cars", (req: Request, res: Response) => {
    const { make, model, color, year } = req.body;
    const car: Car = { id: nextId++, make, model, color, year };
    cars.push(car);
    res.status(201).json(car);
});

app.get("/api/cars/:id", (req: Request, res: Response) => {
    const car = cars.find((c) => c.id === Number(req.params.id));
    if (!car) {
        res.status(404).json({ error: "Car not found" });
        return;
    }
    res.json(car);
});

app.put("/api/cars/:id", (req: Request, res: Response) => {
    const idx = cars.findIndex((c) => c.id === Number(req.params.id));
    if (idx === -1) {
        res.status(404).json({ error: "Car not found" });
        return;
    }
    const { make, model, color, year } = req.body;
    cars[idx] = { ...cars[idx], make, model, color, year };
    res.json(cars[idx]);
});

app.delete("/api/cars/:id", (req: Request, res: Response) => {
    const idx = cars.findIndex((c) => c.id === Number(req.params.id));
    if (idx === -1) {
        res.status(404).json({ error: "Car not found" });
        return;
    }
    cars.splice(idx, 1);
    res.status(204).send();
});

// --- Static files & SPA fallback ---

const staticDir = path.join(__dirname, "..", "static");
app.use(express.static(staticDir));

app.get("*", (_req: Request, res: Response) => {
    res.sendFile(path.join(staticDir, "index.html"));
});

// --- Start ---

app.listen(PORT, () => {
    console.log(`Server running on http://${WENDY_HOSTNAME}:${PORT}`);
});
