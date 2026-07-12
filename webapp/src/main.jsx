import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import Smaug from "./App.jsx";

createRoot(document.getElementById("root")).render(
  <StrictMode>
    <Smaug />
  </StrictMode>
);
