import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import App from "./App";
import "./index.css";
import { LiveProvider } from "./lib/live";
import { installViewport } from "./lib/viewport";

installViewport();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <BrowserRouter>
      <LiveProvider>
        <App />
      </LiveProvider>
    </BrowserRouter>
  </StrictMode>,
);
