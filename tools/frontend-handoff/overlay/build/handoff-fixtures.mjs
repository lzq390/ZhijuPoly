// Vite bundles this bridge and the TypeScript fixture source. The separate JS
// boundary keeps application types out of the composite Node config project.
export { createHandoffMockFetch } from "../src/mocks/handoffMockFetch.ts";
