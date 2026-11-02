import React from "react";
import ReactDOM from "react-dom/client";
import { SimulatedPaymentPage } from "./components/SimulatedPaymentPage";

export function mountSimulationPayment() {
  ReactDOM.createRoot(document.getElementById("root")!).render(<React.StrictMode><SimulatedPaymentPage /></React.StrictMode>);
}
