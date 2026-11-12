// Generate the functional walkthroughs; preserve the separately maintained knowledge GIF.
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
const generator=fileURLToPath(new URL("./generate-research-workflows.cjs",import.meta.url));
const child=spawn(process.execPath,[generator,...process.argv.slice(2)],{stdio:"inherit"});
child.on("error",error=>{console.error(error);process.exitCode=1});
child.on("exit",code=>{process.exitCode=code??1});
