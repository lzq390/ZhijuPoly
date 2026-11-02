export const SIMULATION_PAYMENT_PATH = "/billing/simulate-pay";

export const isSimulationPaymentPath = (pathname: string) => pathname.replace(/\/+$/, "") === SIMULATION_PAYMENT_PATH;
