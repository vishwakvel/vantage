import React from "react";
import ReactDOM from "react-dom/client";

// Placeholder root. The real App component (06-07) replaces this div with the
// login form + run panel; kept self-contained here so `npm run build` stays
// green in this plan without importing App.css yet.
ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <div>Vantage</div>
  </React.StrictMode>,
);
