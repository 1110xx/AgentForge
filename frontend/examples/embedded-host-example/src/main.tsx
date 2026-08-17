import { createRoot } from "react-dom/client";
import { StrictMode } from "react";
import { App } from "./App.js";

const rootElement = document.getElementById("root");
if (rootElement === null) {
  throw new Error("#root element is missing");
}

createRoot(rootElement).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
