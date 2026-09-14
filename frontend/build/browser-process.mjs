// Module-local polyfill: never exposes process or build-machine environment on window.
export { default as process } from 'process/browser.js';
