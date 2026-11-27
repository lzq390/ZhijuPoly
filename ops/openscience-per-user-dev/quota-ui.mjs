import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { join } from "node:path";

const editor = "/assets/session-DxSaVjPe.js";
const digest = "8c8ee4ab20b67c9476428ab865bb6b7809ff9145aec17b238be6f42a1f3718df";
const quotaMessage = "工作区磁盘配额已用完。删除文件后可重试。";
const sharedDiskMessage = "工作区共享磁盘空间不足，暂时无法写入。已有文件仍可读取或删除。";
const monitorMessage = "暂时无法核对工作区磁盘余量，请稍后重试。";

function replaceOnce(source, before, after) {
  if (source.split(before).length !== 2) throw Error("Unexpected November editor layout");
  return source.replace(before, () => after);
}

export async function installQuotaUi(assets, uiRoot) {
  const original = await readFile(join(uiRoot, editor), "utf8");
  if (createHash("sha256").update(original).digest("hex") !== digest) throw Error("Unexpected November editor build");
  const anchor = 'O.$$click=()=>h(k=>k+1),c(O,l(cl,{size:13,strokeWidth:1.6})),';
  // K/O are the editor toolbar and its refresh button; n.url/t()/e.path are
  // the same authenticated endpoint, selected directory and path as native Save.
  const addControl = `addDeleteFileControl(K,O,()=>{
    const file=e.path, directory=t();
    const url=n.url.replace(/\\/$/,"")+"/file/content?directory="+encodeURIComponent(directory);
    return {name:i(), remove:async()=>{
    const res=await(o.fetch??fetch)(url,{method:"DELETE",headers:{"Content-Type":"application/json"},body:JSON.stringify({path:file})});
    if(!res.ok)throw Error("删除失败（"+res.status+"）");
    const receipt=await res.json();
    if(receipt?.deleted!==true)throw Error("未收到文件删除回执，请刷新文件列表。");
    if(e.path===file&&t()===directory)e.onClose?.();
    }};
  }),`;
  // M4 patches the same immutable editor chunk to accept private knowledge
  // documents. Compose its verified result with the quota controls.
  let source = replaceOnce(assets.get(editor) ?? original, anchor, addControl + anchor);
  const saveError = 'if(!ae.ok)throw new Error(`save failed (${ae.status})`);';
  source = replaceOnce(source, saveError, `if(ae.status===507||ae.status===503){
    const reason=(await ae.clone().json().catch(()=>({}))).error;
    if(reason==="workspace_quota_exceeded")throw Error(${JSON.stringify(quotaMessage)});
    if(reason==="workspace_disk_low_watermark")throw Error(${JSON.stringify(sharedDiskMessage)});
    if(reason==="workspace_disk_monitor_unavailable")throw Error(${JSON.stringify(monitorMessage)});
  }` + saveError);
  const modulePath = "/assets/m5/quota-file-controls.mjs";
  assets.set(modulePath, await readFile(new URL("./quota-file-controls.mjs", import.meta.url)));
  assets.set(editor, 'import {addDeleteFileControl} from "' + modulePath + '";\n' + source);
}
