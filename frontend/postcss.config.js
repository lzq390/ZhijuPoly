import tailwindcss from "tailwindcss";
import autoprefixer from "autoprefixer";
import scopeKetcherCss from "./build/scope-ketcher-css.mjs";

export default { plugins: [tailwindcss(), autoprefixer(), scopeKetcherCss()] };
