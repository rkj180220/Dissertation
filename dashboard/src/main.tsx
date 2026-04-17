import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { PipelineProvider } from "@/context/PipelineContext";
import App from "./App";
import "./index.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <BrowserRouter>
      <PipelineProvider>
        <App />
      </PipelineProvider>
    </BrowserRouter>
  </StrictMode>,
);
