import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import Demo from "./Demo";
import "./index.css";

// /config.json comes from Caddy (DEMO_ASK_KEY in the server's .env). With a key: the ask-only public demo.
// Without one (dev, or a server not configured yet): the full console, where you paste your own key.
async function demoKey(): Promise<string | null> {
  try {
    const res = await fetch("/config.json", { cache: "no-store" });
    if (!res.ok || !(res.headers.get("content-type") ?? "").includes("application/json")) return null;
    const body = (await res.json()) as { demoKey?: unknown };
    return typeof body.demoKey === "string" && body.demoKey.trim() ? body.demoKey.trim() : null;
  } catch {
    return null;
  }
}

const root = createRoot(document.getElementById("root")!);
demoKey().then((key) =>
  root.render(<StrictMode>{key ? <Demo demoKey={key} /> : <App />}</StrictMode>),
);
