import React from "react";
import ReactDOM from "react-dom/client";
import { App } from "./App";
import { LocalizationProvider } from "./i18n";
import "./styles.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <LocalizationProvider>
      <App />
    </LocalizationProvider>
  </React.StrictMode>,
);
